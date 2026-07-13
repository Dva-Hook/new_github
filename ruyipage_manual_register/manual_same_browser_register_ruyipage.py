# -*- coding: utf-8 -*-
"""RuyiPage Firefox variant of manual_two_browser_register_v2.py.

目标：
    用 ruyipage/Firefox 替代 cloakbrowser/Chromium。

保持 v2 的核心闭环：
    原注册标签 -> 抓 Arkose publicKey/surl/blob -> 同浏览器新标签手动答题
    -> onCompleted token -> 回原注册标签注入并提交。

注意：
    1. ruyipage 是 Firefox + WebDriver BiDi，不支持 Chrome CDP。
       所以这里不用 CDPBlobCatcher，改用 page.capture 被动抓 /fc/gt2/ 请求体。
    2. 需要先安装 ruyipage runtime：
       python -m pip install ruyiPage --upgrade
       python -m ruyipage install
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import base64
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import ruyipage
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 ruyipage。请先运行：\n"
        "  python -m pip install ruyiPage --upgrade\n"
        "  python -m ruyipage install\n"
    ) from exc

from isolated_proxy_adapter import IsolatedProxyRoute
from register import COUNTRY, REGISTER_URL, generate_identity


LOG = logging.getLogger("ruyipage_same_browser")
DEFAULT_SURL = "blizzard-api.arkoselabs.com"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ruyipage_manual_register" / "runs"
DEFAULT_ISOLATED_ROOT = Path(r"D:\Project\isolated-proxy-browser")
IPIFY_URL = "https://api64.ipify.org?format=json"


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


force_utf8_stdio()


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [RUYI-SAME] %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def screenshot(page, path: Path, full_page: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=full_page)
        LOG.info("Screenshot saved: %s", path)
        return True
    except Exception as exc:
        LOG.warning("screenshot failed %s: %s: %s", path.name, type(exc).__name__, exc)
        return False


def normalize_surl(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return DEFAULT_SURL
    if "://" not in text:
        text = "https://" + text
    parsed = urlsplit(text)
    return parsed.netloc or parsed.path.split("/", 1)[0] or DEFAULT_SURL


def origin_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid URL: {value!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def safe_json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def all_contexts(page) -> list[Any]:
    out = [page]
    try:
        out.extend(page.get_all_frames() or [])
    except Exception:
        pass
    return out


def wait_ele(page, selector: str, desc: str, timeout: float = 25.0):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        for ctx in all_contexts(page):
            try:
                ele = ctx.ele(selector, timeout=0.25)
                if ele:
                    return ele
            except Exception as exc:
                last_exc = exc
        time.sleep(0.25)
    raise TimeoutError(f"等待元素超时: {desc} selector={selector} last={last_exc}")


def wait_or_refresh(page, selector: str, desc: str, timeout: float = 25.0):
    try:
        ele = wait_ele(page, selector, desc, timeout=timeout)
        LOG.info("Ready: %s", desc)
        return ele
    except Exception:
        LOG.warning("等待 %s 超时，刷新重试", desc)
        page.refresh()
        time.sleep(5)
        ele = wait_ele(page, selector, desc, timeout=timeout)
        LOG.info("Ready after refresh: %s", desc)
        return ele


def click_ele(page, selector: str, desc: str, timeout: float = 15.0, by_js: bool = False) -> bool:
    ele = wait_ele(page, selector, desc, timeout=timeout)
    try:
        ele.click(by_js=by_js)
    except TypeError:
        ele.click()
    except Exception:
        if not by_js:
            ele.click(by_js=True)
        else:
            raise
    return True


def elem_value(page, selector: str, timeout: float = 10.0) -> str:
    ele = wait_ele(page, selector, selector, timeout=timeout)
    return ele.value or ele.attr("value") or ""


def set_input_value_js(page, selector: str, value: str, input_type: str = "text") -> Dict[str, Any]:
    return page.run_js(
        """function(selector, value, inputType) {
          const el = document.querySelector(selector);
          if (!el) return {ok:false, reason:'not-found', selector};
          const proto =
            inputType === 'select' ? window.HTMLSelectElement.prototype :
            inputType === 'checkbox' ? window.HTMLInputElement.prototype :
            window.HTMLInputElement.prototype;
          const prop = inputType === 'checkbox' ? 'checked' : 'value';
          const setter = Object.getOwnPropertyDescriptor(proto, prop)?.set;
          if (setter) setter.call(el, value);
          else el[prop] = value;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          el.dispatchEvent(new Event('blur', {bubbles:true}));
          return {ok:true, value: el.value, checked: !!el.checked, selector};
        }""",
        selector,
        value,
        input_type,
        timeout=10,
    )


def fill_birthday_ruyi(page, acc: Dict[str, str]) -> None:
    with contextlib.suppress(Exception):
        wait_ele(page, '[name="dob-plain"]', "birthday trigger", timeout=10).click()
        time.sleep(0.5)
    page.run_js(
        """function(year, month, day) {
          const c = document.querySelector('#dob-field-active') || document;
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          c.querySelectorAll('input').forEach(inp => {
            const cls = inp.className || '';
            const name = inp.name || '';
            if (cls.includes('--yyyy') || /year|yyyy/i.test(name)) setter.call(inp, year);
            else if (cls.includes('--mm') || /month|mm/i.test(name)) setter.call(inp, month);
            else if (cls.includes('--dd') || /day|dd/i.test(name)) setter.call(inp, day);
            inp.dispatchEvent(new Event('input', {bubbles:true}));
            inp.dispatchEvent(new Event('change', {bubbles:true}));
          });
        }""",
        acc["birth_year"],
        acc["birth_month"],
        acc["birth_day"],
        timeout=10,
    )


def close_cookie_banner_ruyi(page) -> None:
    selectors = [
        "button#onetrust-reject-all-handler",
        "button.ot-reject-all",
        'button[id*="reject"]',
        'button[aria-label*="Reject"]',
    ]
    for sel in selectors:
        with contextlib.suppress(Exception):
            ele = page.ele(sel, timeout=0.5)
            if ele and ele.is_displayed:
                ele.click()
                time.sleep(0.3)
                return


def build_solver_harness(public_key: str, blob: str, surl: str, language: str = "en-US") -> str:
    api_url = f"https://{normalize_surl(surl)}/v2/{public_key}/api.js"
    cfg = safe_json_for_script(
        {"publicKey": public_key, "blob": blob, "apiUrl": api_url, "language": language}
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Arkose Manual RuyiPage Worker</title>
  <style>
    html,body{{margin:0;min-height:100%;background:#0f172a;color:#e5e7eb;font-family:Arial,sans-serif}}
    header{{padding:12px 16px;background:#111827;border-bottom:1px solid #334155}}
    #status{{margin-top:6px;font-size:13px;color:#bae6fd;word-break:break-all}}
    #arkose-container{{width:100%;min-height:700px;display:flex;justify-content:center;align-items:flex-start;padding-top:18px;background:#f8fafc;box-sizing:border-box}}
    #events{{margin:0;padding:10px 16px;max-height:170px;overflow:auto;background:#020617;color:#cbd5e1;font-size:12px}}
    .hint{{font-size:12px;color:#fef3c7;margin-top:4px}}
  </style>
</head>
<body>
  <header>
    <strong>Arkose manual worker - RuyiPage Firefox same browser tab</strong>
    <div class="hint">请在本标签完成验证；完成后脚本会自动切回原注册标签注入 token。</div>
    <div id="status">loading Arkose Client API...</div>
  </header>
  <div id="arkose-container"></div>
  <pre id="events"></pre>
  <script>
  (() => {{
    const cfg = {cfg};
    const state = window.__ARKOSE_MANUAL__ = {{
      apiUrl: cfg.apiUrl, apiReady: false, runCalled: false, token: null,
      tokenLength: 0, status: 'boot', error: null, completedPayload: null, events: []
    }};
    let enforcement = null;
    function safe(v) {{ try {{ return JSON.parse(JSON.stringify(v)); }} catch (_) {{ return String(v); }} }}
    function emit(name, payload) {{
      const ev = {{name, at: Date.now(), payload: safe(payload)}};
      state.events.push(ev); state.status = name;
      const s = document.getElementById('status');
      if (s) s.textContent = name + (payload ? ': ' + JSON.stringify(safe(payload)).slice(0, 320) : '');
      const out = document.getElementById('events');
      if (out) out.textContent = state.events.slice(-24)
        .map(e => new Date(e.at).toLocaleTimeString() + ' ' + e.name + (e.payload ? ' ' + JSON.stringify(e.payload).slice(0, 180) : ''))
        .join('\\n');
      console.log('[ARKOSE-RUYI]', name, payload || '');
    }}
    function runIt() {{
      if (!enforcement) {{ emit('run-before-ready', null); return; }}
      try {{ state.runCalled = true; enforcement.run(); emit('run', null); }}
      catch (e) {{ state.error = String(e && (e.stack || e.message) || e); emit('run-error', state.error); }}
    }}
    window.setupEnforcement = function(myEnforcement) {{
      enforcement = myEnforcement; state.apiReady = true;
      emit('api-ready', {{publicKey: cfg.publicKey, hasBlob: !!cfg.blob, apiUrl: cfg.apiUrl}});
      try {{
        myEnforcement.setConfig({{
          publicKey: cfg.publicKey,
          selector: '#arkose-container',
          mode: 'inline',
          language: cfg.language,
          data: cfg.blob ? {{blob: cfg.blob}} : {{}},
          onReady: r => {{ emit('onReady', r); if (!state.runCalled) setTimeout(runIt, 100); }},
          onShow: r => emit('onShow', r),
          onShown: r => emit('onShown', r),
          onHide: r => emit('onHide', r),
          onReset: r => {{ state.runCalled = false; emit('onReset', r); }},
          onWarning: r => emit('onWarning', r),
          onFailed: r => {{ state.error = safe(r); emit('onFailed', r); }},
          onError: r => {{ state.error = safe(r); emit('onError', r); }},
          onCompleted: r => {{
            state.completedPayload = safe(r);
            state.token = r && r.token ? String(r.token) : null;
            state.tokenLength = state.token ? state.token.length : 0;
            emit('onCompleted', {{tokenLength: state.tokenLength}});
          }}
        }});
      }} catch (e) {{ state.error = String(e && (e.stack || e.message) || e); emit('setConfig-error', state.error); }}
    }};
    const script = document.createElement('script');
    script.id = 'arkose-client-api';
    script.src = cfg.apiUrl;
    script.async = true;
    script.defer = true;
    script.setAttribute('data-callback', 'setupEnforcement');
    script.onload = () => emit('script-loaded', cfg.apiUrl);
    script.onerror = e => {{ state.error = 'api.js load failed'; emit('script-error', String(e)); }};
    document.head.appendChild(script);
    emit('script-added', cfg.apiUrl);
  }})();
  </script>
</body>
</html>"""


def decode_bidi_body_value(value: Any) -> str:
    """Decode Firefox BiDi request/response body formats to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if not isinstance(value, dict):
        return str(value)

    # network.getData result commonly contains {"bytes": {"type":"string","value":"..."}}
    for key in ("bytes", "base64"):
        if key not in value:
            continue
        child = value.get(key)
        if key == "base64":
            raw = child.get("value") if isinstance(child, dict) else child
            if raw:
                with contextlib.suppress(Exception):
                    return base64.b64decode(raw).decode("utf-8", errors="replace")
        else:
            text = decode_bidi_body_value(child)
            if text:
                return text

    typ = value.get("type")
    val = value.get("value")
    if typ == "base64" and val:
        with contextlib.suppress(Exception):
            return base64.b64decode(val).decode("utf-8", errors="replace")
    if val is not None:
        return str(val)
    return ""


@dataclass
class RuyiArkoseCatcher:
    page: Any
    captured_blob: Optional[str] = None
    captured_pk: Optional[str] = None
    fc_requests: list[str] = None
    ca_requests: list[dict] = None
    _driver: Any = None
    _subscription_id: Optional[str] = None
    _collector_id: Optional[str] = None
    _pending: Dict[str, Dict[str, Any]] = None
    _handled: set[str] = None
    _lock: Lock = None

    def __post_init__(self) -> None:
        self.fc_requests = []
        self.ca_requests = []
        self._pending = {}
        self._handled = set()
        self._lock = Lock()

    def start(self) -> None:
        # 被动抓包，Firefox BiDi，不阻断请求。
        #
        # 不使用 page.capture.start()，因为它默认只订阅当前 browsing context；
        # Arkose 的 /fc/gt2/ 往往发生在后创建的跨域 iframe context 里。
        # 这里直接用全局 session.subscribe(contexts=None) 覆盖所有 tab/frame。
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        self._driver = self.page._driver._browser_driver
        with contextlib.suppress(Exception):
            result = bidi_network.add_data_collector(
                self._driver,
                events=["beforeRequestSent", "responseCompleted"],
                contexts=None,
                data_types=["request", "response"],
                max_encoded_data_size=2 * 1024 * 1024,
            )
            self._collector_id = result.get("collector")
        result = bidi_session.subscribe(
            self._driver,
            ["network.beforeRequestSent", "network.responseCompleted", "network.fetchError"],
            contexts=None,
        )
        self._subscription_id = result.get("subscription")
        self._driver.set_callback("network.beforeRequestSent", self._on_request, context=None)
        self._driver.set_callback("network.responseCompleted", self._on_response, context=None)
        self._driver.set_callback("network.fetchError", self._on_fetch_error, context=None)
        LOG.info(
            "RuyiPage global BiDi capture started for /fc/gt2/ and /fc/ca/ (collector=%s)",
            bool(self._collector_id),
        )

    def stop(self) -> None:
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        with contextlib.suppress(Exception):
            if self._driver:
                self._driver.remove_callback("network.beforeRequestSent", context=None)
                self._driver.remove_callback("network.responseCompleted", context=None)
                self._driver.remove_callback("network.fetchError", context=None)
        with contextlib.suppress(Exception):
            if self._driver and self._subscription_id:
                bidi_session.unsubscribe(self._driver, subscription=self._subscription_id)
        with contextlib.suppress(Exception):
            if self._driver and self._collector_id:
                bidi_network.remove_data_collector(self._driver, self._collector_id)

    def _on_request(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = req.get("url", "") or ""
        if "/fc/gt2/" not in url and "/fc/ca/" not in url:
            return
        rid = req.get("request", "") or ""
        if rid:
            with self._lock:
                self._pending[rid] = {"url": url, "request": req, "params": params, "at": time.time()}
        self._handle_request(url, rid, req, request_body=decode_bidi_body_value(req.get("body")))

    def _on_response(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = req.get("url", "") or ""
        if "/fc/gt2/" not in url and "/fc/ca/" not in url:
            return
        rid = req.get("request", "") or ""
        if rid:
            with self._lock:
                old = self._pending.setdefault(rid, {"url": url, "request": req, "at": time.time()})
                old["response"] = params.get("response", {}) or {}
        self._try_collect_body(rid)

    def _on_fetch_error(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = req.get("url", "") or ""
        if "/fc/gt2/" in url or "/fc/ca/" in url:
            LOG.warning("RuyiPage network fetchError: %s", url[:180])

    def _get_collected_data(self, request_id: str, data_type: str) -> str:
        if not self._driver or not self._collector_id or not request_id:
            return ""
        from ruyipage._bidi import network as bidi_network

        try:
            raw = bidi_network.get_data(self._driver, self._collector_id, request_id, data_type=data_type)
            return decode_bidi_body_value(raw)
        except Exception:
            return ""

    def _try_collect_body(self, request_id: str) -> None:
        if not request_id:
            return
        with self._lock:
            item = dict(self._pending.get(request_id) or {})
        if not item:
            return
        req = item.get("request") or {}
        url = item.get("url") or req.get("url", "")
        body = decode_bidi_body_value(req.get("body"))
        if not body:
            body = self._get_collected_data(request_id, "request")
        self._handle_request(url, request_id, req, request_body=body)

    def _handle_request(self, url: str, request_id: str, request: Dict[str, Any], request_body: str = "") -> None:
        if "/fc/gt2/" in url and url not in self.fc_requests:
            self.fc_requests.append(url)
        m = re.search(r"/fc/gt2/public_key/([0-9A-F-]+)", url, re.I)
        if m:
            self.captured_pk = m.group(1)

        body = request_body or ""
        if not body:
            return

        if "/fc/gt2/" in url:
            blob = extract_blob_from_body(body)
            if blob and blob != self.captured_blob:
                self.captured_blob = blob
                LOG.info("RuyiPage captured blob: len=%s pk=%s", len(blob), self.captured_pk)
        elif "/fc/ca/" in url:
            fp = f"{request_id}:{len(body)}"
            if fp in self._handled:
                return
            self._handled.add(fp)
            rec = {
                "url": url,
                "requestBody": body[:8000],
                "status": (request or {}).get("status", 0),
            }
            self.ca_requests.append(rec)
            LOG.info("RuyiPage captured /fc/ca/: count=%s status=%s", len(self.ca_requests), rec.get("status"))

    def pump(self, timeout: float = 0.2) -> None:
        time.sleep(max(0.0, min(timeout, 0.5)))
        with self._lock:
            ids = list(self._pending.keys())
        for rid in ids:
            self._try_collect_body(rid)
        if not self.captured_blob:
            blob = try_extract_blob_from_runtime(self.page)
            if blob and blob != self.captured_blob:
                self.captured_blob = blob
                LOG.info("RuyiPage runtime extracted blob: len=%s", len(blob))

    def wait_for_blob(self, timeout: float = 30.0) -> Optional[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(timeout=0.5)
            if self.captured_blob:
                return self.captured_blob
        return self.captured_blob


def extract_blob_from_body(body: str) -> Optional[str]:
    if not body:
        return None
    # request_body 可能是原始 form，也可能已经做过部分解码。
    for text in (body, unquote(body)):
        try:
            qs = parse_qs(text, keep_blank_values=True)
            for key in ("data[blob]", "blob", "bda"):
                if qs.get(key):
                    return qs[key][0]
        except Exception:
            pass
        m = re.search(r"(?:^|&)(?:data(?:%5B|\[)blob(?:%5D|\])|blob|bda)=([^&]+)", text, re.I)
        if m:
            return unquote(m.group(1))
    return None


def try_extract_blob_from_runtime(page) -> Optional[str]:
    """Best-effort blob extraction from DOM/runtime attributes.

    CDP 版从 /fc/gt2/ POST body 抓 blob。Firefox BiDi 有时抓不到子 frame 的
    request body，所以这里补一个 DOM 兜底：扫描 capture input / scripts /
    iframes 里可能出现的 data[blob]、blob、bda 参数。
    """
    js = r"""return (() => {
      const out = [];
      const push = v => { if (v && typeof v === 'string') out.push(v); };
      const cap = document.querySelector('#capture-arkose, input[name="arkose"]');
      if (cap) {
        ['data-arkose-src','data-exchange-data','data-arkose-data','value'].forEach(k => push(cap.getAttribute(k) || cap[k] || ''));
      }
      document.querySelectorAll('script[src],iframe[src],input').forEach(el => {
        ['src','data-arkose-src','data-exchange-data','data-arkose-data','value'].forEach(k => push(el.getAttribute(k) || el[k] || ''));
      });
      return Array.from(new Set(out)).slice(0, 80);
    })();"""
    for ctx in all_contexts(page):
        with contextlib.suppress(Exception):
            vals = ctx.run_js(js, timeout=3) or []
            for val in vals:
                blob = extract_blob_from_body(str(val))
                if blob:
                    return blob
    return None


def detect_arkose_context(page, catcher: Optional[RuyiArkoseCatcher]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "found": False,
        "siteKey": catcher.captured_pk if catcher else None,
        "surl": None,
        "websiteURL": page.url,
        "siteOrigin": origin_from_url(page.url),
        "userAgent": None,
        "candidateURLs": [],
        "dataArkoseSrc": None,
    }
    with contextlib.suppress(Exception):
        result["userAgent"] = page.user_agent

    js = """return (() => {
      const candidates = [];
      const capture = document.querySelector('#capture-arkose');
      if (capture) {
        const src = capture.getAttribute('data-arkose-src') || '';
        if (src) candidates.push(src);
      }
      document.querySelectorAll('script[src], iframe[src]').forEach(el => {
        const src = el.src || el.getAttribute('src') || '';
        if (/arkoselabs|funcaptcha/i.test(src)) candidates.push(src);
      });
      return {
        hasCaptureInput: !!capture,
        dataArkoseSrc: capture ? (capture.getAttribute('data-arkose-src') || '') : '',
        candidates: Array.from(new Set(candidates))
      };
    })();"""
    for ctx in all_contexts(page):
        with contextlib.suppress(Exception):
            dom = ctx.run_js(js, timeout=5)
            if dom:
                result["found"] = bool(result["found"] or dom.get("hasCaptureInput") or dom.get("candidates"))
                if dom.get("dataArkoseSrc"):
                    result["dataArkoseSrc"] = dom.get("dataArkoseSrc")
                result["candidateURLs"].extend(dom.get("candidates") or [])
        with contextlib.suppress(Exception):
            url = ctx.url or ""
            if re.search(r"arkoselabs|funcaptcha", url, re.I):
                result["candidateURLs"].append(url)
                result["found"] = True

    for candidate in list(dict.fromkeys(result["candidateURLs"])):
        if not result.get("siteKey"):
            m = re.search(r"(?:/v\d+/|[?&#]pk=|#)([0-9A-F]{8}-[0-9A-F-]{27,})", candidate, re.I)
            if m:
                result["siteKey"] = m.group(1)
        if not result.get("surl"):
            with contextlib.suppress(Exception):
                host = urlsplit(candidate if "://" in candidate else "https:" + candidate).netloc
                if re.search(r"arkoselabs\.com$|funcaptcha\.com$", host, re.I):
                    result["surl"] = host

    result["candidateURLs"] = list(dict.fromkeys(result["candidateURLs"]))
    result["surl"] = normalize_surl(result.get("surl"))
    if result.get("siteKey"):
        result["found"] = True
    return result


def click_arkose_verify(page, timeout: float = 25.0) -> bool:
    js_click = r"""return (() => {
      const seen = new Set();
      const roots = [document];
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      for (let i = 0; i < roots.length; i++) {
        const root = roots[i];
        if (!root || seen.has(root)) continue;
        seen.add(root);
        try {
          root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) roots.push(el.shadowRoot); });
        } catch(e) {}
        let candidates = [];
        try {
          candidates = candidates.concat(Array.from(root.querySelectorAll(
            'button[data-theme="home.verifyButton"], button[aria-label="Verify"], button[aria-label="验证"], button'
          )));
        } catch(e) {}
        for (const el of candidates) {
          const txt = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('data-theme') || '')).toLowerCase();
          if (visible(el) && (/verify|human|验证|人类|home\.verifybutton/i.test(txt))) {
            el.click();
            return {ok:true, text: txt.slice(0,120)};
          }
        }
      }
      return {ok:false};
    })();"""
    selectors = [
        'button[data-theme="home.verifyButton"]',
        'button[aria-label="Verify"]',
        'button[aria-label="验证"]',
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ctx in all_contexts(page):
            with contextlib.suppress(Exception):
                result = ctx.run_js(js_click, timeout=2)
                if isinstance(result, dict) and result.get("ok"):
                    LOG.info("Clicked Arkose Verify by JS: %s", result)
                    return True
            for sel in selectors:
                with contextlib.suppress(Exception):
                    ele = ctx.ele(sel, timeout=0.2)
                    if ele and ele.is_displayed:
                        ele.click()
                        return True
            with contextlib.suppress(Exception):
                for btn in ctx.eles("button", timeout=0.2):
                    text = ((btn.text or "") + " " + (btn.attr("aria-label") or "")).lower()
                    if any(x in text for x in ("verify", "i am a human", "验证", "我是人类")) and btn.is_displayed:
                        btn.click()
                        return True
        time.sleep(0.5)
    return False


def replace_document_under_origin(page, website_url: str, html: str) -> Dict[str, Any]:
    expected_origin = origin_from_url(website_url)
    nav_error = ""
    try:
        page.get(expected_origin + "/", wait="interactive", timeout=30)
    except Exception as exc:
        nav_error = f"{type(exc).__name__}: {exc}"
    with contextlib.suppress(Exception):
        page.stop_loading()
    actual = page.run_js("return {url: location.href, origin: location.origin};", timeout=10)
    if actual.get("origin") != expected_origin:
        raise RuntimeError(f"origin mismatch: expected={expected_origin}, actual={actual}, nav_error={nav_error}")
    page.run_js(
        """function(html) {
          window.stop();
          document.open();
          document.write(html);
          document.close();
        }""",
        html,
        timeout=15,
    )
    return {"expectedOrigin": expected_origin, "beforeReplace": actual, "navigationError": nav_error}


def drive_original_to_battletag(page, acc: Dict[str, str], out: Path) -> None:
    LOG.info("Open URL: %s", REGISTER_URL)
    page.get(REGISTER_URL, wait="interactive", timeout=60)
    time.sleep(2)
    close_cookie_banner_ruyi(page)

    LOG.info("Step 1 email: %s", acc["email"])
    wait_ele(page, "#accountName", "email input", timeout=25).input(acc["email"])
    click_ele(page, "#submit", "email submit")
    time.sleep(2)

    wait_or_refresh(page, "#capture-country", "country selector")
    LOG.info("Reload page to ensure full registration form")
    page.refresh()
    time.sleep(3)
    wait_or_refresh(page, "#capture-country", "country selector after reload")

    LOG.info("Step 2 country: %s", COUNTRY)
    # RuyiPage 的 native select.by_value 在这个页面上容易只打开下拉框、不稳定改值；
    # 这里改用和 Playwright 版一致的 DOM setter + input/change 事件。
    country_result = set_input_value_js(page, "#capture-country", COUNTRY, input_type="select")
    LOG.info("Country set result: %s", country_result)
    if not country_result.get("ok") or country_result.get("value") != COUNTRY:
        raise RuntimeError(f"country set failed: {country_result}")
    time.sleep(1.5)

    close_cookie_banner_ruyi(page)
    LOG.info("Step 3 birthday: %s-%s-%s", acc["birth_year"], acc["birth_month"], acc["birth_day"])
    fill_birthday_ruyi(page, acc)
    time.sleep(0.5)
    click_ele(page, "#flow-form-submit-btn", "birthday submit")
    time.sleep(2)

    LOG.info("Step 4 name: %s %s", acc["first_name"], acc["last_name"])
    try:
        wait_ele(page, "#capture-first-name", "first name", timeout=8)
    except Exception:
        page.refresh()
        time.sleep(5)
        wait_ele(page, "#capture-first-name", "first name after refresh", timeout=10)
    wait_ele(page, "#capture-first-name", "first name").input(acc["first_name"])
    wait_ele(page, "#capture-last-name", "last name").input(acc["last_name"])
    time.sleep(0.5)
    click_ele(page, "#flow-form-submit-btn", "name submit")
    time.sleep(2)

    LOG.info("Step 5 email confirmation")
    actual_email = elem_value(page, "#capture-email")
    if actual_email != acc["email"]:
        screenshot(page, out / "original_screenshots" / "error_email_mismatch.png")
        raise RuntimeError(f"email mismatch actual={actual_email} expected={acc['email']}")
    click_ele(page, "#flow-form-submit-btn", "email confirmation submit")
    time.sleep(2)

    LOG.info("Step 6 legal checkboxes")
    legal_result = page.run_js(
        """return (() => {
          ['#capture-opt-in-blizzard-news-special-offers','#legal-checkboxes > label > input.step__checkbox'].forEach(sel => {
            const el = document.querySelector(sel);
            if (el && !el.checked) {
              const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'checked').set;
              s.call(el, true);
              el.dispatchEvent(new Event('change', {bubbles:true}));
              el.dispatchEvent(new Event('input', {bubbles:true}));
            }
          });
          return Array.from(document.querySelectorAll('#capture-opt-in-blizzard-news-special-offers,#legal-checkboxes > label > input.step__checkbox'))
            .map(el => ({id: el.id || null, name: el.name || null, checked: !!el.checked}));
        })();""",
        timeout=10,
    )
    LOG.info("Legal checkbox result: %s", legal_result)
    if legal_result and not all(x.get("checked") for x in legal_result):
        LOG.warning("Some legal checkboxes are still unchecked: %s", legal_result)
    time.sleep(0.5)
    click_ele(page, "#flow-form-submit-btn", "legal submit")
    time.sleep(2)

    LOG.info("Step 7 password")
    wait_or_refresh(page, "#capture-password", "password field")
    wait_ele(page, "#capture-password", "password field").input(acc["password"])
    time.sleep(0.5)
    click_ele(page, "#flow-form-submit-btn", "password submit")
    time.sleep(2)

    LOG.info("Step 8 BattleTag: %s", acc["battle_tag"])
    wait_or_refresh(page, "#capture-battletag", "BattleTag field")
    page.run_js(
        """function(val) {
          const el = document.querySelector('#capture-battletag');
          const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          s.call(el, val);
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        acc["battle_tag"],
        timeout=10,
    )
    time.sleep(0.5)
    deadline = time.time() + 5
    while time.time() < deadline:
        ok = page.run_js(
            "return (() => { const btn = document.querySelector('#flow-form-submit-btn'); return !!(btn && !btn.disabled); })();",
            timeout=5,
        )
        if ok:
            break
        time.sleep(0.25)
    screenshot(page, out / "original_screenshots" / "before_battletag_submit.png")


def wait_for_solver_token(page, timeout: float, out: Path) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_status = None
    last_log_at = 0.0
    while time.time() < deadline:
        try:
            state = page.run_js(
                """return (() => {
                  const s = window.__ARKOSE_MANUAL__ || {};
                  return {
                    status: s.status || null,
                    token: s.token || null,
                    tokenLength: s.tokenLength || (s.token ? String(s.token).length : 0),
                    error: s.error || null,
                    events: (s.events || []).slice(-12)
                  };
                })();""",
                timeout=5,
            )
            status = state.get("status") if state else None
            if status != last_status or time.time() - last_log_at >= 5:
                LOG.info("Solver tab status: %s%s", status, f", tokenLength={state.get('tokenLength')}" if state and state.get("token") else "")
                last_status = status
                last_log_at = time.time()
                if state:
                    write_json(out / "solver_state_latest.json", {k: v for k, v in state.items() if k != "token"})
            token = state.get("token") if state else None
            if token:
                result = {"ok": True, "token": token, "tokenLength": len(token), "status": status, "events": state.get("events") or []}
                write_json(out / "solver_result.json", result)
                return result
        except Exception as exc:
            if time.time() - last_log_at >= 5:
                LOG.warning("Solver tab poll failed: %s: %s", type(exc).__name__, exc)
                last_log_at = time.time()
        time.sleep(0.5)
    result = {"ok": False, "error": f"timeout waiting for solver token after {timeout:.0f}s"}
    write_json(out / "solver_result.json", result)
    return result


def inject_token_to_original(page, token: str) -> Dict[str, Any]:
    with contextlib.suppress(Exception):
        page.activate()
    return page.run_js(
        r"""function(token) {
          const form =
            document.querySelector('form#flow-form') ||
            document.querySelector('form[action*="captcha-gate"]') ||
            document.querySelector('form');
          if (!form) return {ok:false, reason:'no-form'};
          let arkose =
            form.querySelector('input[name="arkose"]') ||
            document.querySelector('#capture-arkose') ||
            document.querySelector('input[name="arkose"]');
          if (!arkose) return {ok:false, reason:'no-arkose-input'};
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(arkose, token);
          else arkose.value = token;
          arkose.dispatchEvent(new Event('input', {bubbles:true}));
          arkose.dispatchEvent(new Event('change', {bubbles:true}));
          arkose.dispatchEvent(new Event('blur', {bubbles:true}));
          const submitBtn = document.querySelector('#flow-form-submit-btn') || form.querySelector('button[type="submit"], input[type="submit"]');
          let method = 'none';
          try {
            if (typeof form.requestSubmit === 'function') {
              if (submitBtn && !submitBtn.disabled) {
                form.requestSubmit(submitBtn);
                method = 'requestSubmit(button)';
              } else {
                form.requestSubmit();
                method = 'requestSubmit';
              }
            } else if (submitBtn && !submitBtn.disabled) {
              submitBtn.click();
              method = 'button.click';
            } else {
              form.submit();
              method = 'form.submit';
            }
          } catch (e) {
            return {ok:false, reason:'submit-error:' + (e && e.message || e), tokenSet: arkose.value === token, tokenLength: token.length};
          }
          return {
            ok:true, method, tokenSet: arkose.value === token, tokenLength: token.length,
            inputId: arkose.id || null, inputName: arkose.name || null,
            formId: form.id || null, formAction: form.action || null
          };
        }""",
        token,
        timeout=15,
    )


def captcha_text(page) -> str:
    parts = []
    for ctx in all_contexts(page):
        with contextlib.suppress(Exception):
            text = ctx.run_js("return document.body ? (document.body.innerText || '') : '';", timeout=3)
            if text:
                parts.append(text)
    return "\n".join(parts)


def is_registration_success(page, expected_email: Optional[str] = None) -> bool:
    expected_email = (expected_email or "").strip().lower()
    js = r"""return ((expectedEmail) => {
      const text = document.body ? (document.body.innerText || '') : '';
      const lower = text.toLowerCase();
      const hasIcon = !!document.querySelector('#success-icon > svg > path, #success-icon, [data-testid*="success"], [class*="success"] svg');
      const hasAllSet = /you['’]?\s*re\s+all\s+set|all\s+set/i.test(text);
      const hasCreated = /account\s+has\s+been\s+created|has\s+been\s+created/i.test(text);
      const hasDownloadApp = /download\s+battle\.net\s+app/i.test(text);
      const hasEmail = expectedEmail ? lower.includes(String(expectedEmail).toLowerCase()) : true;
      const strongSuccess = hasAllSet && (hasCreated || hasDownloadApp);
      return {success: !!(hasIcon || (strongSuccess && hasEmail) || (strongSuccess && !expectedEmail)), sample: text.slice(0, 260)};
    })(arguments[0]);"""
    for ctx in all_contexts(page):
        with contextlib.suppress(Exception):
            result = ctx.run_js(js, expected_email, timeout=5)
            if isinstance(result, dict) and result.get("success"):
                LOG.info("检测到注册成功页: %s", result)
                return True
    return False


def captcha_state(page) -> str:
    text = captcha_text(page).lower()
    if "not quite right" in text or "try again" in text or "invalid" in text or "incorrect" in text or "无效" in text:
        return "rejected"
    if "verify" in text or "i am a human" in text or "submit" in text or "验证" in text:
        return "active"
    return "gone"


def wait_registration_success(page, expected_email: Optional[str], timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_registration_success(page, expected_email):
            return True
        if captcha_state(page) == "rejected":
            LOG.warning("Arkose token 被页面拒绝，提前停止等待注册成功")
            return False
        time.sleep(0.5)
    return False


def verify_browser_egress(page, out: Path, proxy_url: Optional[str]) -> Dict[str, Any]:
    result = {"checked": False, "proxy": proxy_url, "ok": False, "url": IPIFY_URL}
    tab = None
    try:
        tab = page.new_tab(IPIFY_URL, background=False)
        time.sleep(2)
        text = tab.ele("tag:body", timeout=10).text.strip()
        parsed = json.loads(text)
        result.update({"checked": True, "ok": True, "ip": parsed.get("ip")})
    except Exception as exc:
        result.update({"checked": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        with contextlib.suppress(Exception):
            if tab:
                tab.close()
        with contextlib.suppress(Exception):
            page.activate()
    write_json(out / "network_route.json", result)
    if result.get("ok"):
        LOG.info("Browser egress checked: ip=%s", result.get("ip"))
    else:
        LOG.warning("Browser egress check failed: %s", result.get("error"))
    return result


def choose_network_mode(args: argparse.Namespace) -> int:
    if args.network_mode is not None:
        return int(args.network_mode)
    print("\n请选择网络方案：")
    print("1. 不启动 isolated-proxy-browser，使用本机默认网络/系统代理")
    print("2. 启动 isolated-proxy-browser，测速并选择一个节点；当前 RuyiPage Firefox 所有标签共同走该节点")
    while True:
        raw = input("请输入 1 或 2，直接回车默认 1：").strip()
        if not raw:
            return 1
        if raw in ("1", "2"):
            return int(raw)
        print("请输入 1 或 2。")


def launch_ruyi_browser(args: argparse.Namespace, proxy_url: Optional[str]):
    LOG.info("Launching RuyiPage Firefox: headless=%s proxy=%s", args.headless, proxy_url or "<default>")
    page = ruyipage.launch(
        headless=bool(args.headless),
        proxy=proxy_url,
        window_size=(1920, 1080),
        timeout_page_load=60,
        timeout_script=60,
        close_on_exit=True,
        failure_snapshot=True,
        snapshot_dir=str(Path(args.output_dir or DEFAULT_OUTPUT_ROOT) / "_ruyi_failure_snapshots"),
    )
    with contextlib.suppress(Exception):
        page.set_bypass_csp(True)
    return page


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="RuyiPage Firefox same-browser manual FunCaptcha registration experiment.")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT), help="运行输出目录")
    ap.add_argument("--headless", action="store_true", help="使用 headless；手动求解一般不要开")
    ap.add_argument("--keep-open", action="store_true", help="结束后保持浏览器打开，按 Enter 后关闭")
    ap.add_argument("--manual-timeout", type=float, default=300.0, help="等待手动求解 token 的秒数")
    ap.add_argument("--blob-timeout", type=float, default=45.0, help="等待原标签 blob 的秒数")
    ap.add_argument("--success-timeout", type=float, default=45.0, help="注入 token 后等待注册成功的秒数")
    ap.add_argument("--click-original-verify", action="store_true", help="提交 BattleTag 后也点击原标签 Verify；默认只在 blob 没抓到时兜底点击")
    ap.add_argument("--skip-egress-check", action="store_true", help="不打开 ipify 检查浏览器出口")
    ap.add_argument("--shared-proxy", "--solver-proxy", dest="shared_proxy", help="显式给当前 Firefox 设置代理，例如 http://127.0.0.1:7890")
    ap.add_argument("--network-mode", type=int, choices=(1, 2), help="1=本机默认网络/代理，2=isolated-proxy-browser 节点")
    ap.add_argument("--isolated-root", default=str(DEFAULT_ISOLATED_ROOT), help="isolated-proxy-browser 项目根目录")
    ap.add_argument("--proxy-config", help="Mihomo/Clash YAML 配置，默认 isolated-root/config/proxy-config.yaml")
    ap.add_argument("--proxy-core", help="mihomo.exe / clash-meta.exe 路径，默认 isolated-root/bin/mihomo.exe")
    ap.add_argument("--proxy-node-index", type=int, help="固定节点编号；不传则测速后手动选择")
    ap.add_argument("--proxy-timeout-ms", type=int, default=6000)
    ap.add_argument("--proxy-workers", type=int, default=8)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.output_dir) / run_id()
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(out / "run.log")

    mode = choose_network_mode(args)
    proxy_route: Optional[IsolatedProxyRoute] = None
    proxy_url = args.shared_proxy
    proxy_info: Dict[str, Any] = {"mode": "default", "proxyURL": proxy_url}

    if mode == 2:
        proxy_route = IsolatedProxyRoute(
            project_dir=Path(args.isolated_root),
            config_path=Path(args.proxy_config) if args.proxy_config else None,
            core_path=Path(args.proxy_core) if args.proxy_core else None,
            timeout_ms=args.proxy_timeout_ms,
            workers=args.proxy_workers,
            node_index=args.proxy_node_index,
            evidence_dir=out / "proxy_evidence",
            require_explicit_choice=True,
        )
        proxy_info = proxy_route.start()
        proxy_url = proxy_info.get("proxyURL")
    else:
        LOG.info("Network mode 1: use local default network/system proxy")
        if proxy_url:
            LOG.info("Explicit proxy for RuyiPage Firefox: %s", proxy_url)

    LOG.info("输出目录: %s", out.resolve())
    LOG.info("架构: RuyiPage 原注册标签 -> 同浏览器新求解标签 -> token -> 回原标签注入")

    page = None
    solver_tab = None
    catcher = None
    try:
        acc = generate_identity()
        write_json(out / "account_generated.json", acc)
        LOG.info("账号: %s", acc["email"])
        LOG.info("BattleTag: %s", acc["battle_tag"])

        page = launch_ruyi_browser(args, proxy_url)
        LOG.info("RuyiPage version=%s, UA=%s", getattr(ruyipage, "__version__", "?"), page.user_agent)

        if not args.skip_egress_check:
            verify_browser_egress(page, out, proxy_url)

        drive_original_to_battletag(page, acc, out)

        catcher = RuyiArkoseCatcher(page)
        catcher.start()

        LOG.info("Submit BattleTag to trigger FunCaptcha")
        click_ele(page, "#flow-form-submit-btn", "BattleTag submit")
        time.sleep(2)
        screenshot(page, out / "original_screenshots" / "after_battletag_submit.png")

        blob = catcher.wait_for_blob(timeout=min(15.0, args.blob_timeout))
        if args.click_original_verify or not blob:
            LOG.info("Click original Arkose Verify%s", " (forced)" if args.click_original_verify else " because blob was not captured yet")
            clicked = click_arkose_verify(page, timeout=25)
            write_json(out / "original_verify_click.json", {"clicked": clicked, "forced": bool(args.click_original_verify)})
            time.sleep(1.5)
            screenshot(page, out / "original_screenshots" / "after_original_verify_click.png")
            if not blob:
                blob = catcher.wait_for_blob(timeout=args.blob_timeout)

        blob = blob or catcher.captured_blob
        if not blob:
            raise RuntimeError("no Arkose blob captured from original tab through RuyiPage capture")

        ctx = detect_arkose_context(page, catcher)
        if not ctx.get("siteKey"):
            raise RuntimeError("Arkose public key not detected")

        public_ctx = {**ctx, "blobLength": len(blob), "hasBlob": True}
        write_json(out / "original_arkose_context.json", public_ctx)
        write_json(
            out / "solver_task.json",
            {
                "websiteURL": ctx.get("websiteURL"),
                "websitePublicKey": ctx.get("siteKey"),
                "funcaptchaApiJSSubdomain": ctx.get("surl"),
                "userAgent": ctx.get("userAgent"),
                "data": {"blob": blob},
                "blobLength": len(blob),
                "mode": "ruyipage-same-browser-new-tab",
            },
        )
        LOG.info("Captured Arkose context: pk=%s, surl=%s, blob_len=%s", ctx.get("siteKey"), ctx.get("surl"), len(blob))

        LOG.info("Open solver tab in the SAME RuyiPage Firefox browser")
        solver_tab = page.new_tab(background=False)
        html = build_solver_harness(str(ctx["siteKey"]), blob, str(ctx.get("surl") or DEFAULT_SURL))
        (out / "solver_harness.html").write_text(html, encoding="utf-8")
        origin_info = replace_document_under_origin(solver_tab, str(ctx["websiteURL"]), html)
        write_json(out / "solver_origin.json", origin_info)
        solver_tab.activate()
        screenshot(solver_tab, out / "solver_screenshots" / "harness_loaded.png")
        LOG.info("请在新标签手动完成 Arkose 验证；原标签不会再操作，直到本标签返回 token")

        solver_result = wait_for_solver_token(solver_tab, args.manual_timeout, out)
        if not solver_result.get("ok"):
            raise TimeoutError(solver_result.get("error") or "solver tab did not return token")
        token = str(solver_result["token"])
        LOG.info("Solver tab returned onCompleted token, length=%s", len(token))

        inject_result = inject_token_to_original(page, token)
        write_json(out / "token_injection_result.json", inject_result)
        LOG.info("Original tab token injection result: %s", inject_result)
        screenshot(page, out / "original_screenshots" / "after_token_injection.png")

        success = wait_registration_success(page, acc["email"], timeout=args.success_timeout)
        reg_result: Dict[str, Any] = {
            "ok": bool(success),
            "email": acc["email"],
            "battleTag": acc["battle_tag"],
            "url": page.url,
        }
        if success:
            screenshot(page, out / "original_screenshots" / "registration_success.png")
            LOG.info("注册成功；RuyiPage same-browser 新标签 token 已被原注册标签接受")
        else:
            reg_result["captchaState"] = captcha_state(page)
            reg_result["sample"] = captcha_text(page).replace("\n", " ")[:300]
            screenshot(page, out / "original_screenshots" / "registration_not_confirmed.png")
        write_json(out / "registration_result.json", reg_result)

        write_json(
            out / "summary.json",
            {
                "ok": bool(success),
                "outputDir": str(out.resolve()),
                "mode": "ruyipage-same-browser-new-tab",
                "networkMode": mode,
                "proxy": proxy_info,
                "siteKey": ctx.get("siteKey"),
                "surl": ctx.get("surl"),
                "blobLength": len(blob),
                "tokenLength": len(token),
                "injectResult": inject_result,
                "registration": reg_result,
                "caRecords": catcher.ca_requests if catcher else [],
            },
        )
        return 0 if success else 1
    except KeyboardInterrupt:
        LOG.warning("收到 Ctrl+C，准备退出")
        write_json(out / "summary.json", {"ok": False, "error": "KeyboardInterrupt", "outputDir": str(out.resolve())})
        return 130
    except Exception as exc:
        LOG.error("Run failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        write_json(out / "summary.json", {"ok": False, "error": f"{type(exc).__name__}: {exc}", "outputDir": str(out.resolve())})
        with contextlib.suppress(Exception):
            if page:
                screenshot(page, out / "original_screenshots" / "error_original_page.png")
        with contextlib.suppress(Exception):
            if solver_tab:
                screenshot(solver_tab, out / "solver_screenshots" / "error_solver_page.png")
        return 1
    finally:
        with contextlib.suppress(Exception):
            if catcher:
                catcher.stop()
        if args.keep_open and page is not None:
            try:
                input("浏览器保持打开。检查完后按 Enter 关闭...")
            except EOFError:
                pass
        with contextlib.suppress(Exception):
            if page:
                page.quit()
        if proxy_route is not None:
            proxy_route.stop()


if __name__ == "__main__":
    raise SystemExit(main())
