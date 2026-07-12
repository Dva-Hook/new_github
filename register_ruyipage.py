# -*- coding: utf-8 -*-
"""Battle.net register runner using RuyiPage Firefox.

This is a thin compatibility runner for ``register.py``:

- form filling and final solve flow still call ``register.register_one()``
- CapMonster/local fallback logic remains in ``register.CapMonsterFunCaptchaSolver``
- only the browser backend is swapped from CloakBrowser/Chromium CDP to
  RuyiPage/Firefox WebDriver BiDi

Firefox does not expose Chrome CDP, so the CDP blob catcher is replaced by a
BiDi network watcher with the same small public surface:
``captured_blob``, ``captured_pk``, ``start()``, ``stop()``, ``reset_blob()``,
``wait_for_blob()``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import sys
import time
from threading import Lock
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote

import ruyipage

import register as base


logger = logging.getLogger("battle_net_ruyipage")
_CURRENT_PAGE: Optional["RuyiPageAdapter"] = None
_NOARG = object()


def _truthy(value: Optional[str], default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _decode_bidi_body_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if not isinstance(value, dict):
        return str(value)

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
            text = _decode_bidi_body_value(child)
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


def _extract_blob_from_body(body: str) -> Optional[str]:
    if not body:
        return None
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


def _run_js_playwright_style(ctx, script: str, arg: Any = _NOARG, timeout: Optional[float] = None):
    """Execute a Playwright-style evaluate string on a RuyiPage context.

    ``register.py`` mostly passes arrow functions such as ``() => {...}`` and
    ``([x]) => {...}``; RuyiPage's ``run_js`` needs either an expression or a
    function declaration.  This wrapper normalizes those forms.
    """
    s = (script or "").strip()
    timeout_s = None if timeout is None else max(1.0, float(timeout))
    if arg is _NOARG:
        if "=>" in s[:160]:
            return ctx.run_js(f"return ({s})();", timeout=timeout_s)
        return ctx.run_js(s, timeout=timeout_s)

    if "=>" in s[:200] or s.startswith("function") or s.startswith("("):
        return ctx.run_js(f"function(__arg) {{ return ({s})(__arg); }}", arg, timeout=timeout_s)
    return ctx.run_js(s, arg, timeout=timeout_s)


class RuyiElementAdapter:
    def __init__(self, raw):
        self._raw = raw

    def click(self, *args, **kwargs):
        by_js = bool(kwargs.get("by_js", False))
        try:
            self._raw.click(by_js=by_js)
        except Exception:
            self._raw.click(by_js=True)
        return self

    def fill(self, value: str):
        self._raw.input(str(value), clear=True)
        return self

    def input_value(self):
        return self._raw.value or self._raw.attr("value") or ""

    def get_attribute(self, name: str):
        return self._raw.attr(name)

    def text_content(self):
        return self._raw.text

    def is_visible(self, timeout=None):
        if timeout:
            end = time.time() + (float(timeout) / 1000.0 if float(timeout) > 100 else float(timeout))
            while time.time() < end:
                with contextlib.suppress(Exception):
                    if bool(self._raw.is_displayed):
                        return True
                time.sleep(0.1)
        with contextlib.suppress(Exception):
            return bool(self._raw.is_displayed)
        return False

    def screenshot(self, path: str, timeout=None):
        return self._raw.screenshot(path=path)


class RuyiLocatorAdapter:
    def __init__(self, owner: "RuyiContextAdapter", selector: str):
        self._owner = owner
        self._selector = selector

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index: int):
        items = self.all()
        if 0 <= index < len(items):
            return items[index]
        # Let ruyipage return NoneElement for compatible no-op behavior.
        return RuyiElementAdapter(self._owner._raw.ele(self._selector, index=index + 1, timeout=0.1))

    def all(self):
        try:
            return [RuyiElementAdapter(e) for e in (self._owner._raw.eles(self._selector, timeout=0.5) or [])]
        except Exception:
            return []

    def count(self):
        return len(self.all())

    def click(self, *args, **kwargs):
        return self.first.click(*args, **kwargs)

    def fill(self, value: str):
        return self.first.fill(value)

    def input_value(self):
        return self.first.input_value()

    def get_attribute(self, name: str):
        return self.first.get_attribute(name)

    def text_content(self):
        return self.first.text_content()

    def is_visible(self, timeout=None):
        return self.first.is_visible(timeout=timeout)


class RuyiContextAdapter:
    def __init__(self, raw):
        self._raw = raw

    @property
    def url(self):
        return self._raw.url

    @property
    def frames(self):
        try:
            return [RuyiFrameAdapter(f) for f in (self._raw.get_all_frames() or [])]
        except Exception:
            return []

    @property
    def child_frames(self):
        return self.frames

    @property
    def main_frame(self):
        return self

    def locator(self, selector: str):
        return RuyiLocatorAdapter(self, selector)

    def evaluate(self, script: str, arg: Any = _NOARG):
        return _run_js_playwright_style(self._raw, script, arg)

    def screenshot(self, path: str = None, full_page: bool = False, timeout=None, **kwargs):
        path = path or kwargs.get("path")
        return self._raw.screenshot(path=path, full_page=full_page)


class RuyiFrameAdapter(RuyiContextAdapter):
    pass


class RuyiPageAdapter(RuyiContextAdapter):
    def goto(self, url: str, wait_until: str = None, timeout: int = None, **kwargs):
        wait = "interactive" if wait_until in (None, "domcontentloaded", "load") else "none"
        timeout_s = None if timeout is None else max(1.0, float(timeout) / 1000.0)
        return self._raw.get(url, wait=wait, timeout=timeout_s)

    def reload(self, wait_until: str = None, timeout: int = None):
        return self._raw.refresh()

    def wait_for_selector(self, selector: str, timeout: int = 30000):
        end = time.time() + max(0.5, float(timeout) / 1000.0)
        while time.time() < end:
            try:
                ele = self._raw.ele(selector, timeout=0.2)
                if ele:
                    return RuyiElementAdapter(ele)
            except Exception:
                pass
            time.sleep(0.15)
        raise TimeoutError(f"wait_for_selector timeout: {selector}")

    def wait_for_function(self, script: str, timeout: int = 30000):
        end = time.time() + max(0.5, float(timeout) / 1000.0)
        last = None
        while time.time() < end:
            try:
                last = self.evaluate(script)
                if last:
                    return last
            except Exception as exc:
                last = exc
            time.sleep(0.15)
        raise TimeoutError(f"wait_for_function timeout: {last}")

    def select_option(self, selector: str, value: str):
        return self.evaluate(
            """([selector, value]) => {
                const el = document.querySelector(selector);
                if (!el) return {ok:false, reason:'not-found'};
                const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return {ok:true, value: el.value};
            }""",
            [selector, value],
        )


class RuyiContextListAdapter:
    def __init__(self, page: RuyiPageAdapter):
        self.pages = [page]

    def new_page(self):
        return self.pages[0]


class RuyiBrowserAdapter:
    def __init__(self, raw, page: RuyiPageAdapter):
        self._raw = raw
        self.contexts = [RuyiContextListAdapter(page)]

    def new_context(self, **kwargs):
        return self.contexts[0]

    def close(self):
        with contextlib.suppress(Exception):
            self._raw.quit()


class RuyiBlobCatcher:
    """CDPBlobCatcher-compatible BiDi blob catcher for RuyiPage Firefox."""

    def __init__(self, debug_port=0, ws_url=None, label=""):
        if _CURRENT_PAGE is None:
            raise RuntimeError("RuyiPage has not been launched yet")
        self.page = _CURRENT_PAGE
        self._raw = _CURRENT_PAGE._raw
        self._label = label or "ruyi"
        self.captured_blob = None
        self.captured_pk = None
        self.fc_requests = []
        self.ca_requests = []
        self._driver = None
        self._collector_id = None
        self._subscription_id = None
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._handled = set()
        self._lock = Lock()

    def start(self):
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        self._driver = self._raw._driver._browser_driver
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
        logger.info(f"[{self._label}] RuyiPage BiDi blob catcher started")

    def stop(self):
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

    def reset_blob(self):
        self.captured_blob = None

    def _on_request(self, params: Dict[str, Any]):
        req = params.get("request", {}) or {}
        url = req.get("url", "") or ""
        if "/fc/gt2/" not in url and "/fc/ca/" not in url:
            return
        rid = req.get("request", "") or ""
        if rid:
            with self._lock:
                self._pending[rid] = {"url": url, "request": req, "at": time.time()}
        self._handle_request(url, rid, req, _decode_bidi_body_value(req.get("body")))

    def _on_response(self, params: Dict[str, Any]):
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

    def _get_collected_data(self, request_id: str, data_type: str) -> str:
        if not self._driver or not self._collector_id or not request_id:
            return ""
        from ruyipage._bidi import network as bidi_network
        try:
            raw = bidi_network.get_data(self._driver, self._collector_id, request_id, data_type=data_type)
            return _decode_bidi_body_value(raw)
        except Exception:
            return ""

    def _try_collect_body(self, rid: str):
        if not rid:
            return
        with self._lock:
            item = dict(self._pending.get(rid) or {})
        if not item:
            return
        req = item.get("request") or {}
        body = _decode_bidi_body_value(req.get("body")) or self._get_collected_data(rid, "request")
        self._handle_request(item.get("url") or req.get("url", ""), rid, req, body)

    def _handle_request(self, url: str, rid: str, req: Dict[str, Any], body: str = ""):
        if "/fc/gt2/" in url and url not in self.fc_requests:
            self.fc_requests.append(url)
        m = re.search(r"/fc/gt2/public_key/([0-9A-F-]+)", url, re.I)
        if m:
            self.captured_pk = m.group(1)
        if "/fc/gt2/" in url and body:
            blob = _extract_blob_from_body(body)
            if blob and blob != self.captured_blob:
                self.captured_blob = blob
                logger.info(f"[{self._label}] RuyiPage captured blob len={len(blob)}, pk={self.captured_pk}")
        elif "/fc/ca/" in url and body:
            fp = f"{rid}:{len(body)}"
            if fp not in self._handled:
                self._handled.add(fp)
                self.ca_requests.append({"url": url, "requestBody": body[:8000]})

    def _pump(self, delay: float = 0.2):
        time.sleep(max(0.0, min(delay, 0.5)))
        with self._lock:
            ids = list(self._pending.keys())
        for rid in ids:
            self._try_collect_body(rid)

    def wait_for_blob(self, timeout: float = 30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._pump(0.4)
            if self.captured_blob:
                return self.captured_blob
        return self.captured_blob


def create_ruyipage_browser():
    global _CURRENT_PAGE
    headless = _truthy(os.environ.get("RUYIPAGE_HEADLESS"), default=True)
    proxy = os.environ.get("RUYIPAGE_PROXY") or None
    raw_page = ruyipage.launch(
        headless=headless,
        proxy=proxy,
        window_size=(1920, 1080),
        timeout_page_load=60,
        timeout_script=60,
        close_on_exit=True,
        failure_snapshot=True,
        snapshot_dir="ruyipage_failure_snapshots",
    )
    page = RuyiPageAdapter(raw_page)
    _CURRENT_PAGE = page
    browser = RuyiBrowserAdapter(raw_page, page)
    context = browser.contexts[0]
    logger.info(
        "RuyiPage Firefox launched: version=%s, headless=%s, proxy=%s, UA=%s",
        getattr(ruyipage, "__version__", "?"),
        headless,
        proxy or "<default>",
        raw_page.user_agent,
    )
    return browser, context, page


def patch_register_module():
    base.create_cloak_browser = create_ruyipage_browser
    base.CDPBlobCatcher = RuyiBlobCatcher
    # Firefox/BiDi replacement keeps CapMonster logic unchanged.  CDP image
    # capture is Chromium-only, so local dice fallback is skipped just like
    # register.py already does when image_catcher is None.
    base.CDPImageCatcher = None


def main():
    if not base.CAPMONSTER_API_KEY:
        logger.warning("CAPMONSTER_API_KEY is empty; RuyiPage workflow needs CapMonster fallback because CDP image capture is unavailable")
    patch_register_module()
    acc = base.generate_identity()
    base.logger.info("=" * 50)
    base.logger.info("Battle.net auto register - RuyiPage Firefox backend")
    base.logger.info(f"   email: {acc['email']}")
    base.logger.info(f"   BattleTag: {acc['battle_tag']}")
    base.logger.info("=" * 50)
    ok = base.register_one(acc)
    base.logger.info(f"\nRegistration finished: {'success' if ok else 'failed'}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
