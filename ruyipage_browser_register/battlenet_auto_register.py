# -*- coding: utf-8 -*-
"""Battle.net 自动注册脚本 V4 - 最终完整版

完整功能：
1. 自动填写注册表单
2. 使用 RuyiPage BiDi 网络监听捕获 Arkose blob
3. 使用 YesCaptcha API 求解验证码
4. 自动提交 token 完成注册

参考项目 V6 完整实现，不修改项目源码
"""

import sys
import time
import os
import random
import string
import json
import re
import contextlib
import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from ruyipage import FirefoxPage, FirefoxOptions
    import requests
except ImportError as e:
    print(f"缺少依赖: {e}")
    print("请安装: pip install ruyipage requests")
    sys.exit(1)


# ==================== 控制台编码 ====================
# 本模块有 42 处 print 直接内嵌 ✓ / ✗ / ⚠️ / ★ / → 这类符号。Windows 控制台默认
# 是 GBK（cp936），这些字符编码不进去，print 会抛 UnicodeEncodeError 并**打断
# 整条流程**——不是打印乱码，是直接崩。已经在 unittest 里真实触发过一次。
#
# 在入口统一把 stdout/stderr 重配成 UTF-8，比逐处改 print 可靠：新增的输出不
# 会再踩这个坑。errors="replace" 兜底，确保任何情况下 print 都不会抛异常——
# 日志掉几个字符远好过整条注册流程中断。
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    _reconf = getattr(_stream, "reconfigure", None)
    if callable(_reconf):
        with contextlib.suppress(Exception):
            _reconf(encoding="utf-8", errors="replace")


# ==================== 配置 ====================
YESCAPTCHA_API_KEY = "0f42a4d546495074fa12c4d7c0b40759c898777e139353"  # 请填入 YesCaptcha API Key
DEFAULT_SITE_KEY = "E8A75615-1CBA-5DFF-8032-D16BCF234E10"
DEFAULT_SURL = "blizzard-api.arkoselabs.com"
YESCAPTCHA_CLASSIFICATION_URL = "https://api.yescaptcha.com/createTask"
YESCAPTCHA_RESULT_URL = "https://api.yescaptcha.com/getTaskResult"
QUESTION_SELECTORS = (
    "#root h2 span[role='text']",
    "#root h2 span",
    "h2 span[role='text']",
    "h2 span",
    "span[role='text']",
)
RUNS_DIR = Path(__file__).resolve().parent / "runs"

# 求解标签页里 Arkose 自己报错（POW_FAILED 等，token 还没产出）时，同一标签
# 最多重开几次。这只是换一个 challenge，不是换 realm，重开超过一次没意义。
MAX_HARNESS_ATTEMPTS = 2
# 服务端拒绝已解出的 token 时，带着当前 blob 重开求解页的次数。Arkose 每次
# 都会换一个 challenge，换一个通常就能被接受。
MAX_TOKEN_ATTEMPTS = 3
# 首选路径是原生求解：在原 captcha-gate 页让站点自己的 api.js 渲染 Arkose，
# 再调 YesCaptcha 解题。实测跨 realm 的 harness 路径 6/6 失败（document.write
# 重置 performance.now() 基线、document.currentScript 与脚本加载时序，正是 PoW
# 校验的东西），原生路径 3 次里过 2 次。harness 只在原生走不通时作回退。
# 原生路径本身也是间歇性的：偶发失败不代表这条路不通，先就地重试几次再谈回退。
MAX_NATIVE_ATTEMPTS = 2
# 原生求解：等 Arkose enforcement 就绪（站点自渲染）的总窗口。
NATIVE_ARKOSE_READY_TIMEOUT = 20.0
# 原生求解：站点 Arkose 回调名读不到时的兜底值（真实页面为 blz-arkose-callback）。
NATIVE_ARKOSE_CALLBACK_FALLBACK = "blz-arkose-callback"
# 新一轮求解开始时，用于判断"题目是否与上一轮相同"的探测窗口（秒）。
REPLAY_PROBE_TIMEOUT = 15.0
# 等待 Arkose blob 期间，每隔这么久检查一次页面是否已经注册成功。
# 站点并不总是先发 Arkose 请求再放行：有时 gate 刚出现、`/fc/gt2/` 还没发出去，
# 服务端就已经判定通过并把页面换成 "You're All Set!"。此时再等 blob 只会白等
# 到超时，最后报"未捕获到 Arkose blob"，把一次成功的注册当成失败。
BLOB_WAIT_SUCCESS_PROBE_INTERVAL = 1.0


# ==================== Arkose Blob 捕获器 ====================

def decode_bidi_body_value(body_value):
    """解码 BiDi body value"""
    if not body_value:
        return ""
    if isinstance(body_value, str):
        return body_value
    if isinstance(body_value, bytes):
        return body_value.decode("utf-8", errors="replace")
    if isinstance(body_value, dict):
        # network.getData 常见形态：{"bytes": {"type": "string", "value": ...}}
        for key in ("bytes", "base64"):
            if key not in body_value:
                continue
            child = body_value.get(key)
            if key == "base64":
                raw = child.get("value") if isinstance(child, dict) else child
                if raw:
                    with contextlib.suppress(Exception):
                        return base64.b64decode(str(raw)).decode("utf-8", errors="replace")
            else:
                text = decode_bidi_body_value(child)
                if text:
                    return text
        encoding = body_value.get("type", "string")
        value = body_value.get("value", "")
        if encoding == "base64":
            with contextlib.suppress(Exception):
                return base64.b64decode(str(value)).decode("utf-8", errors="replace")
        if value is not None:
            return str(value)
    return ""


def extract_blob_from_body(body: str) -> Optional[str]:
    """从请求 body 中提取 blob"""
    if not body:
        return None
    for text in (body, unquote(body)):
        try:
            qs = parse_qs(text, keep_blank_values=True)
            for key in ("data[blob]", "blob", "bda"):
                if qs.get(key):
                    return qs[key][0]
        except:
            pass
        m = re.search(r"(?:^|&)(?:data(?:%5B|\[)blob(?:%5D|\])|blob|bda)=([^&]+)", text, re.I)
        if m:
            return unquote(m.group(1))
    return None


# ==================== KASADA 拦截记录器 ====================
#
# 背景：Battle.net 的 captcha-gate 会按会话分流到 KASADA（data-captcha-provider=
# "KASADA"）。纯 HTTP 协议栈在到达该步骤时只能 `raise UnsupportedCaptchaProvider`
# 退出，因此从来没有真正观察过一次 KASADA 会话的交互——没有 p.js、没有
# ips.js、没有 x-kpsdk-* 头。本记录器用真实 Firefox 跑一遍，把 KASADA 的完整
# 请求/响应证据落盘，为后续判断"能否浏览器化"提供数据。
#
# 记录策略刻意保守：**不修改任何请求**，只旁路记录。KASADA 有代码完整性校验
# 和反调试检测，任何改写都可能触发异常分支，污染观察结果。
#
# 关键待观察项（对应三份文献给出的机制）：
#   - x-kpsdk-cd 从何处下发：响应头 / Cookie(_kpsdk_cd) / HTML 内嵌
#   - x-kpsdk-ct / integrity 何时出现、何时刷新（文献称周期性刷新）
#   - p.js（种子加载器）与其动态拉取的 ips.js（核心 SDK）的 URL 与响应体
#   - 有多少请求携带 KASADA 头（决定浏览器化改造范围）

# 需要落盘的脚本响应体上限。ips.js 通常几百 KB，2 MB 足够。
_KASADA_SCRIPT_MAX_BYTES = 2 * 1024 * 1024


def _kasada_is_candidate_url(url: str) -> bool:
    """判断一个 URL 是否属于 KASADA 观察范围。"""

    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    if any(
        token in low
        for token in (
            "kpsdk",          # x-kpsdk-* 相关端点
            "/p.js",          # 种子加载器
            "ips.js",         # 核心 SDK
            "/fp?",           # 指纹收集（文献第一篇）
            "/149e9513",      # 你的样本里 KASADA 的 resource-address 前缀
        )
    ):
        return True
    # Battle.net 侧的 captcha 端点与遥测
    return any(
        token in low
        for token in (
            "account.battle.net/creation",
            "/creation/api/telemetry/captcha",
        )
    )


def _kasada_is_script_url(url: str) -> bool:
    low = url.lower()
    return low.endswith(".js") or "/p.js" in low or "ips.js" in low


def _header_pairs(headers: Any) -> Dict[str, str]:
    """把 BiDi 的 headers 结构归一化成 {name.lower(): value}。

    BiDi 的 headers 可能是 ``{"name": ..., "value": ...}`` 列表，也可能是
    ``{value: {type, value}}`` 映射，两种形态都要能吃下。
    """

    out: Dict[str, str] = {}
    if isinstance(headers, dict):
        for key, raw in headers.items():
            if isinstance(raw, dict):
                value = raw.get("value")
            else:
                value = raw
            out[str(key).lower()] = "" if value is None else str(value)
    elif isinstance(headers, (list, tuple)):
        for item in headers:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            out[str(name).lower()] = str(item.get("value") or "")
    return out


def _extract_kasada_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """挑出与 KASADA / 会话相关的头，其余丢弃以免日志爆炸。"""

    picked: Dict[str, str] = {}
    for name, value in headers.items():
        if any(
            token in name
            for token in ("kpsdk", "captcha", "csrf", "cookie", "set-cookie",
                          "content-type", "location", "x-")
        ):
            picked[name] = value
    return picked


class RuyiKasadaRecorder:
    """旁路记录 KASADA 会话的完整网络证据（只读，不改写任何请求）。"""

    def __init__(self, page: Any, out_dir: Path) -> None:
        self.page = page
        self.out_dir = Path(out_dir)
        self.records: list[dict[str, Any]] = []
        self.scripts: list[dict[str, Any]] = []
        self._driver = None
        self._subscription_id = None
        self._collector_id = None
        self._pending: Dict[str, dict[str, Any]] = {}
        self._seen_script_urls: set[str] = set()
        self._lock = Lock()
        self._started = False
        self._index = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._started:
            return
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._driver = self.page._driver._browser_driver

        with contextlib.suppress(Exception):
            result = bidi_network.add_data_collector(
                self._driver,
                contexts=None,
                data_types=["request", "response"],
                max_encoded_data_size=_KASADA_SCRIPT_MAX_BYTES,
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
        self._started = True
        print(
            f"✓ KASADA 记录器已启动 (collector={bool(self._collector_id)}, "
            f"subscription={bool(self._subscription_id)}) → {self.out_dir}"
        )

    def stop(self) -> None:
        if not self._started:
            return
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

        self._started = False
        self.flush()
        print(f"✓ KASADA 记录器已停止，共 {len(self.records)} 条记录")

    # ---------- BiDi 回调 ----------

    def _on_request(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = str(req.get("url") or "")
        if not _kasada_is_candidate_url(url):
            return
        rid = str(req.get("request") or "")
        entry = {
            "seq": None,
            "at": time.time(),
            "phase": "request",
            "url": url,
            "method": str(req.get("method") or ""),
            "requestId": rid,
            "headers": _extract_kasada_headers(_header_pairs(req.get("headers"))),
            "bodyLength": None,
            "body": None,
            "status": None,
            "responseHeaders": {},
        }
        with self._lock:
            self._pending[rid] = entry
        self._record(entry)

    def _on_response(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = str(req.get("url") or "")
        if not _kasada_is_candidate_url(url):
            return
        rid = str(req.get("request") or "")
        resp = params.get("response", {}) or {}
        headers = _extract_kasada_headers(_header_pairs(resp.get("headers")))
        with self._lock:
            entry = self._pending.pop(rid, None)
        if entry is None:
            # 没有对应请求事件（订阅晚于请求），补一条
            entry = {
                "at": time.time(), "phase": "response", "url": url,
                "method": "", "requestId": rid, "headers": {},
                "bodyLength": None, "body": None,
            }
        entry["phase"] = "response"
        entry["status"] = resp.get("status")
        entry["responseHeaders"] = headers
        self._record(entry)

        # KASADA 的种子/令牌可能在响应体或响应头里 —— 落盘脚本源码
        if _kasada_is_script_url(url):
            self._dump_script(rid, url)

    def _on_fetch_error(self, params: Dict[str, Any]) -> None:
        req = params.get("request", {}) or {}
        url = str(req.get("url") or "")
        if _kasada_is_candidate_url(url):
            self._record({
                "at": time.time(), "phase": "error", "url": url,
                "method": str(req.get("method") or ""),
                "requestId": str(req.get("request") or ""),
                "headers": {}, "bodyLength": None, "body": None,
            })

    # ---------- 落盘 ----------

    def _record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._index += 1
            entry["seq"] = self._index
            self.records.append(entry)
        # 即时落盘，避免崩溃丢失观察窗口
        self._append_jsonl(entry)

    def _append_jsonl(self, entry: dict[str, Any]) -> None:
        path = self.out_dir / "kasada_network.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # 记录失败不能打断注册流程
            print(f"⚠️ KASADA 记录写入失败: {exc}")

    def _get_collected_data(self, request_id: str, data_type: str) -> str:
        if not self._driver or not self._collector_id or not request_id:
            return ""
        from ruyipage._bidi import network as bidi_network
        try:
            raw = bidi_network.get_data(
                self._driver, self._collector_id, request_id, data_type=data_type
            )
            return decode_bidi_body_value(raw)
        except Exception:
            return ""

    def _dump_script(self, request_id: str, url: str) -> None:
        """保存 KASADA 脚本源码（p.js / ips.js）。同一个 URL 只存一次。"""

        with self._lock:
            if url in self._seen_script_urls:
                return
            self._seen_script_urls.add(url)

        body = self._get_collected_data(request_id, "response")
        if not body:
            return
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", urlsplit(url).path or "script")[-80:]
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12]
        scripts_dir = self.out_dir / "kasada_scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        path = scripts_dir / f"{safe}.{digest}.js"
        try:
            path.write_text(body, encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"⚠️ KASADA 脚本保存失败 {url[:120]}: {exc}")
            return
        info = {
            "url": url,
            "path": str(path),
            "bytes": len(body.encode("utf-8", "replace")),
            "sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
        }
        with self._lock:
            self.scripts.append(info)
        print(
            f"✓ KASADA 脚本已保存: {pathsafe(url)[:100]} "
            f"({info['bytes']} 字节, sha256={info['sha256'][:12]})"
        )

    def flush(self) -> None:
        """把汇总写盘：头部清单 + 观察结论。"""

        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            records = list(self.records)
            scripts = list(self.scripts)

        names: Dict[str, int] = {}
        values: Dict[str, set] = {}
        for rec in records:
            for scope in (rec.get("headers") or {}, rec.get("responseHeaders") or {}):
                for name, value in scope.items():
                    names[name] = names.get(name, 0) + 1
                    if "kpsdk" in name or "captcha" in name:
                        values.setdefault(name, set()).add((value or "")[:200])

        summary = {
            "recordedAt": datetime.now().isoformat(timespec="seconds"),
            "recordCount": len(records),
            "scriptCount": len(scripts),
            "scripts": scripts,
            "headerNameCounts": dict(sorted(names.items(), key=lambda kv: -kv[1])),
            "interestingHeaderValues": {k: sorted(v) for k, v in values.items()},
            "sawKpsdkHeader": any("kpsdk" in n for n in names),
            "kasadaEvidence": {
                name: sum(
                    1 for r in records
                    if any(name in (r.get("headers") or {}) or name in (r.get("responseHeaders") or {})
                           for _ in (0,))
                )
                for name in ("x-kpsdk-cd", "x-kpsdk-ct", "x-kpsdk-v", "set-cookie")
            },
        }
        (self.out_dir / "kasada_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n✓ KASADA 汇总已保存：{self.out_dir / 'kasada_summary.json'}")


def pathsafe(url: str) -> str:
    """把 URL 压成可读的一行，便于日志。"""

    split = urlsplit(url)
    return f"{split.netloc}{split.path}"


def try_extract_blob_from_runtime(page) -> Optional[str]:
    """从页面 DOM/runtime 提取 blob（备用）"""
    js = r"""return (() => {
      const out = [];
      ['#capture-arkose','input[name="arkose"]','script[src]','iframe[src]','input'].forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
          ['src','data-arkose-src','data-exchange-data','value'].forEach(k => {
            const v = el.getAttribute(k) || el[k];
            if (v && typeof v === 'string') out.push(v);
          });
        });
      });
      return Array.from(new Set(out)).slice(0, 50);
    })();"""
    for ctx in all_contexts(page):
        try:
            vals = ctx.run_js(js, timeout=3) or []
            for val in vals:
                blob = extract_blob_from_body(str(val))
                if blob:
                    return blob
        except Exception:
            pass
    return None


def all_contexts(page, *extra) -> list[Any]:
    """Return the page, its currently available frames, and any extra roots' frames.

    ``extra`` 存在的原因是一个实测出来的坑：``page.new_tab()`` 开出的求解标签是
    **独立的 browsing context**，Arkose 的 iframe 挂在原 ``page`` 的子树下，不在
    solver_tab 的子树下。只枚举 solver_tab 时：

        all_contexts(solver_tab) -> 1 个（只有它自己）
        all_contexts(page)       -> 4 个（含 ec-game-core 里的 Verify 按钮）

    所以 harness 路径把 ``page`` 作为附加根传进来，两棵子树一起看。找不到时的
    后果很明确：``wait_verify_button`` 等 25 秒超时，``click_verify`` 必然点不到。
    """
    roots = [page, *extra]
    contexts: list[Any] = []
    seen: set[int] = set()
    for root in roots:
        if root is None:
            continue
        try:
            if id(root) not in seen:
                seen.add(id(root))
                contexts.append(root)
            for frame in (root.get_all_frames() or []):
                if id(frame) not in seen:
                    seen.add(id(frame))
                    contexts.append(frame)
        except Exception:
            continue
    return contexts


def decode_bidi_bytes(value: Any) -> bytes:
    """Decode Firefox BiDi network.getData() values to raw bytes."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        compact = re.sub(r"\s+", "", text)
        if len(compact) >= 16 and len(compact) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            with contextlib.suppress(Exception):
                return base64.b64decode(compact, validate=True)
        return value.encode("utf-8", errors="replace")
    if not isinstance(value, dict):
        return str(value).encode("utf-8", errors="replace")
    if "bytes" in value:
        return decode_bidi_bytes(value.get("bytes"))
    if "base64" in value:
        raw = value.get("base64")
        if isinstance(raw, dict):
            raw = raw.get("value")
        if raw:
            with contextlib.suppress(Exception):
                return base64.b64decode(str(raw))
    if value.get("type") == "base64" and value.get("value"):
        with contextlib.suppress(Exception):
            return base64.b64decode(str(value["value"]))
    if value.get("value") is not None:
        return decode_bidi_bytes(value["value"])
    return b""


def image_size(data: bytes) -> Optional[tuple[int, int]]:
    """Read dimensions when Pillow is available; image capture still works without it."""
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(data)) as image:
            return tuple(image.size)
    except Exception:
        return None


def image_extension(mime: str, data: bytes) -> str:
    mime = (mime or "").lower()
    if "png" in mime or data.startswith(b"\x89PNG"):
        return ".png"
    if "webp" in mime or data.startswith(b"RIFF"):
        return ".webp"
    if "jpeg" in mime or "jpg" in mime or data.startswith(b"\xff\xd8"):
        return ".jpg"
    return ".bin"


class RuyiArkoseCatcher:
    """Arkose Blob 捕获器（完整复制自 manual_same_browser_register_ruyipage.py）"""

    def __init__(self, page):
        self.page = page
        self.captured_blob = None
        self.captured_pk = None
        self.captured_surl = None
        self.fc_requests = []
        self._driver = None
        self._subscription_id = None
        self._collector_id = None
        self._pending = {}
        self._handled = set()
        self._lock = Lock()
        self._started = False

    def start(self):
        """启动 BiDi 网络监听"""
        if self._started:
            return
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        self._driver = self.page._driver._browser_driver

        # 添加数据收集器
        with contextlib.suppress(Exception):
            result = bidi_network.add_data_collector(
                self._driver,
                contexts=None,
                data_types=["request", "response"],
                max_encoded_data_size=2 * 1024 * 1024,
            )
            self._collector_id = result.get("collector")

        # 订阅网络事件
        result = bidi_session.subscribe(
            self._driver,
            ["network.beforeRequestSent", "network.responseCompleted", "network.fetchError"],
            contexts=None,
        )
        self._subscription_id = result.get("subscription")

        # 设置回调
        self._driver.set_callback("network.beforeRequestSent", self._on_request, context=None)
        self._driver.set_callback("network.responseCompleted", self._on_response, context=None)
        self._driver.set_callback("network.fetchError", self._on_fetch_error, context=None)
        self._started = True

        print(f"✓ Arkose 捕获器已启动 (collector={bool(self._collector_id)}, subscription={bool(self._subscription_id)})")

    def stop(self):
        """停止监听"""
        if not self._started:
            return
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

        self._started = False
        print("✓ Arkose 捕获器已停止")

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
            print(f"⚠️ 网络请求失败: {url[:180]}")

    def _get_collected_data(self, request_id: str, data_type: str) -> str:
        if not self._driver or not self._collector_id or not request_id:
            return ""
        from ruyipage._bidi import network as bidi_network
        try:
            raw = bidi_network.get_data(self._driver, self._collector_id, request_id, data_type=data_type)
            return decode_bidi_body_value(raw)
        except:
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
                print(f"✓ 捕获到 Blob! (长度: {len(blob)}, PK: {self.captured_pk})")

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
                print(f"✓ 从 DOM 提取到 Blob! (长度: {len(blob)})")

    def wait_for_blob(self, timeout: float = 45.0, expected_email: str = "",
                      probe_interval: float = BLOB_WAIT_SUCCESS_PROBE_INTERVAL
                      ) -> tuple[Optional[str], Optional[dict]]:
        """等 Arkose blob，同时探测页面是否已经注册成功。

        站点并不保证"先发 Arkose 请求、再放行"：实测有时 ``captcha-gate`` 刚出现、
        `/fc/gt2/` 的 blob 还没发出去，服务端就已经判定通过并把页面换成
        `You're All Set!`。此时若只顾着等 blob，会一路等到超时，最后报
        "未捕获到 Arkose blob"，把一次**已经成功**的注册当成失败（还白等 45 秒）。

        返回 ``(blob, snapshot)``：

        - 拿到 blob → ``(blob, None)``
        - 期间页面已成功 → ``(None, snapshot)``，其中 ``snapshot["success"]`` 为真
        - 超时 → ``(captured_blob, None)``（可能仍为 None）
        """
        print(f"\n等待 Blob 参数... (最多 {timeout} 秒；期间同时检测是否已注册成功)")
        deadline = time.time() + timeout
        next_probe = time.time() + probe_interval
        last_report = 0.0
        while time.time() < deadline:
            self.pump(timeout=0.5)
            if self.captured_blob:
                return self.captured_blob, None
            now = time.time()
            if now >= next_probe:
                next_probe = now + probe_interval
                snapshot = registration_success_snapshot(self.page, expected_email)
                if snapshot.get("success"):
                    return None, snapshot
            remaining = int(deadline - now)
            if remaining > 0 and (remaining % 5 == 0) and now - last_report > 0.8:
                print(f"  等待中... (剩余 {remaining} 秒)")
                last_report = now
        return self.captured_blob, None



# ==================== V6 同浏览器 YesCaptcha 求解器 ====================

def clean_prompt(value: str) -> str:
    """读取实时题目，只移除题目末尾的轮次标记。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    while True:
        cleaned = re.sub(r"\s*\(\s*\d+\s+of\s+\d+\s*\)\s*$", "", text, flags=re.I).strip()
        if cleaned == text:
            return text
        text = cleaned


def _question_js() -> str:
    selectors = json.dumps(list(QUESTION_SELECTORS))
    return f"""return (() => {{
      const selectors = {selectors};
      const roots = [document];
      const seen = new Set();
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      for (let i = 0; i < roots.length; i++) {{
        const root = roots[i];
        if (!root || seen.has(root)) continue;
        seen.add(root);
        try {{ root.querySelectorAll('*').forEach(el => {{ if (el.shadowRoot) roots.push(el.shadowRoot); }}); }} catch (e) {{}}
        for (const selector of selectors) {{
          let elements = [];
          try {{ elements = Array.from(root.querySelectorAll(selector)); }} catch (e) {{}}
          for (const element of elements) {{
            if (!visible(element)) continue;
            const text = (element.innerText || element.textContent || '').trim();
            if (text) return {{ok: true, selector, text}};
          }}
        }}
      }}
      return {{ok: false}};
    }})();"""


def extract_dynamic_prompt(page: Any, timeout: float = 5.0) -> dict[str, str]:
    """按 V6 逻辑从求解标签当前 DOM 读取题目，而不是写死题型。"""
    deadline = time.monotonic() + max(0.2, float(timeout))
    script = _question_js()
    while time.monotonic() < deadline:
        for context in all_contexts(page):
            try:
                result = context.run_js(script, timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            except Exception:
                continue
            if isinstance(result, dict) and result.get("ok"):
                prompt = clean_prompt(result.get("text") or "")
                if prompt:
                    return {"prompt": prompt, "selector": str(result.get("selector") or "")}
        time.sleep(0.1)
    raise RuntimeError("在实时 Arkose 页面中未找到动态题目")


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF"):
        return "image/webp"
    return "image/jpeg"


def classify_image(
    image_path: Path,
    *,
    question: str,
    api_key: str,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    """V6 使用的 FunCaptchaClassification 请求/轮询流程。"""
    if not api_key.strip():
        raise RuntimeError("YesCaptcha API Key 为空")
    image = image_path.read_bytes()
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "FunCaptchaClassification",
            "image": f"data:{image_mime(image)};base64,{base64.b64encode(image).decode('ascii')}",
            "question": clean_prompt(question),
        },
    }
    response = requests.post(YESCAPTCHA_CLASSIFICATION_URL, json=payload, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if result.get("errorId", 0) not in (None, 0):
        raise RuntimeError(f"YesCaptcha 创建分类任务失败: {result.get('errorDescription') or result.get('errorCode')}")

    task_id = result.get("taskId")
    if task_id and result.get("status") != "ready":
        deadline = time.monotonic() + max(30.0, float(timeout) * 4)
        while time.monotonic() < deadline:
            time.sleep(0.25)
            poll = requests.post(
                YESCAPTCHA_RESULT_URL,
                json={"clientKey": api_key, "taskId": task_id},
                timeout=timeout,
            )
            poll.raise_for_status()
            result = poll.json()
            if result.get("status") == "ready" or result.get("errorId", 0) not in (None, 0):
                break

    if result.get("errorId", 0) not in (None, 0):
        raise RuntimeError(f"YesCaptcha 分类失败: {result.get('errorDescription') or result.get('errorCode')}")
    objects = (result.get("solution") or {}).get("objects") or []
    if not objects:
        raise RuntimeError(f"YesCaptcha 返回中没有 solution.objects: {result}")
    try:
        answer = int(objects[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"YesCaptcha 返回了非整数答案: {objects[0]!r}") from exc
    if not 0 <= answer <= 20:
        raise RuntimeError(f"YesCaptcha 答案超出 0..20: {answer}")
    return answer, {"taskId": task_id, "status": result.get("status"), "objects": objects}


def normalize_surl(value: Optional[str]) -> str:
    text = (value or "").strip()
    # 粘贴/脱敏过程可能把 ASCII 点替换成中点或全角点；Arkose 主机名
    # 必须保持 ASCII DNS 形式，否则 solver harness 的 api.js 会加载失败。
    text = text.replace("·", ".").replace("．", ".").replace("。", ".")
    text = re.sub(r"\.{2,}", ".", text)
    if not text:
        return DEFAULT_SURL
    if "://" not in text:
        text = "https://" + text
    return urlsplit(text).netloc or DEFAULT_SURL


def origin_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"无效 URL: {value!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def safe_json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def build_solver_harness(public_key: str, blob: str, surl: str) -> str:
    """构造 V6 同源 Arkose harness；token 兼容 token/arkose/captcha 字段。"""
    api_url = f"https://{normalize_surl(surl)}/v2/{public_key}/api.js"
    cfg = safe_json_for_script({"publicKey": public_key, "blob": blob, "apiUrl": api_url, "language": "en-US"})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Arkose Solver</title>
<style>html,body{{margin:0;min-height:100%;font-family:Arial,sans-serif}}#arkose-container{{min-height:700px;display:flex;justify-content:center;align-items:flex-start;padding-top:18px}}</style>
</head><body><div id="arkose-container"></div>
<script>
(() => {{
  const cfg = {cfg};
  const state = window.__ARKOSE_MANUAL__ = {{apiReady:false,runCalled:false,token:null,tokenLength:0,status:'boot',error:null,completedPayload:null,events:[]}};
  let enforcement = null;
  const safe = v => {{ try {{ return JSON.parse(JSON.stringify(v)); }} catch (_) {{ return String(v); }} }};
  const emit = (name, payload) => {{ state.status=name; state.events.push({{name,at:Date.now(),payload:safe(payload)}}); console.log('[ARKOSE-RUYI]',name,payload||''); }};
  const runIt = () => {{ if (!enforcement) return emit('run-before-ready',null); try {{ state.runCalled=true; enforcement.run(); emit('run',null); }} catch(e) {{ state.error=String(e&& (e.stack||e.message)||e); emit('run-error',state.error); }} }};
  window.setupEnforcement = myEnforcement => {{
    enforcement=myEnforcement; state.apiReady=true; emit('api-ready',{{publicKey:cfg.publicKey,hasBlob:!!cfg.blob}});
    try {{ myEnforcement.setConfig({{
      publicKey:cfg.publicKey, selector:'#arkose-container', mode:'inline', language:cfg.language,
      data:cfg.blob ? {{blob:cfg.blob}} : {{}},
      onReady:r => {{ emit('onReady',r); if (!state.runCalled) setTimeout(runIt,100); }},
      onShow:r => emit('onShow',r), onShown:r => emit('onShown',r), onHide:r => emit('onHide',r),
      onReset:r => {{ state.runCalled=false; emit('onReset',r); }}, onWarning:r => emit('onWarning',r),
      onFailed:r => {{ state.error=safe(r); emit('onFailed',r); }},
      onError:r => {{ state.error=safe(r); emit('onError',r); }},
      onCompleted:r => {{
        state.completedPayload=safe(r);
        const token=r && (r.token || r.arkose || r.captcha);
        state.token=token ? String(token) : null; state.tokenLength=state.token ? state.token.length : 0;
        emit('onCompleted',{{tokenLength:state.tokenLength}});
      }}
    }}); }} catch(e) {{ state.error=String(e&& (e.stack||e.message)||e); emit('setConfig-error',state.error); }}
  }};
  const script=document.createElement('script'); script.src=cfg.apiUrl; script.async=true; script.defer=true;
  script.setAttribute('data-callback','setupEnforcement'); script.onload=()=>emit('script-loaded',cfg.apiUrl); script.onerror=()=>{{state.error='api.js load failed';emit('script-error',state.error);}};
  document.head.appendChild(script); emit('script-added',cfg.apiUrl);
}})();
</script></body></html>"""


def replace_document_under_origin(page: Any, website_url: str, html: str) -> None:
    expected_origin = origin_from_url(website_url)
    try:
        page.get(expected_origin + "/", wait="interactive", timeout=30)
    except Exception:
        pass
    with contextlib.suppress(Exception):
        page.stop_loading()
    actual = page.run_js("return {url:location.href,origin:location.origin};", timeout=10)
    if actual.get("origin") != expected_origin:
        raise RuntimeError(f"求解标签 origin 不匹配: expected={expected_origin}, actual={actual}")
    page.run_js(
        """function(value){ window.stop(); document.open(); document.write(value); document.close(); }""",
        html,
        timeout=15,
    )


@dataclass
class RuyiArkoseImageCatcher:
    """V6 同浏览器图片捕获器，监听所有 tab/frame 的 Arkose 图片响应。"""
    page: Any
    captured_images: list[dict] = field(default_factory=list)
    _driver: Any = None
    _subscription_id: Optional[str] = None
    _collector_id: Optional[str] = None
    _lock: Lock = field(default_factory=Lock)
    _event: Event = field(default_factory=Event)
    _seen_requests: set[str] = field(default_factory=set)
    _started: bool = False

    def start(self) -> None:
        if self._started:
            return
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session

        self._driver = self.page._driver._browser_driver
        with contextlib.suppress(Exception):
            result = bidi_network.add_data_collector(
                self._driver,
                contexts=None,
                data_types=["response"],
                max_encoded_data_size=8 * 1024 * 1024,
            )
            self._collector_id = result.get("collector")
        result = bidi_session.subscribe(self._driver, ["network.responseCompleted", "network.fetchError"], contexts=None)
        self._subscription_id = result.get("subscription")
        self._driver.set_callback("network.responseCompleted", self._on_response, context=None)
        self._driver.set_callback("network.fetchError", self._on_fetch_error, context=None)
        self._started = True
        print(f"✓ Arkose 题图捕获器已启动 (collector={bool(self._collector_id)})")

    def stop(self) -> None:
        if not self._started:
            return
        from ruyipage._bidi import network as bidi_network
        from ruyipage._bidi import session as bidi_session
        with contextlib.suppress(Exception):
            self._driver.remove_callback("network.responseCompleted", context=None)
            self._driver.remove_callback("network.fetchError", context=None)
        with contextlib.suppress(Exception):
            if self._subscription_id:
                bidi_session.unsubscribe(self._driver, subscription=self._subscription_id)
        with contextlib.suppress(Exception):
            if self._collector_id:
                bidi_network.remove_data_collector(self._driver, self._collector_id)
        self._started = False

    @staticmethod
    def _looks_like_image(url: str, mime: str) -> bool:
        lower_url = (url or "").lower()
        if "/rtig/image" in lower_url:
            return True
        return "arkoselabs.com" in lower_url and mime.startswith("image/") and "/fc/assets/" not in lower_url

    def _on_fetch_error(self, params: Dict[str, Any]) -> None:
        request = params.get("request", {}) or {}
        url = request.get("url", "") or ""
        if "/rtig/image" in url:
            print(f"⚠️ Arkose 题图请求失败: {url[:180]}")

    def _on_response(self, params: Dict[str, Any]) -> None:
        request = params.get("request", {}) or {}
        response = params.get("response", {}) or {}
        url = response.get("url") or request.get("url") or ""
        request_id = request.get("request") or ""
        mime = str(response.get("mimeType") or response.get("mime") or "").lower()
        if not request_id or not self._looks_like_image(url, mime):
            return
        try:
            status = int(response.get("status") or response.get("statusCode") or 0)
        except (TypeError, ValueError):
            status = 0
        # blob 过期时 /rtig/image 会返回 403 + "DENIED ACCESS"（13 字节文本）。
        # 那不是题图，一旦被当成题图送去 YesCaptcha 只会浪费额度并给出错答案。
        if status and not (200 <= status < 300):
            with contextlib.suppress(Exception):
                print(f"⚠️ 跳过非 2xx 的 Arkose 图片响应 (status={status}): {url[:140]}")
            return
        if mime and "/rtig/image" in url.lower() and not mime.startswith("image/"):
            with contextlib.suppress(Exception):
                print(f"⚠️ 跳过非图片 mime 的 Arkose 题图响应 (mime={mime}): {url[:140]}")
            return
        with self._lock:
            if request_id in self._seen_requests:
                return
            self._seen_requests.add(request_id)
            record = {
                "url": url,
                "mime": mime,
                "status": response.get("status") or response.get("statusCode") or 0,
                "requestId": request_id,
                "timestamp": time.time(),
                "body_bytes": None,
            }
            self.captured_images.append(record)
            index = len(self.captured_images) - 1
        print(f"  发现 Arkose 题图 #{index + 1}: {url[:140]}")
        self._collect_body(request_id, index)

    def _collect_body(self, request_id: str, index: int) -> None:
        if not self._driver or not self._collector_id:
            return
        from ruyipage._bidi import network as bidi_network
        try:
            data = decode_bidi_bytes(bidi_network.get_data(self._driver, self._collector_id, request_id, data_type="response"))
        except Exception as exc:
            print(f"⚠️ 读取题图正文失败: {type(exc).__name__}: {exc}")
            return
        if not data:
            return
        with self._lock:
            record = self.captured_images[index]
            record.update({
                "body_bytes": data,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": image_size(data),
            })
        self._event.set()
        print(f"  题图正文已捕获: bytes={len(data)} sha256={record['sha256'][:12]} size={record['size']}")

    def wait_new_challenge(self, seen: set[str], timeout: float) -> Optional[dict]:
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            with self._lock:
                ready = [dict(record) for record in self.captured_images if record.get("body_bytes")]
            ready.sort(key=lambda record: (0 if "/rtig/image" in str(record.get("url") or "").lower() else 1, record.get("timestamp") or 0))
            for record in ready:
                data = record.get("body_bytes") or b""
                sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
                if sha in seen:
                    continue
                size = record.get("size") or image_size(data)
                if size:
                    width, height = size
                    if "/rtig/image" not in str(record.get("url") or "").lower() and not (width >= 800 and 300 <= height <= 650):
                        seen.add(sha)
                        continue
                return record
            self._event.wait(0.4)
            self._event.clear()
        return None


def save_image_record(record: dict, out_dir: Path, wave: int, prefix: str = "yescaptcha") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = record.get("body_bytes") or b""
    sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
    stem = f"{prefix}_wave_{wave:02d}_{sha[:12]}"
    path = out_dir / f"{stem}{image_extension(record.get('mime') or '', data)}"
    path.write_bytes(data)
    metadata = {key: value for key, value in record.items() if key != "body_bytes"}
    metadata.update({"file": str(path), "sha256": sha, "bytes": len(data)})
    (out_dir / f"{stem}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def solver_state(page: Any) -> dict[str, Any]:
    return page.run_js(
        """return (() => { const s=window.__ARKOSE_MANUAL__||{}; return {
          status:s.status||null, token:s.token||s.arkose||s.captcha||null,
          tokenLength:s.tokenLength||(s.token?String(s.token).length:0), error:s.error||null,
          completedPayload:s.completedPayload||null }; })();""",
        timeout=5,
    ) or {}


def wait_token_quick(page: Any, timeout: float) -> Optional[str]:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        state = solver_state(page)
        status = state.get("status")
        if status in {"onFailed", "onError", "run-error", "setConfig-error", "script-error"}:
            raise RuntimeError(f"Arkose 求解器失败: {status}: {state.get('error')}")
        token = state.get("token")
        if token:
            return str(token)
        time.sleep(0.2)
    return None


def wait_native_token(page: Any, timeout: float) -> Optional[str]:
    """读原页里 Arkose 自己产出的 token（``window.__ARKOSE_NATIVE__``）。

    这个变量由本脚本注入的 ``setupEnforcement`` 钩子写；若站点是自行渲染的
    （没有走到注入分支），则退回读 captcha 输入框的值。
    """
    deadline = time.time() + max(0.0, timeout)
    script = r"""return (() => {
      const s = window.__ARKOSE_NATIVE__;
      if (s) {
        if (s.token) return {ok:true, token:String(s.token), source:'hook'};
        if (s.status === 'onFailed' || s.status === 'onError' || s.status === 'run-error' || s.status === 'setConfig-error' || s.status === 'script-error') {
          return {ok:false, failed:true, status:s.status, error:String(s.error || '')};
        }
      }
      const input = document.querySelector('#capture-captcha,input[name="captcha"],input[name="arkose"]');
      if (input && input.value && String(input.value).length > 40) return {ok:true, token:String(input.value), source:'input'};
      return {ok:false};
    })();"""
    while True:
        # 不能走 run_js_any_context：它只透传 ok=True 的结果，会把
        # {ok:false, failed:true} 的失败态吞掉，导致这里只会一路空等到超时。
        result = None
        for context in all_contexts(page):
            with contextlib.suppress(Exception):
                candidate = context.run_js(script, timeout=3)
                if isinstance(candidate, dict) and (candidate.get("ok") or candidate.get("failed")):
                    result = candidate
                    break
        if isinstance(result, dict) and result.get("ok"):
            return str(result.get("token"))
        if isinstance(result, dict) and result.get("failed"):
            raise RuntimeError(f"原生 Arkose 求解失败: {result.get('status')}: {result.get('error')}")
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def run_js_any_context(page: Any, script: str, *args, timeout: float = 3.0, extra_roots: tuple = ()) -> Any:
    """在 page（及其帧）与任意附加根（及其帧）里执行脚本，返回第一个 ok=True 的结果。

    ``extra_roots`` 见 ``all_contexts``：harness 的求解标签与注册页是两棵独立的
    browsing context 树，Arkose 帧只挂在注册页那棵上。
    """
    for context in all_contexts(page, *extra_roots):
        with contextlib.suppress(Exception):
            result = context.run_js(script, *args, timeout=timeout)
            if isinstance(result, dict) and result.get("ok"):
                return result
    return None


def _dump_verify_candidates(page: Any, *extra_roots: Any) -> None:
    """点不到 Verify 时，把每个 context 的可见按钮列出来，便于定位选择器问题。

    同样接受附加根：harness 路径下 Arkose 帧在注册页那棵树上，只列求解标签
    自己会得到一份空清单，反而掩盖问题。
    """
    script = r"""return (() => {
      const visible=el=>!!el && !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
      const out=[];
      const roots=[document], seen=new Set();
      for(let i=0;i<roots.length;i++){
        const root=roots[i]; if(!root||seen.has(root)) continue; seen.add(root);
        try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot) roots.push(el.shadowRoot);});}catch(e){}
        let els=[]; try{els=Array.from(root.querySelectorAll('button,[role="button"],[data-theme]'));}catch(e){}
        for(const el of els){
          if (!visible(el)) continue;
          const c=el.getAttribute('class');
          out.push({tag:el.tagName, id:el.id||'', class:(typeof c==='string'?c:''),
                    theme:el.getAttribute('data-theme')||'',
                    text:((el.innerText||'')+'|'+(el.getAttribute('aria-label')||'')).slice(0,60),
                    type:el.type||''});
          if (out.length>=20) return out;
        }
      } return out; })();"""
    contexts = all_contexts(page, *extra_roots)
    print(f"    共枚举到 {len(contexts)} 个 context（page 及其帧 + 附加根及其帧）")
    for idx, context in enumerate(contexts):
        cid = getattr(context, "_context_id", None)
        url = ""
        with contextlib.suppress(Exception):
            url = str(getattr(context, "url", "") or "")
        rows: Any = None
        err = ""
        try:
            rows = context.run_js(script, timeout=2)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        n = len(rows) if isinstance(rows, list) else 0
        # 每个 context 都必须有输出行，哪怕 0 个按钮。否则"只列出 context 0"
        # 无法区分两种完全不同的情况：只枚举到 1 个 context（帧树有问题），
        # 还是枚举到多个但只有第一个有按钮（帧正常）。真机上正是这个歧义
        # 让排查方向偏过。
        print(f"    [context {idx}] cid={cid} buttons={n}"
              + (f"  ⚠读取失败: {err}" if err else "")
              + (f"  {url[:100]}" if url else ""))
        for row in (rows if isinstance(rows, list) else []):
            print(f"      {row.get('tag')} id={row.get('id')!r} class={row.get('class')!r} "
                  f"theme={row.get('theme')!r} type={row.get('type')!r} text={row.get('text')!r}")


def click_verify(page: Any, *extra_roots: Any, timeout: float = 20.0) -> bool:
    """点 Arkose 挑战起始卡片上的 Verify 按钮。

    ``extra_roots`` 是**位置参数**，``timeout`` 是 keyword-only，顺序不能反：
    早先写成 ``(page, timeout=20.0, *extra_roots)``，调用 ``click_verify(tab,
    timeout=25.0, *extra)`` 时 ``*extra`` 展开的位置参数会抢占 ``timeout`` 的
    槽位，同时关键字又赋一次同一个参数，直接抛
    ``TypeError: got multiple values for argument 'timeout'``。真机上 harness
    路径就是这样一进门就崩的——25 秒等待压根没执行。

    真实 DOM（challenge iframe 内，实测）：

        BUTTON class='sc-nkuzb1-0 sc-d5trka-0 eZxMRy button'
               data-theme='home.verifyButton' type='submit' text='Verify'
        BUTTON class='sc-rqvnx3-0 kTxXTj audio-button icon-button'
               data-theme='' type='submit' text='Audio'

    有两个反直觉之处，都踩过：

    1. **class 是 CSS-in-JS 哈希**（``sc-nkuzb1-0`` ``eZxMRy``），不含 ``fc-button``；
       旧代码按 ``fc-button`` 之类匹配 class，一个都点不中。
    2. **按钮自身是 ``type='submit'``**。旧代码把 ``type==='submit'`` 当作"站点按钮"
       直接排除，于是唯一的 Arkose 按钮也被排除了——这就是卡在 Verification
       卡片上不动的直接原因。

    可靠的判据是 ``data-theme``：Arkose 给它放了语义名（``home.verifyButton`` /
    ``home.title`` / ``home.instructions``），而 Audio 按钮的 ``data-theme`` 是空串。
    区分"站点按钮 / Arkose 按钮"改用 **所在容器**：站点提交按钮在 ``form#flow-form``
    或 ``form[action*="captcha-gate"]`` 里，Arkose 按钮在 ``index.html?session=…``
    那个 challenge iframe 里。绝不再用按钮自身的 type 做判据。
    """
    script = r"""return (() => {
      const roots=[document], seen=new Set();
      const visible=el=>!!el && !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
      // 站点自己的表单按钮：按**容器**排除，不按 type。Arkose 的 Verify 也是
      // type=submit，用 type 排除会把唯一该点的按钮排掉。
      const isSiteSubmit=el=>{
        if ((el.id||'') === 'flow-form-submit-btn') return true;
        try { if (el.closest && el.closest('form#flow-form,form[action*="captcha-gate"]')) return true; } catch(e) {}
        return false;
      };
      // Arkose 挑战 iframe：ec-game-core 的 index.html?session=…
      const inArkoseFrame=el=>{
        try {
          let w = el.ownerDocument && el.ownerDocument.defaultView;
          if (!w || w === window) return false;
          const href = String(w.location.href || '');
          return /ec-game-core|index\.html\?session=|arkoselabs\.com\/fc/i.test(href);
        } catch(e) { return false; }
      };
      const VERIFY_TEXT=/^\s*(verify|验证)\s*$/i;
      const ANY_VERIFY=/verify|验证/i;
      // data-theme 的 Arkose 语义名，实测 home.verifyButton；verify 单独兜底。
      const THEME_VERIFY=/^home\.verify\w*$|(^|[._-])verify([._-]|$)/i;
      const clsOf=el=>{ const c=el.getAttribute('class'); return typeof c === 'string' ? c : ''; };
      for(let i=0;i<roots.length;i++){
        const root=roots[i]; if(!root||seen.has(root)) continue; seen.add(root);
        try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot) roots.push(el.shadowRoot);});}catch(e){}
        let els=[]; try{els=Array.from(root.querySelectorAll('button,[role="button"],[data-theme]'));}catch(e){}
        for(const el of els){
          if (isSiteSubmit(el)) continue;
          if (!visible(el)) continue;
          const id=(el.id||'').trim();
          const klass=clsOf(el);
          const theme=(el.getAttribute('data-theme')||'').trim();
          const text=((el.innerText||'')+' '+(el.getAttribute('aria-label')||'')).replace(/\s+/g,' ').trim();
          const inFrame=inArkoseFrame(el);
          // 点得中的条件，按可靠度排序：
          //  a) data-theme 是 Arkose 的 verify 语义名 —— 最干净，Audio 的 theme 是空串；
          //  b) 在 Arkose 挑战 iframe 内且文案是 Verify；
          //  c) 在 Arkose 挑战 iframe 内且文案含 verify（其它语言/变体）。
          const hit=(THEME_VERIFY.test(theme) && (inFrame || ANY_VERIFY.test(text)))
                 || (inFrame && VERIFY_TEXT.test(text))
                 || (inFrame && ANY_VERIFY.test(text));
          if (hit) {
            el.click();
            return {ok:true, id, klass, theme, text, inFrame,
                    marker:(theme+' | '+klass+' | '+text).slice(0,140)};
          }
        }
      } return {ok:false}; })();"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = run_js_any_context(page, script, timeout=2, extra_roots=extra_roots)
        if hit:
            print(f"  ✓ 已点击求解标签 Verify ({hit.get('marker', '')})")
            return True
        time.sleep(0.4)
    _dump_verify_candidates(page, *extra_roots)
    print("  ✗ 未找到可点击的 Arkose Verify 按钮；上面已列出各 context 的可见按钮")
    return False


def current_image_index(page: Any) -> int:
    script = r"""return (() => { for(const img of Array.from(document.querySelectorAll('img[aria-label],img'))){
      const text=img.getAttribute('aria-label')||''; const nums=text.match(/\d+/g);
      if(nums && nums.length>=1 && /image|of|图/i.test(text)){ const n=parseInt(nums[0],10); if(n>=1&&n<=20) return n-1; }
    } return -1; })();"""
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            value = context.run_js(script, timeout=2)
            if isinstance(value, int) and value >= 0:
                return value
    return -1


def wait_image_index_change(page: Any, before: int, timeout: float = 1.2) -> int:
    deadline = time.time() + timeout
    latest = before
    while time.time() < deadline:
        latest = current_image_index(page)
        if before < 0 or (latest >= 0 and latest != before):
            return latest
        time.sleep(0.05)
    return latest


def click_right_arrow(page: Any, timeout: float = 5.0) -> bool:
    script = r"""return (() => { const roots=[document],seen=new Set();
      const visible=el=>!!el&&!el.disabled&&el.getAttribute('aria-disabled')!=='true'&&!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
      for(let i=0;i<roots.length;i++){const root=roots[i];if(!root||seen.has(root))continue;seen.add(root);
        try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot)roots.push(el.shadowRoot);});}catch(e){}
        for(const sel of ['a.right-arrow','[class*="right-arrow"]','button[aria-label*="Next"]','a[aria-label*="Next"]','[aria-label*="next"]']){
          let els=[];try{els=Array.from(root.querySelectorAll(sel));}catch(e){} for(const el of els){if(!visible(el))continue;el.scrollIntoView({block:'center',inline:'center'});el.click();return {ok:true,selector:sel};}
        }} return {ok:false}; })();"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run_js_any_context(page, script, timeout=2):
            return True
        time.sleep(0.25)
    return False


def click_solver_submit(page: Any, timeout: float = 6.0) -> bool:
    script = r"""return (() => { const roots=[document],seen=new Set();
      const visible=el=>!!el&&!el.disabled&&el.getAttribute('aria-disabled')!=='true'&&!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
      for(let i=0;i<roots.length;i++){const root=roots[i];if(!root||seen.has(root))continue;seen.add(root);
        try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot)roots.push(el.shadowRoot);});}catch(e){}
        for(const el of Array.from(root.querySelectorAll('button,input[type="submit"],[role="button"]'))){const mark=((el.textContent||el.value||'')+' '+(el.getAttribute('aria-label')||'')+' '+(el.className||'')).toLowerCase();
          if(visible(el)&&/submit|continue|提交|yuVdl/.test(mark)&&!(/verify|human|验证|人类/.test(mark)&&!/submit|提交/.test(mark))){el.click();return {ok:true,marker:mark.slice(0,100)};}}
      } return {ok:false}; })();"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run_js_any_context(page, script, timeout=2):
            return True
        time.sleep(0.3)
    return False


def wait_image_or_token(catcher: RuyiArkoseImageCatcher, seen: set[str], timeout: float, solver_tab: Any) -> tuple[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = wait_token_quick(solver_tab, 0.15)
        if token:
            return "token", token
        record = catcher.wait_new_challenge(seen, timeout=min(0.7, max(0.1, deadline - time.time())))
        if record:
            return "image", record
    token = wait_token_quick(solver_tab, 0.3)
    return ("token", token) if token else ("timeout", None)


def click_next_n(page: Any, count: int) -> bool:
    count = max(0, int(count))
    for index in range(count):
        before = current_image_index(page)
        if not click_right_arrow(page):
            return False
        after = wait_image_index_change(page, before)
        print(f"  点击第 {index + 1}/{count} 张箭头: {before} -> {after}")
        if before >= 0 and after == before:
            return False
    return True


def reset_image_catcher(catcher: RuyiArkoseImageCatcher) -> None:
    """开新一轮求解前清掉去重集合。

    ``_seen_requests`` 原先永不重置，一次运行里每个 requestId 只会被采集一次，
    于是后续轮次拿到的总是更早的陈旧响应（403 尸体、旧題图）。这里在每轮求解
    开始时清空，保证 wait_new_challenge 只看本轮真正的新响应。
    """
    if not catcher:
        return
    with catcher._lock:
        catcher._seen_requests.clear()


def auto_solve_yescaptcha_tab(
    solver_tab: Any,
    catcher: RuyiArkoseImageCatcher,
    out: Path,
    answer_prefix: str = "",
    reuse_wave: Optional[dict] = None,
    root_page: Any = None,
) -> tuple[str, dict]:
    """V6 per-wave 动态题图 -> YesCaptcha 分类 -> 箭头 -> Submit -> token。

    返回 ``(token, replay)``。``replay`` 记录最后一轮"不依赖题目内容"的动作
    （答案下标 + Submit），供下一轮在题干相同的挑战被复用时直接重放，避免拿
    新題图 + 旧題干去问 YesCaptcha、从而得到必然错误的答案。

    ``reuse_wave`` 是上一轮返回的 replay 记录；给定时先按它重放一次，
    重放后仍未拿到 token 再走正常的抓图分类流程。
    """
    api_key = os.getenv("YESCAPTCHA_API_KEY", YESCAPTCHA_API_KEY).strip()
    if not api_key:
        raise RuntimeError("请设置脚本顶部 YESCAPTCHA_API_KEY 或环境变量 YESCAPTCHA_API_KEY")
    images_dir = out / "yescaptcha_images"
    # 冻结本轮的图片应答；reset 放在订阅之后、click_verify 之前，避免把
    # click_verify 触发的新 challenge 响应也清掉。
    reset_image_catcher(catcher)
    seen: set[str] = set()
    actions: list[dict[str, Any]] = []
    replay: dict[str, Any] = dict(reuse_wave) if reuse_wave else {}

    token = wait_token_quick(solver_tab, 2.0)
    if token:
        return token, replay
    # 先等 Verify 按钮真正渲染出来再点。harness 文档是同步写入的，但 Arkose 的
    # React 树要等 api.js 执行完才挂载；抢在前面点是必然失败的。
    #
    # root_page 必须一起传：求解标签是独立 browsing context，Arkose 的 iframe
    # 挂在注册页那棵树上（实测 all_contexts(solver_tab)=1，all_contexts(page)=4）。
    # 只枚举求解标签自己的话，这里必然 25 秒超时、后面必然点不到。
    extra = (root_page,) if root_page is not None else ()
    if not wait_verify_button(solver_tab, *extra, timeout=25.0):
        print("  ⚠️ Verify 按钮迟迟没渲染出来；下面仍尝试点一次，若抓不到题图问题就在这里")
        # 页面可能是空白的（连 iframe 都没有）。把运行时状态落盘，否则只剩
        # 一行"没渲染出来"，分不清是 harness 没跑、api.js 没加载，还是 CSP 拦了。
        with contextlib.suppress(Exception):
            save_solver_diagnostics(solver_tab, out, prefix=f"{answer_prefix or ''}solver_blank".lstrip("_"))
    if not click_verify(solver_tab, *extra, timeout=20.0):
        print("  ⚠️ 未点到 Arkose Verify 按钮；若接下来抓不到题图，问题就在这里")

    if replay.get("answer") is not None:
        kind, value = wait_image_or_token(catcher, seen, REPLAY_PROBE_TIMEOUT, solver_tab)
        if kind == "token":
            return str(value), replay
        if kind == "image":
            answer = int(replay["answer"])
            print(f"  题目与上一轮相同，直接重放上一次答案: {answer}")
            if not click_next_n(solver_tab, answer):
                raise RuntimeError(f"重放轮无法点击下一张箭头 {answer} 次")
            time.sleep(0.08 + random.random() * 0.08)
            submit_ok = click_solver_submit(solver_tab)
            actions.append({"wave": "replay", "answer": answer, "reusedQuestion": True, "submit": submit_ok})
            (out / "yescaptcha_actions_latest.json").write_text(
                json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not submit_ok:
                raise RuntimeError("重放轮 Submit 点击失败")
            token = wait_token_quick(solver_tab, 3.0)
            if token:
                return token, replay
            replay = {}

    for wave in range(12):
        token = wait_token_quick(solver_tab, 0.8)
        if token:
            return token, replay
        kind, value = wait_image_or_token(catcher, seen, 45.0 if wave == 0 else 30.0, solver_tab)
        if kind == "token":
            return str(value), replay
        if not value:
            # 本轮没抓到新题图。若已解出 token 就返回；否则如实报错，
            # 不去猜答案（宁可失败也不提交一个赌博性的答案）。
            token = wait_token_quick(solver_tab, 30.0)
            if token:
                return token, replay
            raise RuntimeError(f"第 {wave} 轮未捕获到新的 Arkose 题图")
        record = value
        data = record.get("body_bytes") or b""
        sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
        seen.add(sha)
        image_path = save_image_record(record, images_dir, wave, prefix=answer_prefix or "yescaptcha")
        question = extract_dynamic_prompt(solver_tab, timeout=5.0)
        question_stem = clean_prompt(question["prompt"])
        # Arkose 为每一步都发新題图；若题干与刚解过的完全相同，说明这多半不是
        # 新挑战（陈旧响应），拿这张新图去问分类会得到与旧題干不匹配的错答案。
        # 此时重放上次的答案，并把題图留档备查。
        if replay.get("question") == question_stem and replay.get("answer") is not None:
            question_file = images_dir / f"{answer_prefix or 'yescaptcha'}_wave_{wave:02d}_question.json"
            question_file.write_text(
                json.dumps({**question, "reusedFromPreviousWave": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            answer = int(replay["answer"])
            print(f"  断言题干未变（{question_stem}），重放上一次答案: {answer}")
            if not click_next_n(solver_tab, answer):
                raise RuntimeError(f"第 {wave} 轮无法点击下一张箭头 {answer} 次")
            time.sleep(0.08 + random.random() * 0.08)
            submit_ok = click_solver_submit(solver_tab)
            actions.append({
                "wave": wave, "image": str(image_path), "sha256": sha, "answer": answer,
                "question": {**question, "reusedFromPreviousWave": True}, "submit": submit_ok,
                "reusedAnswer": True,
            })
            (out / "yescaptcha_actions_latest.json").write_text(
                json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not submit_ok:
                raise RuntimeError(f"第 {wave} 轮 Submit 点击失败")
            token = wait_token_quick(solver_tab, 2.0)
            if token:
                return token, replay
            continue
        (images_dir / f"{answer_prefix or 'yescaptcha'}_wave_{wave:02d}_question.json").write_text(
            json.dumps(question, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        answer, api_result = classify_image(image_path, question=question["prompt"], api_key=api_key)
        print(f"  YesCaptcha 题目: {question_stem}")
        print(f"  YesCaptcha 答案: {answer}")
        if not click_next_n(solver_tab, answer):
            raise RuntimeError(f"第 {wave} 轮无法点击下一张箭头 {answer} 次")
        time.sleep(0.08 + random.random() * 0.08)
        submit_ok = click_solver_submit(solver_tab)
        actions.append({"wave": wave, "image": str(image_path), "sha256": sha, "answer": answer, "question": question, "submit": submit_ok, "yescaptcha": api_result})
        (out / "yescaptcha_actions_latest.json").write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
        if not submit_ok:
            raise RuntimeError(f"第 {wave} 轮 Submit 点击失败")
        replay = {"answer": answer, "question": question_stem, "wave": wave}
        token = wait_token_quick(solver_tab, 2.0)
        if token:
            return token, replay

    token = wait_token_quick(solver_tab, 30.0)
    if token:
        return token, replay
    raise RuntimeError("Arkose 求解超过最大轮次仍未返回 token")


# ==================== 辅助函数 ====================

def generate_identity():
    age = random.randint(18, 35)
    birth_date = datetime.now() - timedelta(days=age * 365 + random.randint(0, 365))
    dob = birth_date.strftime("%m/%d/%Y")
    password_length = random.randint(8, 16)
    password = ''.join([
        random.choice(string.ascii_uppercase) for _ in range(2)
    ] + [
        random.choice(string.ascii_lowercase) for _ in range(password_length - 4)
    ] + [
        random.choice(string.digits) for _ in range(2)
    ])
    password = ''.join(random.sample(password, len(password)))
    return {
        "dob": dob,
        "password": password,
        "battletag": f"Amirex{random.randint(10, 99)}",
        "first_name": random.choice(["James", "John", "Robert", "Michael", "William"]),
        "last_name": random.choice(["Smith", "Johnson", "Williams", "Brown", "Jones"]),
        "country": "USA",
    }

def wait_user(msg="按回车继续..."):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")
    input()

def get_step(page):
    for ctx in all_contexts(page):
        try:
            result = ctx.run_js(
                r"""(() => {
                  const has = selector => !!document.querySelector(selector);
                  if (has('#accountName')) return 'account-name';
                  if (has('#capture-country')) return 'get-started';
                  if (has('#capture-first-name')) return 'provide-name';
                  if (has('#capture-email')) return 'provide-credentials';
                  if (has('input[type="checkbox"][name="tou-agreements-implicit"]')) return 'legal-and-opt-ins';
                  if (has('#capture-password')) return 'set-password';
                  if (has('#capture-battletag')) return 'set-battletag';
                  if (has('#capture-arkose,input[name="arkose"],input[name="captcha"]')) return 'captcha-gate';
                  const html = document.documentElement?.innerHTML?.toLowerCase() || '';
                  if (html.includes('arkoselabs') || html.includes('funcaptcha')) return 'captcha-gate';
                  return 'unknown';
                })()""",
                timeout=1.5,
            )
            if isinstance(result, dict) and result.get("type") == "function":
                result = None
            if result and result != 'unknown':
                return result
        except Exception:
            continue
    return 'unknown'

def wait_for_step(page, expected, timeout=8.0, interval=0.08):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_step(page) == expected:
            return True
        time.sleep(interval)
    return False


def input_text(page, selector, value, desc, timeout=5.0):
    """幂等快速填值；使用原生 setter 一次写入，避免逐字符输入造成额外延迟。"""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            result = page.run_js(
                r"""function(selector, value) {
                  const el = document.querySelector(selector);
                  if (!el) return {ok:false, reason:'not-found'};
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                  if (setter) setter.call(el, value); else el.value = value;
                  for (const type of ['input', 'change', 'blur']) {
                    el.dispatchEvent(new Event(type, {bubbles:true}));
                  }
                  return {ok: el.value === value, value: el.value};
                }""",
                selector,
                value,
                timeout=2,
            )
            if isinstance(result, dict) and result.get("ok"):
                print(f"  ✓ {desc}: {value}")
                return True
            last_error = result
        except Exception as exc:
            last_error = exc
        time.sleep(0.08)
    print(f"  ✗ {desc} 失败: {last_error}")
    return False

def click_btn(page, selector, desc):
    deadline = time.time() + 5.0
    last_error = None
    while time.time() < deadline:
        try:
            result = page.run_js(
                r"""function(selector) {
                  const el = document.querySelector(selector);
                  if (!el) return {ok:false, reason:'not-found'};
                  if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                    return {ok:false, reason:'disabled'};
                  }
                  el.click();
                  return {ok:true};
                }""",
                selector,
                timeout=2,
            )
            if isinstance(result, dict) and result.get("ok"):
                print(f"  ✓ 点击 {desc}")
                return True
            last_error = result
        except Exception as exc:
            last_error = exc
        time.sleep(0.08)
    print(f"  ✗ {desc} 失败: {last_error}")
    return False


def advance_after_click(page, previous_step, timeout=8.0):
    """点击后快速等待状态变化，避免固定 sleep；captcha/成功页也视为有效转移。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = get_step(page)
        if step != previous_step:
            return step
        if registration_success_snapshot(page).get("success"):
            return "success"
        time.sleep(0.08)
    return get_step(page)


def run_step_state_machine(page, max_iterations=32, stagnant_timeout=18.0):
    """按页面状态分派步骤；短暂识别不到页面时轮询，避免旧步骤重复提交。"""
    previous_step = None
    stagnant_since = None
    for step_count in range(max_iterations):
        current = get_step(page)
        if current == 'unknown':
            deadline = time.time() + 2.0
            while current == 'unknown' and time.time() < deadline:
                time.sleep(0.08)
                current = get_step(page)
            if current == 'unknown':
                print(f"⚠️ 状态机无法识别页面，URL={getattr(page, 'url', '')}")
                wait_user("检查页面状态后按回车继续检测...")
                previous_step = None
                continue

        if current == previous_step:
            if stagnant_since is None:
                stagnant_since = time.monotonic()
        else:
            stagnant_since = time.monotonic()
        if stagnant_since is not None and time.monotonic() - stagnant_since >= stagnant_timeout:
            print(f"⚠️ 页面连续 {stagnant_timeout:.0f} 秒停留在 {current}，停止重复提交；URL={getattr(page, 'url', '')}")
            return False
        previous_step = current
        yield step_count, current


def registration_success_snapshot(page, expected_email: str = "") -> dict[str, Any]:
    script = r"""function(expectedEmail) {
      const text = document.body ? (document.body.innerText || '') : '';
      const lower = text.toLowerCase();
      const hasAllSet = /you['’]?re\s+all\s+set|all\s+set/i.test(text);
      const hasCreated = /account\s+has\s+been\s+created|has\s+been\s+created/i.test(text);
      const hasDownload = /download\s+battle\.net\s+app/i.test(text);
      const hasHumanityTitle = /detecting\s+humanity/i.test(text);
      const hasHumanityPrompt = /only\s+humans\s+are\s+allowed\s+to\s+create\s+battle\.net\s+accounts/i.test(text)
        && /i\s+am\s+a\s+human/i.test(text);
      // 注意：Detecting Humanity / Only humans… / I am a human 是 captcha-gate
      // 这一步的固定文案，正常等待求解时也成立，因此不能单独作为"失败"判据。
      // 真正的失败态看 gate 自己的错误标记。
      const meta = document.querySelector('#step-meta-data');
      const gateHasErrors = !!meta && meta.getAttribute('data-step-has-errors') === 'true';
      const errBox = document.querySelector('#capture-error-captcha-container');
      const errText = errBox ? (errBox.innerText || errBox.textContent || '') : '';
      const gateRejected = gateHasErrors || /weren['’]?t able to verify|unable to verify/i.test(errText);
      const hasHumanityGate = hasHumanityTitle || hasHumanityPrompt;
      const successIcon = document.querySelector('#success-icon');
      const hasIcon = !!(successIcon && (successIcon.querySelector('svg,path') || successIcon.offsetWidth || successIcon.offsetHeight));
      const hasEmail = !expectedEmail || lower.includes(String(expectedEmail).toLowerCase());
      return {success: !!(hasIcon || ((hasAllSet && (hasCreated || hasDownload)) && hasEmail)), hasAllSet, hasCreated, hasDownload, hasIcon, hasEmail, hasHumanityGate, hasHumanityTitle, hasHumanityPrompt, gateRejected, gateHasErrors, gateErrorText: errText.slice(0, 300), url:location.href, sample:text.slice(0,500)};
    }"""
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            result = context.run_js(script, expected_email, timeout=4)
            if isinstance(result, dict):
                return result
    return {"success": False, "reason": "page inspection failed"}


def extract_captcha_site_key(page) -> dict[str, Any]:
    """从当前 captcha-gate 页读 Arkose sitekey（原生兜底用）。

    站点把 sitekey 放在 ``data-captcha-src``（``//host/v2/<PK>/api.js``）里，
    ``data-captcha-integrity-token`` 就是随本次 gate 下发的 blob。
    """
    script = r"""return (() => {
      const nodes = document.querySelectorAll('[data-captcha-src],[data-captcha-integrity-token],#capture-captcha,input[name="captcha"]');
      // provider 决定后续能否求解：ARKOSE 走图片分类，KASADA 是浏览器内 PoW，
      // 打码平台不支持。这里一并读出，便于每轮运行直接看到分流结果。
      const cap = document.querySelector('#capture-captcha,input[name="captcha"]');
      const provider = cap ? (cap.getAttribute('data-captcha-provider') || '') : '';
      const autoEnforce = cap ? (cap.getAttribute('data-captcha-auto-enforcement') || '') : '';
      for (const el of nodes) {
        const src = el.getAttribute('data-captcha-src') || '';
        const pk = el.getAttribute('data-captcha-public-key') || '';
        const blob = el.getAttribute('data-captcha-integrity-token') || '';
        const m = src.match(/\/v2\/([0-9A-Fa-f-]{16,})\/api\.js/);
        const surl = src ? (src.replace(/^\/\//,'').split('/')[0] || '') : '';
        if (src || pk || blob) return {src, publicKey: pk || (m ? m[1].toUpperCase() : ''), blob, surl, provider, autoEnforce};
      }
      return {src:'', publicKey:'', blob:'', surl:'', provider, autoEnforce};
    })();"""
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            result = context.run_js(script, timeout=3)
            if isinstance(result, dict) and (
                result.get("src") or result.get("publicKey") or result.get("blob") or result.get("provider")
            ):
                return result
    return {}


def refresh_gate_blob(page, identity: dict[str, Any], consumed_blobs: Iterable[str] = (),
                      timeout: float = 45.0) -> str:
    """在 captcha-gate 上重走一次流程，取回一个**未被消费过**的 blob。

    harness 已经用掉一个 blob。Arkose 的 ``data-captcha-integrity-token`` 是随
    本次 gate 下发的会话票据，拿同一个再跑一遍不算独立验证——若原页也失败，就
    分不清是"realm 有问题"还是"这个 blob 早就死了"。所以原页兜底前必须换一个
    新的：点 ``Go back`` 退回上一步再重新提交，让站点重新下发 token。

    站点若已自行刷新过 gate（DOM 里已是一个没被消费的 blob），直接采用。
    拿不到新 blob 就抛错，绝不复用旧值——复用得到的结论没有意义。
    """
    before = extract_captcha_site_key(page).get("blob") or ""
    seen = set(consumed_blobs)
    if before and before not in seen:
        print(f"\n原页 DOM 里已是不曾被消费的 blob（长度={len(before)}），无需重走流程")
        return before
    print(f"\n为原页兜底换取新 blob（当前 DOM 长度={len(before)}，已消费 {len(seen)} 个）")
    if not click_btn(page, '#flow-button-back', 'Go back（退出本次 gate）'):
        raise RuntimeError("换取新 blob 失败：找不到 #flow-button-back，无法重走流程")

    deadline = time.time() + 15.0
    step = 'captcha-gate'
    while time.time() < deadline:
        step = get_step(page)
        if step not in ('captcha-gate', 'unknown'):
            break
        time.sleep(0.1)
    print(f"  退回后步骤: {step}")
    if step in ('captcha-gate', 'unknown'):
        raise RuntimeError(f"换取新 blob 失败：退回后仍停在 {step}（url={page.url}）")
    if step == 'set-battletag':
        current_tag = page.run_js('return document.querySelector("#capture-battletag")?.value || ""')
        if current_tag != identity.get("battletag"):
            if not input_text(page, '#capture-battletag', identity['battletag'], 'BattleTag'):
                raise RuntimeError("换取新 blob 失败：退回后无法重填 BattleTag")
    if not click_btn(page, '#flow-form-submit-btn', '重新提交（再次进入 captcha-gate）'):
        raise RuntimeError("换取新 blob 失败：重新提交失败")
    if not wait_for_step(page, 'captcha-gate', timeout=20.0):
        raise RuntimeError(f"换取新 blob 失败：重新提交后未回到 captcha-gate（当前={get_step(page)}）")

    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        new_blob = extract_captcha_site_key(page).get("blob") or ""
        if new_blob and new_blob != before:
            return new_blob
        time.sleep(0.2)
    state = registration_gate_state(page)
    raise RuntimeError(
        f"原页未能取得新的 blob（仍是长度 {len(before)} 的旧值）；gate={state}。"
        "旧 blob 已被求解页消费，复用无法作为独立验证。"
    )


def is_terminal_gate_rejection(state: Mapping[str, Any]) -> bool:
    """判断 gate 是否已进入"服务端判定会话不可信"的终态。

    实测证据（runs/run_20261011_134037）：当 gate 报 ``rejected`` 且
    ``hasArkoseIframe`` 为假时，求解器在四轮尝试里全部失败——

    - native 第 0 轮未捕获到题图（Arkose 根本没出题）
    - 点 Go back 重走流程后 blob **仍是旧值**，拿不到新会话票据
    - harness 第 6 轮未捕获到题图
    - harness 重试报 ``POW_FAILED`` 且 ``recoverable: False``

    四张诊断截图形态一致：挑战框体在、加载动画在转，题目永远不下发。
    即 Arkose 侧明确拒绝为该会话出题，**重试同一会话不会好转**。

    这类终态与"偶发失败"必须区分：前者应换会话（由上层重跑整个流程），
    后者才值得就地重试。此前把两者混为一谈，会在注定失败的路径上白跑
    三次尝试。
    """

    if not state:
        return False
    # 只有"已判拒绝"才算终态；仅停在 gate 等求解不算。
    if not state.get("rejected"):
        return False
    # 若 Arkose 帧/容器还在，说明还有出题机会，不能判死。
    if state.get("hasArkoseIframe") or state.get("hasArkoseContainer"):
        return False
    return True


def registration_gate_state(page) -> dict[str, Any]:
    """区分"停在 captcha-gate 等求解"和"服务端已判 token 无效"。

    ``Data-step-has-errors="true"`` 与 ``#capture-error-captcha-container`` 里的
    错误文案才是真失败态。``Detecting Humanity`` / ``Only humans…`` / ``I am a
    human`` 只是该步骤的固定文案，不能用来判定失败，否则任何停在 captcha-gate
    的页面都会被误判成"需要人工处理"而挡住重试。
    """
    script = r"""return (() => {
      const meta = document.querySelector('#step-meta-data');
      const stepId = meta ? (meta.getAttribute('data-step-id') || '') : '';
      const hasErrors = !!meta && meta.getAttribute('data-step-has-errors') === 'true';
      const errBox = document.querySelector('#capture-error-captcha-container');
      const errText = errBox ? (errBox.innerText || errBox.textContent || '') : '';
      const rejected = hasErrors || /weren['’]?t able to verify|unable to verify|try again/i.test(errText);
      return {
        stepId,
        hasErrors,
        rejected,
        errorText: errText.slice(0, 300),
        hasArkoseIframe: !!document.querySelector('iframe[src*="arkoselabs.com"],iframe[title*="challenge"],iframe[data-e2e="enforcement-frame"]'),
        hasArkoseContainer: !!document.querySelector('#arkose-container'),
        hasArkoseScript: !!document.querySelector('script#arkose-script,script[data-callback*="arkose"]'),
        url: location.href,
      };
    })();"""
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            result = context.run_js(script, timeout=3)
            if isinstance(result, dict):
                return result
    return {}


def inject_native_enforcement(page) -> dict[str, Any]:
    """在**原注册页同一 document/realm** 内，用站点自己的 Arkose api.js 渲染。

    这里不新建 tab、不用 ``document.write`` 伪造文档——step 3 的求解页正是因此
    与提交页不同 realm，token 才不被服务端接受。真实 captcha-gate 页里：

      <script id="arkose-script" src="//…/v2/<PK>/api.js" data-callback="blz-arkose-callback">
      <input id="capture-captcha" data-captcha-src="//…/v2/<PK>/api.js"
             data-captcha-integrity-token="<blob>">

    注意页面里**没有** ``#arkose-container``，站点也不调用 ``setupEnforcement``，
    因此正确的做法是包装站点声明在 ``data-callback`` 上的那个回调，重新加载
    同一个 api.js 让它再次回调我们，然后由我们在同一 realm 里 setConfig + run。
    """
    script = r"""function() {
      const FALLBACK_CALLBACK = '__NATIVE_CALLBACK_FALLBACK__';
      const HOST_ID = 'ruyi-arkose-host';
      const state = window.__ARKOSE_NATIVE__ = window.__ARKOSE_NATIVE__ || {
        apiReady: false, runCalled: false, token: null, tokenLength: 0,
        status: 'boot', error: null, events: [], callbackName: null, mode: null,
      };
      const emit = (name, payload) => {
        state.status = name;
        try { (state.events = state.events || []).push({name, at: Date.now(), payload: payload === undefined ? null : payload}); } catch (e) {}
        try { console.log('[ARKOSE-NATIVE]', name, payload || ''); } catch (e) {}
      };

      // 站点已经自己渲染出 challenge：直接复用，什么都别做。
      const siteFrame = document.querySelector(
        'iframe[src*="arkoselabs.com"],iframe[data-e2e="enforcement-frame"],iframe[title*="challenge"]');
      if (siteFrame) { state.mode = 'site-iframe'; emit('site-iframe-present', null); return {ok: true, mode: 'site-iframe'}; }

      // 参数一律取自页面自身的 data-captcha-*，不凭外部记忆拼。
      const holder = document.querySelector('[data-captcha-src],#capture-captcha,input[name="captcha"]');
      const src = (holder && holder.getAttribute('data-captcha-src')) || '';
      const blob = (holder && holder.getAttribute('data-captcha-integrity-token')) || '';
      const matched = src.match(/\/v2\/([0-9A-Fa-f-]{16,})\/api\.js/);
      const pk = (holder && holder.getAttribute('data-captcha-public-key')) || (matched ? matched[1].toUpperCase() : '');
      const surl = src ? (src.replace(/^\/\//, '').split('/')[0] || '') : '';
      if (!pk || !surl) return {ok: false, reason: 'no-public-key-or-surl', src: src};
      const apiUrl = 'https://' + surl + '/v2/' + pk + '/api.js';

      // 回调名：站点自己的写法优先。
      const siteScript = document.querySelector('script#arkose-script')
        || document.querySelector('script[src*="/v2/"][data-callback]');
      const callbackName = (siteScript && siteScript.getAttribute('data-callback')) || FALLBACK_CALLBACK;
      state.callbackName = callbackName;

      // Arkose inline 需要可见容器；插在 captcha 字段所在表单内。
      let host = document.getElementById(HOST_ID);
      if (!host) {
        host = document.createElement('div');
        host.id = HOST_ID;
        host.style.cssText = 'width:320px;min-height:340px;position:relative;';
        const anchor = document.querySelector('form#flow-form,form[action*="captcha-gate"],#capture-captcha');
        (anchor && anchor.parentNode ? anchor.parentNode : document.body).appendChild(host);
        emit('host-created', HOST_ID);
      }

      let enforcement = null;
      const runIt = () => {
        if (!enforcement) return emit('run-before-ready', null);
        try { state.runCalled = true; enforcement.run(); emit('run', null); }
        catch (e) { state.error = String(e && (e.stack || e.message) || e); emit('run-error', state.error); }
      };

      // 包装站点回调：先让站点原有逻辑跑，它没跑起来才由我们 setConfig。
      const previous = window[callbackName];
      window[callbackName] = function (myEnforcement) {
        enforcement = myEnforcement;
        state.apiReady = true;
        emit('callback', {hadPrevious: typeof previous === 'function'});
        if (typeof previous === 'function') {
          try { previous.apply(this, arguments); } catch (e) { emit('previous-callback-error', String(e)); }
        }
        if (state.runCalled) return;
        try {
          myEnforcement.setConfig({
            publicKey: pk,
            selector: '#' + HOST_ID,
            mode: 'inline',
            language: 'en-US',
            data: blob ? {blob: blob} : {},
            onReady: r => { emit('onReady', r); if (!state.runCalled) setTimeout(runIt, 100); },
            onShow: r => emit('onShow', r),
            onShown: r => emit('onShown', r),
            onHide: r => emit('onHide', r),
            onReset: r => { state.runCalled = false; emit('onReset', r); },
            onWarning: r => emit('onWarning', r),
            onFailed: r => { state.error = r; emit('onFailed', r); },
            onError: r => { state.error = r; emit('onError', r); },
            onCompleted: r => {
              const token = r && (r.token || r.arkose || r.captcha);
              state.token = token ? String(token) : null;
              state.tokenLength = state.token ? state.token.length : 0;
              emit('onCompleted', {tokenLength: state.tokenLength});
            },
          });
          emit('setConfig', {selector: '#' + HOST_ID});
        } catch (e) {
          state.error = String(e && (e.stack || e.message) || e);
          emit('setConfig-error', state.error);
        }
      };

      // 重载同一 api.js：确保上面的包装回调会被调用一次。站点脚本若已执行过，
      // 光包装是等不到第二次回调的。
      const stale = document.querySelector('script#native-arkose-script,script#arkose-script');
      if (stale) {
        stale.setAttribute('data-callback', callbackName);
        try { stale.remove(); } catch (e) {}
      }
      const inject = document.createElement('script');
      inject.id = 'native-arkose-script';
      inject.src = apiUrl;
      inject.async = true;
      inject.defer = true;
      inject.setAttribute('data-callback', callbackName);
      inject.onerror = () => { state.error = 'api.js load failed'; emit('script-error', state.error); };
      document.head.appendChild(inject);
      state.mode = siteScript ? 'wrap-site-callback' : 'load-own-callback';
      emit('script-added', {apiUrl: apiUrl, callbackName: callbackName, mode: state.mode});
      return {ok: true, mode: state.mode, apiUrl: apiUrl, callbackName: callbackName, blobLength: blob.length};
    }""".replace("__NATIVE_CALLBACK_FALLBACK__", NATIVE_ARKOSE_CALLBACK_FALLBACK)
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            result = context.run_js(script, timeout=10)
            if isinstance(result, dict) and result.get("ok"):
                return result
    return {"ok": False}


def wait_native_enforcement_ready(page, timeout: float = NATIVE_ARKOSE_READY_TIMEOUT) -> Optional[str]:
    """等 Arkose 在原页上就绪；返回就绪方式字符串，超时返回 None。"""
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        state = registration_gate_state(page)
        if state.get("hasArkoseIframe"):
            return "site-iframe"
        ready = run_js_any_context(
            page,
            r"""return (() => {
              const s = window.__ARKOSE_NATIVE__ || null;
              const host = document.getElementById('ruyi-arkose-host');
              const frame = document.querySelector('iframe[src*="arkoselabs.com"],iframe[data-e2e="enforcement-frame"],iframe[title*="challenge"]');
              return {ok: !!(s && s.apiReady) || !!frame, mode: (s && s.mode) || null,
                      hostPresent: !!host, framePresent: !!frame};
            })();""",
            timeout=2,
        )
        if ready:
            return str(ready.get("mode") or "injected")
        time.sleep(0.25)
    return None


def wait_verify_button(solver_tab: Any, *extra_roots: Any, timeout: float = 20.0) -> bool:
    """等 Arkose 的 Verify 按钮**真正渲染并可见**，再交给后续流程。

    ``extra_roots`` 位置、``timeout`` keyword-only。顺序不能反：反过来的签名
    会让 ``*extra`` 展开的位置参数抢占 ``timeout`` 槽位，抛
    ``TypeError: got multiple values for argument 'timeout'``——真机上 harness
    路径正是这样一进门就崩。详见 ``click_verify`` 的同款说明。

    原先只看 iframe / apiReady 就算就绪，但那一刻 Arkose 的 React 树还没挂载完，
    iframe 里连按钮都还不存在。抢在前面去点是必然失败的（表现为"停在 Verification
    卡片上不动"）。这里改为轮询"按钮是否已出现且可见"，就绪了才返回。

    判据与 ``click_verify`` 保持一致：``data-theme=home.verify*`` 或挑战 iframe 内
    的 verify 文案。只探测、不点击，避免在等待阶段误触。
    """
    script = r"""return (() => {
      const roots=[document], seen=new Set();
      const visible=el=>!!el && !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
      const inArkoseFrame=el=>{
        try {
          let w = el.ownerDocument && el.ownerDocument.defaultView;
          if (!w || w === window) return false;
          const href = String(w.location.href || '');
          return /ec-game-core|index\.html\?session=|arkoselabs\.com\/fc/i.test(href);
        } catch(e) { return false; }
      };
      const VERIFY_TEXT=/^\s*(verify|验证)\s*$/i;
      const ANY_VERIFY=/verify|验证/i;
      const THEME_VERIFY=/^home\.verify\w*$|(^|[._-])verify([._-]|$)/i;
      for(let i=0;i<roots.length;i++){
        const root=roots[i]; if(!root||seen.has(root)) continue; seen.add(root);
        try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot) roots.push(el.shadowRoot);});}catch(e){}
        let els=[]; try{els=Array.from(root.querySelectorAll('button,[role="button"],[data-theme]'));}catch(e){}
        for(const el of els){
          if (!visible(el)) continue;
          if ((el.id||'') === 'flow-form-submit-btn') continue;
          try { if (el.closest && el.closest('form#flow-form,form[action*="captcha-gate"]')) continue; } catch(e) {}
          const theme=(el.getAttribute('data-theme')||'').trim();
          const text=((el.innerText||'')+' '+(el.getAttribute('aria-label')||'')).replace(/\s+/g,' ').trim();
          const inFrame=inArkoseFrame(el);
          if ((THEME_VERIFY.test(theme) && (inFrame || ANY_VERIFY.test(text)))
              || (inFrame && VERIFY_TEXT.test(text))
              || (inFrame && ANY_VERIFY.test(text))) {
            return {ok:true, theme, text, inFrame};
          }
        }
      } return {ok:false}; })();"""
    deadline = time.time() + max(0.5, timeout)
    waited = False
    rounds = 0
    ctx_counts: dict[int, int] = {}
    saw_arkose_frame = False
    # 轮询统计：超时时必须能回答"25 秒里到底枚举到了几个 context、有没有 Arkose 帧"。
    # 真机上出现过 page 上按钮 +6s 就存在、这里却等满 25 秒的矛盾，只打最终状态
    # 无法区分"帧没枚举到"和"枚举到了但判据没命中"——两种原因的修法完全不同。
    while time.time() < deadline:
        rounds += 1
        # token 已经出来的话，按钮就不用等了。
        if solver_state(solver_tab).get("token"):
            print(f"  ✓ token 已产出，无需再等按钮（第 {rounds} 轮）")
            return True
        ctxs = all_contexts(solver_tab, *extra_roots)
        ctx_counts[len(ctxs)] = ctx_counts.get(len(ctxs), 0) + 1
        for _c in ctxs:
            with contextlib.suppress(Exception):
                _u = str(getattr(_c, "url", "") or "").lower()
                if "arkose" in _u or "ec-game-core" in _u:
                    saw_arkose_frame = True
        hit = run_js_any_context(solver_tab, script, timeout=2, extra_roots=extra_roots)
        if hit:
            print(f"  ✓ Arkose Verify 按钮已渲染 ({hit.get('theme') or hit.get('text')!r})")
            return True
        if not waited:
            print(f"  等待 Arkose Verify 按钮渲染... (最多 {int(timeout)} 秒)")
            waited = True
        time.sleep(0.3)
    print(f"  ✗ 等待 {int(timeout)} 秒后 Verify 按钮仍未出现")
    print(f"     轮询 {rounds} 次；每轮枚举到的 context 数分布={ctx_counts}；"
          f"曾出现 Arkose 帧={saw_arkose_frame}")
    print(f"     （若 context 数恒为 1 且未见 Arkose 帧 → 帧没枚举到；"
          f"若曾见 Arkose 帧 → 是判据没命中）")
    return False


def auto_solve_native_tab(page: Any, catcher: RuyiArkoseImageCatcher, out: Path) -> str:
    """兜底：在原注册页让 Arkose 自己渲染，逐轮用上一套图流程解，提交后读 token。

    与 ``auto_solve_yescaptcha_tab`` 的差别只在"答案怎么落地"——这里必须点
    Arkose 自己 DOM 里的按钮，而不是取消解器页面上的钩子；其余（題图捕获、
    题干读取、YesCaptcha 分类、箭头、Submit）复用同一批函数。
    """
    reset_image_catcher(catcher)
    seen: set[str] = set()
    images_dir = out / "yescaptcha_images"
    actions: list[dict[str, Any]] = []
    token = wait_native_token(page, 1.0)
    if token:
        return token
    # 原页上 Arkose 是站点自己加载的，iframe 出现 ≠ 按钮已挂载；先等按钮渲染出来。
    if not wait_verify_button(page, timeout=25.0):
        print("  ⚠️ 原页 Verify 按钮迟迟没渲染出来；下面仍尝试点一次")
    if not click_verify(page, timeout=20.0):
        print("  ⚠️ 未点到 Arkose Verify 按钮；若接下来抓不到题图，问题就在这里")

    for wave in range(12):
        token = wait_native_token(page, 0.8)
        if token:
            return token
        kind, value = wait_image_or_token(catcher, seen, 30.0 if wave else 45.0, page)
        if kind == "token":
            return str(value)
        if not value:
            token = wait_native_token(page, 20.0)
            if token:
                return token
            raise RuntimeError(f"原生求解第 {wave} 轮未捕获到新的 Arkose 题图")
        record = value
        data = record.get("body_bytes") or b""
        sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
        seen.add(sha)
        image_path = save_image_record(record, images_dir, wave, prefix="native")
        question = extract_dynamic_prompt(page, timeout=5.0)
        (images_dir / f"native_wave_{wave:02d}_question.json").write_text(
            json.dumps(question, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        answer, api_result = classify_image(
            image_path, question=question["prompt"],
            api_key=os.getenv("YESCAPTCHA_API_KEY", YESCAPTCHA_API_KEY).strip(),
        )
        print(f"  原生兜底 YesCaptcha 题目: {question['prompt']}")
        print(f"  原生兜底 YesCaptcha 答案: {answer}")
        if not click_next_n(page, answer):
            raise RuntimeError(f"原生求解第 {wave} 轮无法点击下一张箭头 {answer} 次")
        time.sleep(0.08 + random.random() * 0.08)
        submit_ok = click_solver_submit(page)
        actions.append({
            "wave": wave, "image": str(image_path), "sha256": sha, "answer": answer,
            "question": question, "submit": submit_ok, "yescaptcha": api_result, "native": True,
        })
        (out / "yescaptcha_actions_latest.json").write_text(
            json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not submit_ok:
            raise RuntimeError(f"原生求解第 {wave} 轮 Submit 点击失败")
        token = wait_native_token(page, 3.0)
        if token:
            return token

    token = wait_native_token(page, 30.0)
    if token:
        return token
    raise RuntimeError("原生 Arkose 求解超过最大轮次仍未返回 token")


def inject_token_and_submit(page: Any, token: str) -> dict[str, Any]:
    """把 token 写进原注册页的 captcha 字段并提交表单。"""
    script = r"""function(token) {
      const form = document.querySelector('form#flow-form') || document.querySelector('form[action*="captcha-gate"]') || document.querySelector('form');
      if (!form) return {ok:false, reason:'no-form'};
      const input = form.querySelector('input[name="arkose"],input[name="captcha"]') || document.querySelector('#capture-arkose,input[name="arkose"],input[name="captcha"]');
      if (!input) return {ok:false, reason:'no-arkose-or-captcha-input'};
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')?.set;
      if (setter) setter.call(input, token); else input.value=token;
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
      input.dispatchEvent(new Event('blur',{bubbles:true}));
      const submit = document.querySelector('#flow-form-submit-btn') || form.querySelector('button[type="submit"],input[type="submit"]');
      if (typeof form.requestSubmit === 'function') form.requestSubmit(submit || undefined);
      else if (submit) submit.click(); else form.submit();
      return {ok:true, tokenSet:input.value===token, inputId:input.id||null, inputName:input.name||null, method:'requestSubmit'};
    }"""
    for context in all_contexts(page):
        with contextlib.suppress(Exception):
            result = context.run_js(script, token, timeout=15)
            if isinstance(result, dict) and result.get("ok"):
                return result
    return {"ok": False, "reason": "inject failed in all contexts"}


def wait_captcha_gate_outcome(page: Any, expected_email: str, timeout: float = 30.0) -> str:
    """提交 token 后判定结果：'success' | 'rejected' | 'pending'。

    rejected 的判据是 gate 自身进入带错误状态，而不是页面出现 Detecting
    Humanity——后者是正常等待态，几乎立刻就有。
    """
    deadline = time.time() + timeout
    last_state: dict[str, Any] = {}
    while time.time() < deadline:
        if registration_success_snapshot(page, expected_email).get("success"):
            return "success"
        state = registration_gate_state(page)
        if state:
            last_state = state
            if state.get("rejected"):
                return "rejected"
            if state.get("stepId") and state.get("stepId") != "captcha-gate":
                # 已经离开 captcha-gate，交给成功页检测收尾。
                if registration_success_snapshot(page, expected_email).get("success"):
                    return "success"
                return "pending"
        time.sleep(0.4)
    print(f"captcha-gate 结果等待超时，最后状态: {last_state}")
    return "pending"


def save_registration_diagnostics(page: Any, out: Optional[Path], snapshot: dict[str, Any], prefix: str = "humanity_gate") -> None:
    """保存验证页证据，便于区分 token 无效、页面重载和前端状态问题。"""
    if not out:
        return
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        (out / f"{prefix}_snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    with contextlib.suppress(Exception):
        html = page.html
        if callable(html):
            html = html()
        (out / f"{prefix}.html").write_text(str(html or ""), encoding="utf-8")
    with contextlib.suppress(Exception):
        page.screenshot(str(out / f"{prefix}.png"))


def save_solver_diagnostics(solver_tab: Any, out: Optional[Path], prefix: str = "solver_blank") -> None:
    """把求解标签页的真实状态落盘，用于判断"空白页"卡在哪一步。

    单独写一份、不复用 ``save_registration_diagnostics``，是因为空白页最需要的
    是**运行时状态**而不是注册页快照：harness 有没有执行、api.js 有没有加载、
    挑战 iframe 有没有出现。只有这些才能区分三种截然不同的空白：

    * ``harness-missing``   —— document.write 没生效，页面是别的文档；
    * ``harness-ran``       —— 脚本执行了，但 api.js 没加载出来；
    * ``iframe-missing``    —— api.js 加载了，但 Arkose 没渲染挑战。
    """
    if not out:
        return
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    probe = r"""return (() => {
      const s = window.__ARKOSE_MANUAL__ || null;
      const host = document.getElementById('arkose-container');
      const scripts = Array.from(document.scripts || []).map(e => ({
        src: e.getAttribute('src') || '', cb: e.getAttribute('data-callback') || '',
        readyState: e.readyState || '',
      }));
      const frames = Array.from(document.querySelectorAll('iframe')).map(f => ({
        src: f.getAttribute('src') || '', id: f.id || '', title: f.getAttribute('title') || '',
        w: f.offsetWidth || 0, h: f.offsetHeight || 0,
      }));
      return {
        url: location.href, origin: location.origin,
        title: document.title,
        readyState: document.readyState,
        bodyLength: (document.body && document.body.innerHTML || '').length,
        bodyHead: (document.body && document.body.innerHTML || '').slice(0, 600),
        harnessRan: !!s,
        state: s ? {apiReady:s.apiReady, runCalled:s.runCalled, status:s.status,
                    tokenLength:s.tokenLength||0, error:s.error,
                    eventCount:(s.events||[]).length,
                    events:(s.events||[]).slice(-12)} : null,
        hostPresent: !!host,
        hostChildren: host ? host.children.length : -1,
        hostHTML: host ? (host.innerHTML || '').slice(0, 400) : '',
        scripts, frames, frameCount: frames.length,
      };
    })();"""
    info: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        got = solver_tab.run_js(probe, timeout=8)
        if isinstance(got, dict):
            info = got
    # 兜底：harness 脚本可能压根没执行，上面那个探针本身也会失败。
    if not info:
        with contextlib.suppress(Exception):
            info = {"probeFailed": True, "url": solver_tab.url, "title": solver_tab.title}
    with contextlib.suppress(Exception):
        (out / f"{prefix}_solver.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with contextlib.suppress(Exception):
        html = solver_tab.html
        if callable(html):
            html = html()
        (out / f"{prefix}.html").write_text(str(html or ""), encoding="utf-8")
    with contextlib.suppress(Exception):
        solver_tab.screenshot(str(out / f"{prefix}.png"))
    # harness 自己打的 [ARKOSE-RUYI] 事件已随上面探针的 state.events 落盘；
    # 不再单独抓控制台——ruyipage 的 page.console 需要事先 start() 才能收到，
    # 失败后才订阅拿不到任何东西。
    verdict = "harness-missing"
    if info.get("harnessRan"):
        verdict = "iframe-missing" if not info.get("frameCount") else "harness-ran"
    st = info.get("state") or {}
    print(f"  求解页诊断: {verdict} | url={info.get('url')} | bodyLength={info.get('bodyLength')} "
          f"| host={info.get('hostPresent')} | frames={info.get('frameCount')} | status={st.get('status')}")
    if st.get("events"):
        print(f"  求解页事件: {' -> '.join(str(e.get('name')) for e in st.get('events') or [])}")
    if not info.get("harnessRan"):
        print("  ⇒ 求解页上的 harness 脚本没有执行（__ARKOSE_MANUAL__ 不存在）。"
              "多半是原站 CSP 拦掉了 document.write 注入的 inline script。")


def wait_direct_registration_result(page, expected_email: str, timeout: float = 8.0) -> Optional[bool]:
    """BattleTag 提交后区分：直接成功、验证码页、或尚未完成。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = registration_success_snapshot(page, expected_email)
        if snapshot.get("success"):
            print(f"✓ BattleTag 提交后直接注册成功: {snapshot}")
            return True
        step = get_step(page)
        if step == "captcha-gate":
            print("✓ BattleTag 提交后进入 captcha-gate")
            return False
        time.sleep(0.25)
    return None


def check_box(page, name):
    selector = f'input[type="checkbox"][name="{name}"]'
    page.run_js(f'''
        (function() {{
            const cb = document.querySelector('{selector}');
            if (cb) {{
                cb.checked = true;
                cb.dispatchEvent(new Event('change', {{bubbles:true}}));
            }}
        }})();
    ''')
    is_checked = page.run_js(f'document.querySelector(\'{selector}\')?.checked || false', timeout=2)
    print(f"  {'✓' if is_checked else '✗'} Checkbox {name}: {is_checked}")
    return is_checked


# ==================== 主流程 ====================

def resolve_firefox_path(explicit: str = "") -> str:
    """定位 Firefox 可执行文件。

    查找顺序：

    1. ``--firefox-path`` 显式指定（也可用 ``BATTLENET_FIREFOX_PATH`` 环境变量）
    2. 本目录下捆绑的 ``firefox/firefox.exe``（随包分发时使用）
    3. ruyipage 自带的 ``resolve_firefox_path()``

    之所以需要第 1 条：某些受限环境（含容器化 workspace）里，位于工作区
    内部的子进程无法通过 Windows 作业对象限制创建自身的 GPU/内容进程，
    Firefox 会以退出码 1 静默结束，表现为 ruyipage 报
    ``BrowserConnectError: 启动后无法连接到 127.0.0.1:<port>``。
    此时把浏览器放在工作区之外、用本参数指向它即可。
    """

    candidates: list[str] = []
    explicit = (explicit or os.environ.get("BATTLENET_FIREFOX_PATH", "")).strip()
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "firefox", "firefox.exe"))

    for path in candidates:
        if os.path.exists(path):
            return path

    # 回退到 ruyipage 自带的解析（通常是用户级安装的 Firefox）
    try:
        import ruyipage

        resolver = getattr(ruyipage, "resolve_firefox_path", None)
        if callable(resolver):
            resolved = resolver(None)
            if resolved:
                return str(resolved)
    except Exception:
        pass
    return ""


def auto_register(email, identity, firefox_path: str = ""):
    opts = FirefoxOptions()
    firefox_path = resolve_firefox_path(firefox_path)
    if not firefox_path:
        print("Firefox 未找到：既没有捆绑的 firefox/firefox.exe，也无法自动解析。")
        print("可用 --firefox-path <路径> 或环境变量 BATTLENET_FIREFOX_PATH 显式指定。")
        return 1
    print(f"使用 Firefox: {firefox_path}")
    opts.set_browser_path(firefox_path)

    print("\n启动浏览器...")
    page = FirefoxPage(opts)
    catcher = None
    kasada = None

    try:
        page.get("https://account.battle.net/creation/flow/creation-full", wait='interactive', timeout=30)
        if not wait_for_step(page, 'account-name', timeout=12.0):
            raise RuntimeError(f"注册起始页未就绪，当前步骤={get_step(page)}")
        # KASADA 记录器与 Arkose 捕获器同时启动，观察窗口覆盖整条流程。
        # 必须在 BattleTag 提交之前订阅，否则 p.js / ips.js 可能在
        # captcha-gate 出现前就已经加载完成，观察窗口会漏掉。
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        kasada_out = RUNS_DIR / f"kasada_{run_stamp}"
        kasada = RuyiKasadaRecorder(page, kasada_out)
        print("\n启动 KASADA 拦截记录...")
        kasada.start()
        # 必须在 BattleTag 提交之前订阅全局网络事件，否则 Arkose 的首个
        # /fc/gt2 请求可能在 captcha-gate 页面出现前就已经完成。
        catcher = RuyiArkoseCatcher(page)
        print("\n启动 Arkose 参数捕获...")
        catcher.start()

        for step_count, current in run_step_state_machine(page):
            print(f"\n[{step_count+1}] 当前: {current}")

            if current == 'account-name':
                print("→ 输入邮箱")
                if not input_text(page, '#accountName', email, '邮箱'):
                    continue
                if get_step(page) != current:
                    continue
                if click_btn(page, '#submit', 'Continue'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'get-started':
                print("→ 国家和生日")
                page.run_js(
                    """function(value) {
                      const el=document.querySelector('#capture-country');
                      if (!el) return false;
                      const setter=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value')?.set;
                      if (setter) setter.call(el,value); else el.value=value;
                      el.dispatchEvent(new Event('input',{bubbles:true}));
                      el.dispatchEvent(new Event('change',{bubbles:true}));
                      return el.value===value;
                    }""",
                    identity['country'],
                    timeout=3,
                )
                print(f"  ✓ 国家: {identity['country']}")
                if not input_text(page, 'input[name="dob-plain"]', identity['dob'], '生日'):
                    continue
                if get_step(page) != current:
                    continue
                if click_btn(page, '#flow-form-submit-btn', 'Continue'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'provide-name':
                print("→ 姓名")
                if not input_text(page, '#capture-first-name', identity['first_name'], 'First Name'):
                    continue
                if get_step(page) != current:
                    continue
                if not input_text(page, '#capture-last-name', identity['last_name'], 'Last Name'):
                    continue
                if get_step(page) != current:
                    continue
                if click_btn(page, '#flow-form-submit-btn', 'Continue'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'provide-credentials':
                print("→ 确认邮箱")
                current_email = page.run_js('document.querySelector("#capture-email")?.value || ""')
                if current_email != email:
                    if not input_text(page, '#capture-email', email, '邮箱确认'):
                        continue
                else:
                    print(f"  ✓ 邮箱一致: {email}")
                if get_step(page) != current:
                    continue
                if click_btn(page, '#flow-form-submit-btn', 'Continue'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'legal-and-opt-ins':
                print("→ 勾选协议")
                check_box(page, 'tou-agreements-implicit')
                if get_step(page) != current:
                    continue
                if click_btn(page, '#flow-form-submit-btn', 'Agree'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'set-password':
                print("→ 密码")
                if not input_text(page, '#capture-password', identity['password'], '密码'):
                    continue
                if get_step(page) != current:
                    continue
                if click_btn(page, '#flow-form-submit-btn', 'Continue'):
                    print(f"  页面状态: {current} -> {advance_after_click(page, current)}")

            elif current == 'set-battletag':
                print("→ BattleTag")
                if not input_text(page, '#capture-battletag', identity['battletag'], 'BattleTag'):
                    continue
                # 与 manual_same_browser_register_ruyipage.py 保持同样的时序：
                # BattleTag 已填写且提交按钮可用时，立即开启本次 Arkose 捕获窗口，
                # 然后再点击提交。这样不会依赖 captcha-gate 状态判断的延迟。
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    try:
                        ready = page.run_js(
                            "return (() => { const btn=document.querySelector('#flow-form-submit-btn'); return !!(btn && !btn.disabled); })();",
                            timeout=3,
                        )
                        if ready:
                            break
                    except Exception:
                        pass
                    time.sleep(0.08)
                if get_step(page) != current:
                    continue
                if not click_btn(page, '#flow-form-submit-btn', 'Continue'):
                    raise RuntimeError("BattleTag 提交失败")
                advance_after_click(page, current, timeout=4.0)
                direct_result = wait_direct_registration_result(page, email, timeout=8.0)
                if direct_result is True:
                    catcher.stop()
                    if kasada:
                        kasada.stop()
                    wait_user("查看成功页面后按回车关闭浏览器...")
                    return 0

            elif current == 'captcha-gate':
                print("\n✓ 到达验证码页面！")
                # 等 blob 的同时探测注册成功：站点并不总是先发 Arkose 请求，
                # 有时 gate 刚出现就直接放行成 "You're All Set!"，此时死等 blob
                # 会一路超时并把这次成功误报成"未捕获到 Arkose blob"。
                blob, early_success = catcher.wait_for_blob(timeout=45.0, expected_email=email)
                if early_success:
                    print(f"\n✓ 等 Arkose blob 期间页面已直接注册成功，无需求解验证码: {early_success}")
                    catcher.stop()
                    if kasada:
                        kasada.stop()
                    wait_user("查看成功页面后按回车关闭浏览器...")
                    return 0
                if not blob:
                    raise RuntimeError("未捕获到 Arkose blob，立即结束；不会调用 YesCaptcha")

                public_key = catcher.captured_pk
                if not public_key:
                    raise RuntimeError("已捕获 blob，但未捕获 Arkose sitekey，立即结束；不会调用 YesCaptcha")
                surl = normalize_surl(catcher.captured_surl or DEFAULT_SURL)
                print(f"\n最终 Arkose 参数: sitekey={public_key}, surl={surl}, blobLength={len(blob)}")

                out = RUNS_DIR / datetime.now().strftime("run_%Y%m%d_%H%M%S")
                out.mkdir(parents=True, exist_ok=True)

                def _native_attempt(attempt_index: int) -> tuple[str, str]:
                    """原生路径：在原 captcha-gate 页让站点自己的 Arkose 渲染并求解。

                    这是首选路径。实测依据：跨 realm 的 harness（document.write 换文档）
                    会重置 performance.now() 基线、document.currentScript 与脚本加载时序，
                    而这正是 Arkose PoW 校验的东西——harness 路径 6/6 全失败，原生路径
                    3 次里过了 2 次。所以 native 提到最前面，harness 只作回退。

                    返回 (结果, token)。结果取值：'success' | 'rejected' | 'pending' | 'error:<原因>'。
                    """
                    prefix = "" if attempt_index == 0 else f"native_retry{attempt_index}_"
                    print("\n★ 首选原生路径：在原注册页渲染 Arkose 求解")
                    native_state = registration_gate_state(page)

                    # 先读 provider：KASADA 是浏览器内 PoW（打码平台不支持），
                    # 走 Arkose 求解必然空转。读到即停，留证据给上层换会话。
                    gate_params = extract_captcha_site_key(page)
                    provider = str(gate_params.get("provider") or "").upper()
                    if provider:
                        print(
                            f"\n📋 captcha-gate provider={provider}"
                            f"（auto-enforcement={gate_params.get('autoEnforce') or '-'}）"
                        )
                    if provider and provider != "ARKOSE":
                        print(
                            f"\n⛔ provider={provider} 不是 Arkose，本流程无对应求解器；"
                            "跳过 Arkose 求解，留档供分析"
                        )
                        with contextlib.suppress(Exception):
                            save_registration_diagnostics(
                                page, out,
                                {**registration_success_snapshot(page, email),
                                 **native_state, "provider": provider},
                                prefix=f"{prefix}unsupported_provider".lstrip("_"),
                            )
                        return "unsupported-provider", ""

                    # 服务端已判定该会话不可信（rejected 且 Arkose 帧/容器都不在）时
                    # Arkose 不会出题，重试必然空转。实测四轮全败、四张截图同形，
                    # 这里直接判终态，交给上层换会话，不再原地重试。
                    if is_terminal_gate_rejection(native_state):
                        print(
                            "\n⛔ gate 已被服务端判为终态拒绝（无 Arkose 帧/容器），"
                            "该会话不会出题；跳过原地重试，等上层换会话"
                        )
                        with contextlib.suppress(Exception):
                            save_registration_diagnostics(
                                page, out,
                                {**registration_success_snapshot(page, email), **native_state},
                                prefix=f"{prefix}terminal_rejection".lstrip("_"),
                            )
                        return "terminal", ""

                    # 原页必须用**未被消费过**的 blob。拿一个已经被别处跑过的再跑一遍
                    # 等于没换变量。
                    try:
                        native_blob = refresh_gate_blob(page, identity, consumed_blobs)
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            save_registration_diagnostics(
                                page, out,
                                {**registration_success_snapshot(page, email), **native_state,
                                 "refreshError": str(exc)},
                                prefix=f"{prefix}native_blob_refresh_failed".lstrip("_"),
                            )
                            print("  已保存 native_blob_refresh_failed.html / .png（换 blob 失败现场）")
                        return f"error:{exc}", ""
                    consumed_blobs.add(native_blob)
                    native_params = extract_captcha_site_key(page)
                    print(f"原生参数来源: {({k: (v[:60] if isinstance(v, str) else v) for k, v in native_params.items()})}")
                    print(f"原生 blob: 长度={len(native_blob)}，与已消费的 {len(consumed_blobs)} 个均不同")

                    ready = wait_native_enforcement_ready(page)
                    print(f"原生 Arkose 就绪方式: {ready}")
                    if not ready:
                        save_registration_diagnostics(
                            page, out,
                            {**registration_success_snapshot(page, email), **native_state},
                            prefix=f"{prefix}native_unready".lstrip("_"),
                        )
                        return "error:原页上 Arkose 未能渲染", ""

                    native_catcher = RuyiArkoseImageCatcher(page)
                    native_catcher.start()
                    try:
                        native_token = auto_solve_native_tab(page, native_catcher, out)
                    except Exception as exc:
                        save_registration_diagnostics(
                            page, out,
                            {**registration_success_snapshot(page, email), **registration_gate_state(page),
                             "error": str(exc)},
                            prefix=f"{prefix}native_solve_failed".lstrip("_"),
                        )
                        return f"error:{exc}", ""
                    finally:
                        native_catcher.stop()

                    print(f"\n✓ 原生求解返回 token，长度={len(native_token)}，开始注入原注册页")
                    inject_result = inject_token_and_submit(page, native_token)
                    print(f"原生 Token 注入结果: {inject_result}")
                    if not inject_result.get("ok"):
                        return f"error:原生 Token 注入失败: {inject_result}", native_token

                    result = wait_captcha_gate_outcome(page, email, timeout=30.0)
                    return result, native_token

                def _harness_attempt(attempt_index: int, attempt_blob: str, seed: Optional[dict]) -> tuple[str, str, Optional[dict]]:
                    """开一个求解标签、解出 token、注入原页，返回 (结果, token, replay)。

                    结果取值：'success' | 'rejected' | 'pending' | 'error:<原因>'。
                    """
                    # 该 blob 一旦交给求解页，无论成功失败都不能再当"未消费"用。
                    consumed_blobs.add(attempt_blob)
                    prefix = "" if attempt_index == 0 else f"retry{attempt_index}_"
                    solver_tab = page.new_tab(background=False)
                    html = build_solver_harness(public_key, attempt_blob, surl)
                    (out / f"{prefix}solver_harness.html").write_text(html, encoding="utf-8")
                    replace_document_under_origin(solver_tab, page.url, html)
                    solver_tab.activate()
                    print(f"✓ 已在同一 RuyiPage 浏览器打开 Arkose 求解标签 (第 {attempt_index + 1} 次)")
                    solver_catcher = RuyiArkoseImageCatcher(page)
                    solver_catcher.start()
                    try:
                        solved_token, replay = auto_solve_yescaptcha_tab(
                            solver_tab, solver_catcher, out,
                            answer_prefix=prefix.rstrip("_") or "",
                            reuse_wave=seed,
                            root_page=page,
                        )
                    except Exception as exc:
                        # 页面已经打开；POW_FAILED 发生在出题之前。先留档再关标签，
                        # 否则控制台只剩一行报错，看起来像求解页根本没加载。
                        events = None
                        with contextlib.suppress(Exception):
                            events = solver_tab.run_js(
                                "return (window.__ARKOSE_MANUAL__ && window.__ARKOSE_MANUAL__.events) || [];",
                                timeout=3,
                            )
                        with contextlib.suppress(Exception):
                            save_registration_diagnostics(
                                solver_tab, out,
                                {"error": str(exc), "solverState": solver_state(solver_tab), "events": events},
                                prefix=f"{prefix}solver_failed",
                            )
                            print(f"  已保存 {prefix}solver_failed.html / .png（页面已加载，Arkose 在出题前失败）")
                        with contextlib.suppress(Exception):
                            solver_tab.close()
                        return f"error:{exc}", "", seed
                    finally:
                        solver_catcher.stop()
                    with contextlib.suppress(Exception):
                        solver_tab.close()
                    time.sleep(0.5)

                    inject_result = inject_token_and_submit(page, solved_token)
                    print(f"Token 注入结果: {inject_result}")
                    if not inject_result.get("ok"):
                        return f"error:Token 注入失败: {inject_result}", solved_token, replay

                    outcome = wait_captcha_gate_outcome(page, email, timeout=30.0)
                    return outcome, solved_token, replay

                # 首选原生路径：原页自带 arkose-script，让站点自己渲染并求解。
                # 实测 harness 跨 realm 路径 6/6 失败、原生 2/3 通过，所以 native
                # 放最前面；harness 只在 native 走不通时作回退。
                replay_wave: Optional[dict] = None
                consumed_blobs: set[str] = set()
                attempt_log: list[dict[str, Any]] = []

                outcome, token = _native_attempt(0)
                attempt_log.append({
                    "attempt": len(attempt_log) + 1, "mode": "native",
                    "outcome": outcome, "tokenLength": len(token or ""),
                })

                # 原生路径是间歇性的（实测 3 次过 2 次），偶发失败先就地重试。
                # 每次重试都会经 refresh_gate_blob 换一个未消费过的 blob，
                # 所以不是把同一个 challenge 重复跑一遍。
                #
                # 但 "terminal" / "unsupported-provider" 不是偶发失败：
                # - terminal：服务端已判该会话不可信，Arkose 不会出题
                # - unsupported-provider：分流到 KASADA 等本流程无求解器的 provider
                # 两者下本地重试与 harness 回退都是空转，直接跳出。
                if outcome in ("terminal", "unsupported-provider"):
                    print(f"\n⛔ outcome={outcome}，跳过所有本地重试与 harness 回退")
                else:
                    native_try = 1
                    while (str(outcome).startswith("error:") or outcome == "rejected") \
                            and native_try < MAX_NATIVE_ATTEMPTS:
                        print(f"\n⚠️ 原生路径失败，就地重试 ({native_try}/{MAX_NATIVE_ATTEMPTS - 1})")
                        outcome, token = _native_attempt(native_try)
                        attempt_log.append({
                            "attempt": len(attempt_log) + 1, "mode": "native-retry",
                            "outcome": outcome, "tokenLength": len(token or ""),
                        })
                        native_try += 1
                        if outcome in ("terminal", "unsupported-provider"):
                            print(f"\n⛔ 重试中被判定为 {outcome}，停止本地重试")
                            break

                # 原生路径（含重试）也走不通时才回退到 harness（跨 realm）。
                if outcome not in ("terminal", "unsupported-provider") \
                        and (str(outcome).startswith("error:") or outcome == "rejected"):
                    why = "原生求解报错" if str(outcome).startswith("error:") else "原生 token 被服务端拒绝"
                    print(f"\n⚠️ {why}，回退到 harness（同浏览器求解标签页）路径")
                    outcome, token, replay_wave = _harness_attempt(0, blob, replay_wave)
                    attempt_log.append({
                        "attempt": len(attempt_log) + 1, "mode": "harness-fallback",
                        "outcome": outcome, "tokenLength": len(token or ""),
                    })

                    # harness 自己失败（POW_FAILED 等，token 还没产出）时，同一
                    # 路径重开一次换 challenge。这不是换 realm，重开超过一次没意义。
                    attempt = 1
                    while str(outcome).startswith("error:") and attempt < MAX_HARNESS_ATTEMPTS:
                        print(f"\n⚠️ harness 求解页 Arkose 失败，重开求解页再试一次 ({attempt}/{MAX_HARNESS_ATTEMPTS - 1})")
                        fresh_blob = catcher.captured_blob or blob
                        outcome, token, replay_wave = _harness_attempt(attempt, fresh_blob, replay_wave)
                        attempt_log.append({
                            "attempt": len(attempt_log) + 1, "mode": "harness-retry",
                            "outcome": outcome, "tokenLength": len(token or ""),
                        })
                        attempt += 1

                    # 服务端拒绝已解出的 token 时，再带着当前 blob 重开求解页。
                    # Arkose 每次都会换一个 challenge，换一个通常就能被接受。
                    rejected = 0
                    while outcome == "rejected" and rejected < MAX_TOKEN_ATTEMPTS - 1:
                        print(f"\n⚠️ 服务端未接受本次 token，重开求解页重试 ({rejected + 1}/{MAX_TOKEN_ATTEMPTS - 1})")
                        fresh_blob = catcher.captured_blob or blob
                        outcome, token, replay_wave = _harness_attempt(attempt, fresh_blob, replay_wave)
                        attempt_log.append({
                            "attempt": len(attempt_log) + 1, "mode": "harness-rejected-retry",
                            "outcome": outcome, "tokenLength": len(token or ""),
                        })
                        attempt += 1
                        rejected += 1

                # 求解标签页走不通，或重试用尽仍被拒：原生路径已在最前面试过，
                # 这里不再重复。之前那段"原生兜底"（inject_native_enforcement +
                # 第二次 auto_solve_native_tab）已删除：它会在原生页跑过一遍之后
                # 再注入 enforcement，破坏原页自带的 arkose-script，反而有害。

                (out / "attempts.json").write_text(
                    json.dumps(attempt_log, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                if outcome == "success":
                    print("\n✓ 已确认注册成功页面")
                    wait_user("查看结果后按回车关闭浏览器...")
                    return 0

                snapshot = registration_success_snapshot(page, email)
                if snapshot.get("success"):
                    print("\n✓ 已确认注册成功页面")
                    wait_user("查看结果后按回车关闭浏览器...")
                    return 0
                if str(outcome).startswith("error:"):
                    raise RuntimeError(f"求解流程中断：{str(outcome)[len('error:'):]}")
                if outcome == "unsupported-provider":
                    # 分流到 KASADA 等 provider：本流程无对应求解器，Arkose 求解
                    # 与 harness 回退都不会成功。留档并整体换会话。
                    state = registration_gate_state(page)
                    params = extract_captcha_site_key(page)
                    save_registration_diagnostics(
                        page, out,
                        {**snapshot, **state, "provider": params.get("provider")},
                        prefix="unsupported_provider",
                    )
                    raise RuntimeError(
                        f"captcha-gate 分流到 provider={params.get('provider')!r}，"
                        "本流程只实现了 Arkose 求解；已保存 unsupported_provider_* "
                        "诊断文件。该会话需整体重来。"
                    )
                if outcome == "terminal":
                    # 服务端已判该会话不可信，Arkose 不出题；本地重试与 harness
                    # 回退都是空转，因此提前跳出。这不是"token 被拒"，而是整个
                    # 会话需要重来——上层应换新会话，而不是在这里继续尝试。
                    state = registration_gate_state(page)
                    save_registration_diagnostics(
                        page, out, {**snapshot, **state}, prefix="terminal_rejection"
                    )
                    raise RuntimeError(
                        "captcha-gate 已被服务端判定为终态拒绝（无 Arkose 帧/容器，"
                        f"Arkose 不下发题目）；已保存 terminal_rejection_* 诊断文件。"
                        f"该会话需整体重来（本次仅尝试 {len(attempt_log)} 次，未做无效重试）。"
                    )
                if outcome == "rejected" or snapshot.get("gateRejected"):
                    save_registration_diagnostics(
                        page, out, {**snapshot, **registration_gate_state(page)}, prefix="rejected_gate"
                    )
                    raise RuntimeError(
                        f"所有求解尝试都被服务端拒绝（共 {len(attempt_log)} 次）；"
                        "已保存 rejected_gate_* 诊断文件，需人工检查页面。"
                    )
                raise RuntimeError(f"Token 已提交，但未能确认注册结果（outcome={outcome}）")

            elif current == 'unknown':
                print("⚠️ 未知页面")
                wait_user("检查页面后按回车继续...")

        if catcher:
            catcher.stop()
        if kasada:
            kasada.stop()
        page.quit()
        return 1

    except Exception as e:
        print(f"\n✗ 错误: {e}")
        import traceback
        traceback.print_exc()
        with contextlib.suppress(Exception):
            wait_user("按回车关闭...")
        if catcher:
            catcher.stop()
        if kasada:
            kasada.stop()
        if page:
            page.quit()
        return 1


# ==================== 入口 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Battle.net 浏览器版注册全流程 + KASADA 拦截记录"
    )
    parser.add_argument(
        "--firefox-path",
        default="",
        help=(
            "Firefox 可执行文件路径。留空时优先用本目录捆绑的 "
            "firefox/firefox.exe，再回退到 ruyipage 自动解析。"
            "也可用环境变量 BATTLENET_FIREFOX_PATH 指定。"
        ),
    )
    parser.add_argument(
        "--email",
        default="",
        help="注册邮箱；留空时交互输入。",
    )
    args, _unknown = parser.parse_known_args()

    print("Battle.net 自动注册 V4 (完整版 - BiDi Blob 捕获 + YesCaptcha)")
    print("="*60)

    email = (args.email or "").strip() or input("\n请输入邮箱: ").strip()
    if not email:
        print("邮箱不能为空")
        sys.exit(1)

    identity = generate_identity()
    print(f"\n生成的身份:")
    print(f"  邮箱: {email}")
    print(f"  生日: {identity['dob']}")
    print(f"  密码: {identity['password']}")
    print(f"  名字: {identity['first_name']} {identity['last_name']}")
    print(f"  BattleTag: {identity['battletag']}")
    print(f"  国家: {identity['country']}")

    if not YESCAPTCHA_API_KEY.strip():
        print("\n⚠️ 警告: YesCaptcha API Key 未设置")
        print("请在脚本开头设置 YESCAPTCHA_API_KEY")
        input("\n按回车继续...")

    wait_user("确认信息后按回车开始注册...")

    try:
        exit_code = auto_register(email, identity, firefox_path=args.firefox_path)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(130)
