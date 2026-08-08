# -*- coding: utf-8 -*-
"""Minimal CloakBrowser adapter for the existing Arkose solver helpers."""

from __future__ import annotations

import contextlib
import hashlib
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote

from PIL import Image


class CloakElement:
    def __init__(self, locator: Any):
        self.locator = locator

    @property
    def text(self) -> str:
        with contextlib.suppress(Exception):
            return str(self.locator.inner_text(timeout=500) or "")
        return ""

    def attr(self, name: str) -> Optional[str]:
        with contextlib.suppress(Exception):
            return self.locator.get_attribute(name, timeout=500)
        return None

    def click(self) -> None:
        self.locator.click(timeout=3000)

    def is_displayed(self) -> bool:
        with contextlib.suppress(Exception):
            return bool(self.locator.is_visible(timeout=500))
        return False

    def _get_center(self) -> Optional[dict[str, float]]:
        with contextlib.suppress(Exception):
            box = self.locator.bounding_box(timeout=1000)
            if box:
                return {
                    "x": float(box["x"]) + float(box["width"]) / 2,
                    "y": float(box["y"]) + float(box["height"]) / 2,
                }
        return None


class CloakPage:
    """Expose the small RuyiPage-shaped surface used by V3 solver helpers."""

    def __init__(self, browser: Any, context: Any, raw_page: Any):
        self.browser = browser
        self.context = context
        self.raw_page = raw_page
        self._raw_context = raw_page

    @classmethod
    def frame(cls, owner: "CloakPage", raw_frame: Any) -> "CloakPage":
        value = cls.__new__(cls)
        value.browser = owner.browser
        value.context = owner.context
        value.raw_page = owner.raw_page
        value._raw_context = raw_frame
        return value

    def run_js(self, script: str, *args: Any, timeout: float = 5.0) -> Any:
        del timeout  # Playwright applies the page default timeout.
        source = str(script).strip()
        if source.startswith("function") or source.startswith("async function"):
            expression = f"(args) => ({source})(...args)"
        else:
            expression = f"(args) => (function(){{\n{source}\n}}).apply(null, args)"
        return self._raw_context.evaluate(expression, list(args))

    def get_all_frames(self) -> list["CloakPage"]:
        main = self.raw_page.main_frame
        return [
            CloakPage.frame(self, frame)
            for frame in self.raw_page.frames
            if frame is not main
        ]

    @property
    def url(self) -> str:
        return str(getattr(self._raw_context, "url", "") or "")

    @property
    def user_agent(self) -> str:
        with contextlib.suppress(Exception):
            return str(self.raw_page.evaluate("navigator.userAgent") or "")
        return ""

    def get(self, url: str, wait: str = "interactive", timeout: float = 45.0) -> None:
        wait_until = "domcontentloaded" if wait == "interactive" else "load"
        self.raw_page.goto(url, wait_until=wait_until, timeout=int(timeout * 1000))

    def stop_loading(self) -> None:
        with contextlib.suppress(Exception):
            self.raw_page.evaluate("window.stop()")

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if cookies:
            self.context.add_cookies(cookies)

    def eles(self, selector: str, timeout: float = 0.5) -> list[CloakElement]:
        locator = self._raw_context.locator(selector)
        with contextlib.suppress(Exception):
            count = locator.count()
            return [CloakElement(locator.nth(index)) for index in range(count)]
        return []

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.raw_page.screenshot(path=path, full_page=full_page, timeout=30_000)

    def quit(self) -> None:
        self.browser.close()


def launch_cloak_page(
    *,
    headless: bool,
    proxy: Optional[str],
    locale: str = "en-GB",
) -> CloakPage:
    try:
        from cloakbrowser import launch
    except ImportError as exc:
        raise RuntimeError("cloakbrowser is not installed") from exc

    browser = launch(
        headless=headless,
        proxy=proxy,
        locale=locale,
        humanize=True,
        human_preset="fast",
        stealth_args=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-quic",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1920,1080",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        bypass_csp=True,
        locale=locale,
    )
    page = context.new_page()
    page.set_default_timeout(25_000)
    page.set_default_navigation_timeout(60_000)
    return CloakPage(browser, context, page)


class CloakArkoseImageCatcher:
    """Capture completed Arkose RTIG image responses from Playwright events."""

    def __init__(self, page: CloakPage, label: str = "v5-cloak"):
        self.page = page
        self.label = label
        self.captured_images: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._started = False

    def _on_response(self, response: Any) -> None:
        url = str(getattr(response, "url", "") or "")
        if "arkoselabs.com" not in url or "/rtig/image" not in url:
            return
        try:
            body = response.body()
            headers = response.headers
            mime = str(headers.get("content-type") or "").split(";", 1)[0]
            if not body or not mime.startswith("image/"):
                return
            with Image.open(BytesIO(body)) as image:
                size = [int(image.width), int(image.height)]
            sha = hashlib.sha256(body).hexdigest()
            record = {
                "url": url,
                "status": int(getattr(response, "status", 0) or 0),
                "mime": mime,
                "body_bytes": body,
                "sha256": sha,
                "size": size,
                "capturedAt": time.time(),
                "label": self.label,
            }
        except Exception:
            return
        with self._condition:
            if not any(item.get("sha256") == sha for item in self.captured_images):
                self.captured_images.append(record)
                self._condition.notify_all()

    def start(self) -> None:
        if self._started:
            return
        self.page.context.on("response", self._on_response)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        with contextlib.suppress(Exception):
            self.page.context.remove_listener("response", self._on_response)
        self._started = False

    def wait_new_challenge(
        self,
        seen: set[str],
        timeout: float,
        stop_page: Any = None,
    ) -> Optional[dict[str, Any]]:
        del stop_page
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while True:
                for record in self.captured_images:
                    if record.get("sha256") not in seen and record.get("body_bytes"):
                        return dict(record)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.5))


def _extract_blob(body: str) -> Optional[str]:
    if not body:
        return None
    for value in (body, unquote(body)):
        with contextlib.suppress(Exception):
            fields = parse_qs(value, keep_blank_values=True)
            for key in ("data[blob]", "blob", "bda"):
                if fields.get(key):
                    return fields[key][0]
        match = re.search(
            r"(?:^|&)(?:data(?:%5B|\[)blob(?:%5D|\])|blob|bda)=([^&]+)",
            value,
            re.I,
        )
        if match:
            return unquote(match.group(1))
    return None


class CloakArkoseBlobCatcher:
    """Capture Arkose public key and blob from Playwright request events."""

    def __init__(self, page: CloakPage):
        self.page = page
        self.captured_blob: Optional[str] = None
        self.captured_pk: Optional[str] = None
        self.fc_requests: list[str] = []
        self.ca_requests: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._started = False

    def _on_request(self, request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        if "/fc/gt2/" not in url and "/fc/ca/" not in url:
            return
        if "/fc/gt2/" in url and url not in self.fc_requests:
            self.fc_requests.append(url)
        match = re.search(r"/fc/gt2/public_key/([0-9A-F-]+)", url, re.I)
        if match:
            self.captured_pk = match.group(1)
        body = str(getattr(request, "post_data", "") or "")
        if "/fc/gt2/" in url:
            blob = _extract_blob(body)
            if blob:
                with self._condition:
                    self.captured_blob = blob
                    self._condition.notify_all()
        elif body:
            self.ca_requests.append({"url": url, "requestBodyLength": len(body)})

    def start(self) -> None:
        if self._started:
            return
        self.page.context.on("request", self._on_request)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        with contextlib.suppress(Exception):
            self.page.context.remove_listener("request", self._on_request)
        self._started = False

    def wait_for_blob(self, timeout: float = 30.0) -> Optional[str]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while not self.captured_blob:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.5))
        return self.captured_blob


__all__ = [
    "CloakArkoseBlobCatcher",
    "CloakArkoseImageCatcher",
    "CloakPage",
    "launch_cloak_page",
]
