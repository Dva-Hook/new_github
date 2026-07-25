# -*- coding: utf-8 -*-
"""Selectable V5 runner built on the V4 persistent HTTP registration flow."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlsplit

import requests

import register_ruyipage_v4 as v4
import v5_yescaptcha_solver
from battle_protocol_flow_v4 import BattleProtocolClient, PersistentFlowState
from proxy_traffic_meter import ProxyTrafficMeter
from v5_cloak_adapter import (
    CloakArkoseBlobCatcher,
    CloakArkoseImageCatcher,
    launch_cloak_page,
)


LOG = logging.getLogger("http_register_v5")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ruyipage_http_v5_register" / "runs"
DEFAULT_REGISTRATION_COUNTRY = "USA"
DEFAULT_YESCAPTCHA_API_URL = "https://api.yescaptcha.com/createTask"
DEFAULT_CAPMONSTER_CREATE_URL = "https://api.capmonster.cloud/createTask"
DEFAULT_CAPMONSTER_RESULT_URL = "https://api.capmonster.cloud/getTaskResult"
DEFAULT_WINDOWS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_id() -> str:
    return "run_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [HTTP-V5] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    for name in ("urllib3", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def write_json(path: Path, value: Any) -> None:
    v4.write_json(path, value)


def build_parser() -> argparse.ArgumentParser:
    parser = v4.build_parser()
    parser.description = (
        "Persistent HTTP registration + selectable browser + selectable solver"
    )
    parser.set_defaults(
        output_dir=str(DEFAULT_OUTPUT_ROOT),
        static_cache_dir=os.environ.get(
            "V5_STATIC_CACHE_DIR",
            str(PROJECT_ROOT / ".cache" / "v5_public_static"),
        ),
        protocol_user_agent=os.environ.get(
            "V5_USER_AGENT", DEFAULT_WINDOWS_USER_AGENT
        ),
    )
    parser.add_argument(
        "--solver",
        choices=("v11", "yescaptcha", "capmonster"),
        default=os.environ.get("V5_SOLVER", "v11").lower(),
    )
    parser.add_argument(
        "--browser",
        choices=("ruyipage", "cloakbrowser"),
        default=os.environ.get("V5_BROWSER", "ruyipage").lower(),
    )
    parser.add_argument(
        "--country",
        default=os.environ.get("V5_COUNTRY", DEFAULT_REGISTRATION_COUNTRY),
        help="ISO 3166-1 alpha-3 registration country code (default: USA)",
    )
    parser.add_argument(
        "--yescaptcha-key",
        default=os.environ.get("YESCAPTCHA_API_KEY", ""),
    )
    parser.add_argument(
        "--yescaptcha-api-url",
        default=DEFAULT_YESCAPTCHA_API_URL,
    )
    parser.add_argument("--yescaptcha-timeout", type=float, default=35.0)
    parser.add_argument("--click-interval-min-ms", type=int, default=250)
    parser.add_argument("--click-interval-max-ms", type=int, default=600)
    parser.add_argument(
        "--capmonster-key",
        default=os.environ.get("CAPMONSTER_API_KEY", ""),
    )
    parser.add_argument(
        "--capmonster-create-url",
        default=DEFAULT_CAPMONSTER_CREATE_URL,
    )
    parser.add_argument(
        "--capmonster-result-url",
        default=DEFAULT_CAPMONSTER_RESULT_URL,
    )
    parser.add_argument("--capmonster-timeout", type=float, default=300.0)
    parser.add_argument("--capmonster-poll-interval", type=float, default=2.5)
    parser.add_argument("--cloak-locale", default="en-GB")
    return parser


def validate_configuration(args: argparse.Namespace) -> dict[str, Any]:
    registration_country = str(args.country or "").strip().upper()
    if len(registration_country) != 3 or not registration_country.isalpha():
        raise ValueError(
            "--country must be a three-letter ISO country code, "
            f"got {args.country!r}"
        )
    if args.solver == "yescaptcha" and not str(args.yescaptcha_key).strip():
        raise ValueError(
            "YESCAPTCHA_API_KEY is required when --solver yescaptcha is selected"
        )
    if args.solver == "capmonster" and not str(args.capmonster_key).strip():
        raise ValueError(
            "CAPMONSTER_API_KEY is required when --solver capmonster is selected"
        )
    if args.capmonster_poll_interval <= 0 or args.capmonster_timeout <= 0:
        raise ValueError("CapMonster polling values must be positive")
    if args.yescaptcha_timeout <= 0:
        raise ValueError("YesCaptcha timeout must be positive")
    return {
        "solver": args.solver,
        "browser": args.browser,
        "registrationCountry": registration_country,
        "browserRequired": args.solver in {"v11", "yescaptcha"},
        "apiKeyConfigured": (
            True
            if args.solver == "v11"
            else bool(
                str(
                    args.yescaptcha_key
                    if args.solver == "yescaptcha"
                    else args.capmonster_key
                ).strip()
            )
        ),
    }


def _redacted_provider_response(value: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(value)
    solution = dict(clean.get("solution") or {})
    token = str(solution.pop("token", "") or "")
    if token:
        solution["tokenLength"] = len(token)
    if solution:
        clean["solution"] = solution
    elif "solution" in clean:
        clean["solution"] = {}
    return clean


def _capmonster_proxy_fields(proxy: v4.ProxySettings) -> dict[str, Any]:
    """Translate the selected V5 route into CapMonster FunCaptcha fields."""

    if not proxy.enabled:
        return {}
    parsed = urlsplit(str(proxy.url or ""))
    proxy_type = str(proxy.scheme or parsed.scheme or "http").lower()
    if proxy_type == "socks5h":
        proxy_type = "socks5"
    if proxy_type not in {"http", "https", "socks4", "socks5"}:
        raise ValueError(f"unsupported CapMonster proxy scheme: {proxy_type}")
    address = str(proxy.host or parsed.hostname or "").strip()
    port = int(proxy.port or parsed.port or 0)
    if not address or not 1 <= port <= 65535:
        raise ValueError("selected proxy has no valid address and port")

    fields: dict[str, Any] = {
        "proxyType": proxy_type,
        "proxyAddress": address,
        "proxyPort": port,
    }
    if parsed.username is not None:
        fields["proxyLogin"] = unquote(parsed.username)
        fields["proxyPassword"] = unquote(parsed.password or "")
    return fields


def solve_with_capmonster(
    context: Mapping[str, Any],
    args: argparse.Namespace,
    out: Path,
    proxy: v4.ProxySettings,
) -> dict[str, Any]:
    blob = str(context.get("blob") or "")
    site_key = str(context.get("siteKey") or v4.DEFAULT_SITE_KEY)
    surl = str(context.get("surl") or v4.DEFAULT_SURL)
    website_url = str(context.get("websiteURL") or args.entry_url)
    user_agent = str(
        args.protocol_user_agent
        or context.get("userAgent")
        or DEFAULT_WINDOWS_USER_AGENT
    )
    task: dict[str, Any] = {
        "type": "FunCaptchaTask",
        "websiteURL": website_url,
        "websitePublicKey": site_key,
        "userAgent": user_agent,
    }
    if blob:
        task["data"] = json.dumps({"blob": blob}, separators=(",", ":"))
    if surl and surl != "client-api.arkoselabs.com":
        task["funcaptchaApiJSSubdomain"] = surl
    task.update(_capmonster_proxy_fields(proxy))
    LOG.info(
        "CapMonster worker route: %s",
        proxy.display if proxy.enabled else "provider built-in proxy",
    )

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    create_response = session.post(
        args.capmonster_create_url,
        json={"clientKey": str(args.capmonster_key).strip(), "task": task},
        timeout=20,
    )
    create_response.raise_for_status()
    created = create_response.json()
    write_json(out / "capmonster_create_response.json", _redacted_provider_response(created))
    task_id = created.get("taskId")
    if not task_id or int(created.get("errorId") or 0):
        raise RuntimeError(
            f"CapMonster createTask failed: errorCode={created.get('errorCode')} "
            f"errorDescription={created.get('errorDescription')}"
        )
    LOG.info("CapMonster task created: taskId=%s blobLength=%s", task_id, len(blob))

    deadline = time.monotonic() + float(args.capmonster_timeout)
    polls = 0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        polls += 1
        response = session.post(
            args.capmonster_result_url,
            json={"clientKey": str(args.capmonster_key).strip(), "taskId": task_id},
            timeout=15,
        )
        response.raise_for_status()
        last = response.json()
        status = str(last.get("status") or "")
        if status == "ready":
            token = str((last.get("solution") or {}).get("token") or "")
            write_json(
                out / "capmonster_result.json",
                _redacted_provider_response(last),
            )
            if not token:
                raise RuntimeError("CapMonster returned ready without solution.token")
            return {
                "ok": True,
                "token": token,
                "actions": [],
                "provider": "capmonster",
                "taskId": task_id,
                "polls": polls,
            }
        if int(last.get("errorId") or 0) or status in {"error", "failed"}:
            write_json(
                out / "capmonster_result.json",
                _redacted_provider_response(last),
            )
            raise RuntimeError(
                f"CapMonster task failed: errorCode={last.get('errorCode')} "
                f"errorDescription={last.get('errorDescription')}"
            )
        time.sleep(float(args.capmonster_poll_interval))
    write_json(out / "capmonster_result.json", _redacted_provider_response(last))
    raise TimeoutError(
        f"CapMonster task {task_id} timed out after {args.capmonster_timeout}s"
    )


def _install_cloak_resource_filter(page: Any, enabled: bool) -> None:
    if not enabled:
        return

    def route_handler(route: Any, request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        resource_type = str(getattr(request, "resource_type", "") or "")
        if resource_type in {"font", "media"} or v4.should_block_resource(url):
            route.abort()
        else:
            route.continue_()

    page.context.route("**/*", route_handler)


class V5RuyiYesCaptchaImageCatcher(v4.v3.RuyiArkoseImageCatcher):
    """V4-compatible catcher that accepts compact Arkose RTIG strips."""

    def wait_new_challenge(
        self,
        seen: set[str],
        timeout: float,
        stop_page: Any = None,
    ) -> Optional[dict[str, Any]]:
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            if stop_page is not None:
                with contextlib.suppress(Exception):
                    if v4.base.captcha_state(stop_page) in ("success", "rejected"):
                        return None
            with self._lock:
                ready = [
                    dict(record)
                    for record in self.captured_images
                    if record.get("body_bytes")
                ]
            ready.sort(
                key=lambda record: (
                    0
                    if "/rtig/image" in str(record.get("url") or "").lower()
                    else 1,
                    record.get("timestamp") or 0,
                )
            )
            for record in ready:
                data = record.get("body_bytes") or b""
                sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
                if sha in seen:
                    continue
                size = record.get("size") or v4.v3.image_size(data)
                if size:
                    width, height = size
                    is_rtig = "/rtig/image" in str(
                        record.get("url") or ""
                    ).lower()
                    valid_rtig = is_rtig and 300 <= height <= 650
                    valid_generic = width >= 800 and 300 <= height <= 650
                    if not valid_rtig and not valid_generic:
                        seen.add(sha)
                        LOG.info(
                            "[%s] ignore non-challenge image size=%sx%s url=%s",
                            self.label,
                            width,
                            height,
                            str(record.get("url") or "")[:120],
                        )
                        continue
                return record
            self._event.wait(0.5)
            self._event.clear()
        return None


def save_yescaptcha_image_record(
    record: dict[str, Any],
    out_dir: Path,
    wave: int,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = record.get("body_bytes") or b""
    sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
    extension = v4.v3.image_ext(record.get("mime") or "", data)
    path = out_dir / f"yescaptcha_wave_{wave:02d}_{sha[:12]}{extension}"
    path.write_bytes(data)
    metadata = {
        key: value for key, value in record.items() if key != "body_bytes"
    }
    metadata.update({"file": str(path), "sha256": sha, "bytes": len(data)})
    write_json(
        out_dir / f"yescaptcha_wave_{wave:02d}_{sha[:12]}.json",
        metadata,
    )
    return path


_TERMINAL_SOLVER_STATUSES = frozenset(
    {
        "onFailed",
        "onError",
        "run-error",
        "setConfig-error",
        "script-error",
    }
)


def solver_terminal_reason(page: Any) -> str:
    state = v4.v3.solver_state(page)
    status = str(state.get("status") or "").strip()
    completed = state.get("completedPayload")
    rejection = v4.v3.completion_rejection_reason(completed)
    if rejection:
        return f"Arkose completion rejected: {rejection}"
    if status in _TERMINAL_SOLVER_STATUSES:
        detail = str(state.get("error") or "").strip()
        suffix = f": {detail}" if detail else ""
        return f"Arkose solver terminal status: {status}{suffix}"
    if v4.base.captcha_state(page) == "rejected":
        return "Arkose challenge page is rejected"
    return ""


def wait_image_or_token_fast(
    catcher: Any,
    seen: set[str],
    timeout: float,
    solver_tab: Any,
) -> tuple[str, Any]:
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline:
        token = v4.v3.wait_token_quick(solver_tab, 0.1)
        if token:
            return "token", token
        terminal = solver_terminal_reason(solver_tab)
        if terminal:
            return "terminal", terminal
        remaining = max(0.05, deadline - time.time())
        record = catcher.wait_new_challenge(
            seen,
            timeout=min(0.7, remaining),
            stop_page=solver_tab,
        )
        if record:
            return "image", record
    token = v4.v3.wait_token_quick(solver_tab, 0.2)
    if token:
        return "token", token
    terminal = solver_terminal_reason(solver_tab)
    if terminal:
        return "terminal", terminal
    return "timeout", None


def click_next_n_v4(page: Any, count: int, args: argparse.Namespace) -> bool:
    count = max(0, int(count))
    LOG.info("Click the next-image arrow %s times", count)
    minimum = max(0, int(args.click_interval_min_ms))
    maximum = max(minimum, int(args.click_interval_max_ms))
    for index in range(count):
        clicked = False
        for attempt in range(3):
            before = v4.v3.current_index(page)
            if not v4.v3.click_arrow(page, "right", timeout=4):
                return False
            after = v4.v3.wait_index_change(page, before)
            if before < 0 or after != before:
                LOG.info(
                    "Next click %s/%s ok: before=%s after=%s",
                    index + 1,
                    count,
                    before,
                    after,
                )
                clicked = True
                break
            LOG.warning(
                "Next click %s/%s may have been ignored: retry=%s "
                "before=%s after=%s",
                index + 1,
                count,
                attempt + 1,
                before,
                after,
            )
        if not clicked:
            return False
        if index + 1 < count:
            delay = random.randint(minimum, maximum) / 1000.0
            LOG.info("Waiting %sms before the next arrow click", round(delay * 1000))
            time.sleep(delay)
    return True


def auto_solve_yescaptcha_tab(
    solver_tab: Any,
    catcher: Any,
    args: argparse.Namespace,
    out: Path,
) -> dict[str, Any]:
    """Use the verified local V4 per-wave YesCaptcha state machine."""

    api_key = str(args.yescaptcha_key or "").strip()
    if not api_key:
        raise RuntimeError("YesCaptcha API key is empty")
    images_dir = out / "yescaptcha_images"
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    token = v4.v3.wait_token_quick(solver_tab, 2.0, "initial ")
    if token:
        return v4.v3.build_token_result(solver_tab, token, actions)
    if not v4.v3.ensure_verify_or_image(
        solver_tab,
        catcher,
        args.verify_timeout,
    ):
        LOG.warning("No challenge image confirmed after Verify; keep waiting")

    for wave in range(args.max_waves):
        token = v4.v3.wait_token_quick(solver_tab, 1.0, f"wave{wave} pre ")
        if token:
            return v4.v3.build_token_result(solver_tab, token, actions)
        terminal = solver_terminal_reason(solver_tab)
        if terminal:
            return {"ok": False, "error": terminal, "actions": actions}

        kind, value = wait_image_or_token_fast(
            catcher,
            seen,
            args.first_image_timeout if wave == 0 else args.next_image_timeout,
            solver_tab,
        )
        if kind == "token":
            return v4.v3.build_token_result(solver_tab, str(value), actions)
        if kind == "terminal":
            return {"ok": False, "error": str(value), "actions": actions}
        record = value
        if not record:
            token = v4.v3.wait_token_quick(
                solver_tab,
                args.after_submit_token_wait,
                f"wave{wave} no-image ",
            )
            if token:
                return v4.v3.build_token_result(solver_tab, token, actions)
            state = v4.base.captcha_state(solver_tab)
            sample = v4.base.captcha_text(solver_tab).replace("\n", " ")[:260]
            return {
                "ok": False,
                "error": f"no new challenge image at wave={wave}, state={state}",
                "actions": actions,
                "sample": sample,
            }

        data = record.get("body_bytes") or b""
        sha = record.get("sha256") or hashlib.sha256(data).hexdigest()
        seen.add(sha)
        image_path = save_yescaptcha_image_record(record, images_dir, wave)
        if args.debug_screenshots:
            v4.base.screenshot(
                solver_tab,
                out / "solver_screenshots" / f"wave_{wave:02d}_before_answer.png",
            )

        question = v5_yescaptcha_solver.extract_dynamic_prompt(
            solver_tab,
            v4.base.all_contexts,
            timeout=min(5.0, max(0.5, float(args.yescaptcha_timeout))),
        )
        write_json(
            images_dir / f"yescaptcha_wave_{wave:02d}_question.json",
            question,
        )
        answer, api_result = v5_yescaptcha_solver.classify_image(
            image_path,
            question=question["prompt"],
            api_key=api_key,
            api_url=args.yescaptcha_api_url,
            timeout=args.yescaptcha_timeout,
            response_path=(
                images_dir / f"yescaptcha_wave_{wave:02d}_response.json"
            ),
        )
        retries = int(api_result.get("retries") or 0)
        if retries:
            reasons = ",".join(
                str(item) for item in (api_result.get("reasons") or [])
            ) or "unknown"
            LOG.info(
                "YesCaptcha same-image recovery wave=%s retries=%s "
                "reasons=%s reencoded=%s",
                wave,
                retries,
                reasons,
                bool(api_result.get("imageReencoded")),
            )
        LOG.info(
            "YesCaptcha answer=%s wave=%s question=%r",
            answer,
            wave,
            question["prompt"],
        )
        if not click_next_n_v4(solver_tab, answer, args):
            return {
                "ok": False,
                "error": f"failed to click next {answer} times at wave={wave}",
                "actions": actions,
            }
        time.sleep(0.08 + random.random() * 0.08)
        submit_ok = v4.v3.click_submit(solver_tab)
        action = {
            "wave": wave,
            "image": str(image_path),
            "sha256": sha,
            "answer": answer,
            "clicks": answer,
            "submit": submit_ok,
            "question": question,
            "yescaptcha": api_result,
        }
        actions.append(action)
        write_json(out / "yescaptcha_actions_latest.json", actions)
        if args.debug_screenshots:
            v4.base.screenshot(
                solver_tab,
                out / "solver_screenshots" / f"wave_{wave:02d}_after_submit.png",
            )
        if not submit_ok:
            state = v4.base.captcha_state(solver_tab)
            return {
                "ok": False,
                "error": f"submit button failed at wave={wave}, state={state}",
                "actions": actions,
            }
        token = v4.v3.wait_token_quick(
            solver_tab,
            args.after_submit_token_wait,
            f"wave{wave} post ",
        )
        if token:
            return v4.v3.build_token_result(solver_tab, token, actions)

    token = v4.v3.wait_token_quick(solver_tab, args.token_timeout, "final ")
    if token:
        return v4.v3.build_token_result(solver_tab, token, actions)
    return {
        "ok": False,
        "error": f"max_waves exceeded ({args.max_waves}) without token",
        "actions": actions,
    }


def _launch_solver_browser(
    args: argparse.Namespace,
    proxy: v4.ProxySettings,
    runtime_proxy_url: Optional[str],
) -> tuple[Any, Any, Optional[Any]]:
    if args.browser == "ruyipage":
        page = v4.launch_ruyi_browser(args, proxy, runtime_proxy_url)
        catcher_type = (
            V5RuyiYesCaptchaImageCatcher
            if args.solver == "yescaptcha"
            else v4.v3.RuyiArkoseImageCatcher
        )
        catcher = catcher_type(page, label=f"v5-{args.solver}")
        optimizer = v4.BrowserResourceOptimizer(
            page,
            Path(args.static_cache_dir),
            proxy_enabled=proxy.enabled,
            direct_public_static=(proxy.enabled and not args.no_direct_public_static),
            direct_challenge_images=(proxy.enabled and args.direct_challenge_images),
            block_nonessential=not args.no_resource_blocking,
            fetch_timeout=args.static_fetch_timeout,
            max_entry_bytes=max(1, int(args.static_cache_max_entry_mib * v4.MIB)),
            should_block=v4.should_block_resource,
        )
        optimizer.start()
        return page, catcher, optimizer

    page = launch_cloak_page(
        headless=bool(args.headless),
        proxy=runtime_proxy_url,
        locale=args.cloak_locale,
    )
    _install_cloak_resource_filter(page, not args.no_resource_blocking)
    catcher = CloakArkoseImageCatcher(page, label=f"v5-{args.solver}")
    return page, catcher, None


def _recover_missing_blob(
    client: BattleProtocolClient,
    args: argparse.Namespace,
    proxy: v4.ProxySettings,
    out: Path,
    runtime_proxy_url: Optional[str],
) -> dict[str, Any]:
    if args.browser == "ruyipage":
        recovery_page = v4.launch_ruyi_browser(args, proxy, runtime_proxy_url)
        try:
            return v4.recover_blob_with_ruyi(recovery_page, client, args, out)
        finally:
            with contextlib.suppress(Exception):
                recovery_page.quit()

    recovery_page = launch_cloak_page(
        headless=bool(args.headless),
        proxy=runtime_proxy_url,
        locale=args.cloak_locale,
    )
    catcher = CloakArkoseBlobCatcher(recovery_page)
    try:
        cookies = client.playwright_cookies()
        recovery_page.set_cookies(cookies)
        write_json(
            out / "solver" / "cookie_import_summary.json",
            {"count": len(cookies), "names": sorted({item["name"] for item in cookies})},
        )
        catcher.start()
        recovery_page.get(args.entry_url, wait="interactive", timeout=45)
        recovery_page.stop_loading()
        blob = catcher.wait_for_blob(timeout=min(12.0, args.blob_timeout))
        if not blob:
            v4.base.click_arkose_verify(recovery_page, timeout=12)
            blob = catcher.wait_for_blob(timeout=args.blob_timeout)
        if not blob:
            raise RuntimeError("CloakBrowser fallback did not recover an Arkose blob")
        detected = v4.base.detect_arkose_context(recovery_page, catcher)
        return {
            "blob": blob,
            "siteKey": detected.get("siteKey") or v4.DEFAULT_SITE_KEY,
            "surl": detected.get("surl") or v4.DEFAULT_SURL,
            "websiteURL": args.entry_url,
            "userAgent": detected.get("userAgent"),
            "source": "cloakbrowser-cookie-import-playwright",
        }
    finally:
        with contextlib.suppress(Exception):
            catcher.stop()
        with contextlib.suppress(Exception):
            recovery_page.quit()


def solve_with_browser(
    client: BattleProtocolClient,
    context: Mapping[str, Any],
    args: argparse.Namespace,
    proxy: v4.ProxySettings,
    out: Path,
    runtime_proxy_url: Optional[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = dict(context)
    if not current.get("blob"):
        LOG.info("HTTP response had no blob; using the V4 cookie-import recovery")
        current = _recover_missing_blob(
            client, args, proxy, out, runtime_proxy_url
        )
    current["siteKey"] = str(current.get("siteKey") or v4.DEFAULT_SITE_KEY)
    current["surl"] = str(current.get("surl") or v4.DEFAULT_SURL)
    current["websiteURL"] = str(current.get("websiteURL") or args.entry_url)
    write_json(out / "arkose_context.json", v4.public_arkose_context(current))

    page = None
    catcher = None
    optimizer = None
    try:
        page, catcher, optimizer = _launch_solver_browser(
            args, proxy, runtime_proxy_url
        )
        catcher.start()
        harness = v4.base.build_solver_harness(
            current["siteKey"], str(current["blob"]), current["surl"]
        )
        origin = v4.replace_document_low_traffic(
            page, current["websiteURL"], harness
        )
        write_json(out / "solver" / "origin.json", origin)
        if args.debug_screenshots:
            v4.base.screenshot(
                page, out / "solver_screenshots" / "harness_loaded.png"
            )

        if args.browser == "cloakbrowser":
            # The shared V2/V3 helpers use JS fallback clicks on Playwright.
            v4.v3.CLICK_STYLE = "js"
        if args.solver == "v11":
            result = v4.run_v4_solver_tab(page, catcher, args, out)
            result_file = out / "local_v11_solver_result.json"
        else:
            result = auto_solve_yescaptcha_tab(page, catcher, args, out)
            result_file = out / "yescaptcha_solver_result.json"
        write_json(
            result_file,
            {key: value for key, value in result.items() if key != "token"},
        )
        if not result.get("ok") or not result.get("token"):
            raise RuntimeError(
                str(result.get("error") or f"{args.solver} returned no token")
            )
        with contextlib.suppress(Exception):
            catcher.stop()
        catcher = None
        if optimizer is not None:
            result["browserTraffic"] = v4.stop_browser_optimizer(optimizer, out)
            optimizer = None
        else:
            result["browserTraffic"] = {
                "enabled": True,
                "adapter": "cloakbrowser-playwright",
            }
        return result, current
    finally:
        with contextlib.suppress(Exception):
            if catcher:
                catcher.stop()
        with contextlib.suppress(Exception):
            if optimizer:
                v4.stop_browser_optimizer(optimizer, out)
        if args.keep_open and page is not None:
            with contextlib.suppress(EOFError):
                input("Solver browser is open. Press Enter to close...")
        with contextlib.suppress(Exception):
            if page:
                page.quit()


def resolve_resume_path(value: str) -> Path:
    return v4.resolve_resume_path(value)


def main() -> int:
    force_utf8_stdio()
    args = build_parser().parse_args()
    v4.configure_v3_clicks(args)
    config = validate_configuration(args)
    registration_country = str(config["registrationCountry"])
    resume_path = resolve_resume_path(args.resume) if args.resume else None
    out = (
        resume_path.parent
        if resume_path is not None
        else Path(args.output_dir).expanduser().resolve() / run_id()
    )
    out.mkdir(parents=True, exist_ok=True)
    args.output_dir = str(out)
    setup_logging(out / "run.log")

    state: Optional[PersistentFlowState] = None
    client: Optional[BattleProtocolClient] = None
    identity: dict[str, str] = {}
    proxy = v4.ProxySettings(None, "direct")
    traffic_meter: Optional[ProxyTrafficMeter] = None
    traffic_snapshots: dict[str, dict[str, Any]] = {}
    runtime_proxy_url: Optional[str] = None
    started = time.perf_counter()
    try:
        proxy = v4.parse_proxy(args.proxy)
        if resume_path is not None:
            state = PersistentFlowState.load(resume_path)
            saved_profile = dict(state.data.get("profile") or {})
            saved_country = str(
                saved_profile.get("registrationCountry") or ""
            ).strip().upper()
            if saved_country:
                registration_country = saved_country
                config["registrationCountry"] = saved_country
            identity = {
                key: str(value)
                for key, value in dict(state.data.get("identity") or {}).items()
            }
            if not all(identity.get(key) for key in ("email", "password", "battle_tag")):
                raise RuntimeError("resume state has no complete account identity")
            LOG.info(
                "Resuming persistent state: %s status=%s",
                resume_path,
                state.data.get("status"),
            )
        else:
            identity = v4.configured_identity(args)
            state = PersistentFlowState.create(
                out / "persistent_state.json",
                identity=identity,
                profile={
                    "mode": "persistent-http-v5",
                    "solver": args.solver,
                    "browser": args.browser,
                    "registrationCountry": registration_country,
                    "countryProbe": bool(args.country_probe),
                    "proxy": proxy.summary(),
                    "proxyTrafficMeter": bool(proxy.enabled),
                    "publicStaticDirect": bool(
                        proxy.enabled and not args.no_direct_public_static
                    ),
                    "challengeImageDirect": bool(
                        proxy.enabled and args.direct_challenge_images
                    ),
                    "protocolImpersonate": args.protocol_impersonate,
                },
            )
        write_json(out / "account_generated.json", identity)
        write_json(out / "v5_configuration.json", config)
        LOG.info("Output directory: %s", out)
        LOG.info(
            "Flow: persistent HTTP -> %s/%s -> HTTP captcha-gate",
            args.browser,
            args.solver,
        )
        LOG.info("Registration country: %s", registration_country)
        LOG.info("Proxy route: %s auth=%s", proxy.display, proxy.has_auth)
        LOG.info("Account: %s", identity["email"])
        LOG.info("BattleTag: %s", identity["battle_tag"])

        if state.data.get("status") == "complete":
            LOG.info("Persistent state is already complete")
            print(f"Account: {identity['email']}")
            print(f"Password: {identity['password']}")
            print(f"BattleTag: {identity.get('battle_tag', '')}")
            return 0

        runtime_proxy_url = proxy.url
        if proxy.enabled and proxy.url:
            traffic_meter = ProxyTrafficMeter(proxy.url)
            runtime_proxy_url = traffic_meter.start()
            LOG.info(
                "Proxy traffic meter started: local=%s upstream=%s",
                runtime_proxy_url,
                proxy.display,
            )
            traffic_snapshots["start"] = v4.capture_proxy_traffic_snapshot(
                traffic_meter
            )

        client = BattleProtocolClient(
            state,
            out,
            entry_url=args.entry_url,
            proxy=runtime_proxy_url,
            impersonate=args.protocol_impersonate,
            user_agent=args.protocol_user_agent or None,
            accept_language="en-GB,en;q=0.9",
            timeout=args.protocol_timeout,
        )
        if state.data.get("status") not in {"captcha-gate", "token-ready"}:
            client.run_to_captcha(
                country=registration_country,
                opt_in=False,
                country_probe=bool(args.country_probe),
            )
        LOG.info("Persistent HTTP flow reached captcha-gate")
        arkose = dict(state.data.get("arkose") or {})
        if not arkose.get("blob"):
            arkose = client.recover_arkose_from_last_response()
        traffic_snapshots["captchaGate"] = v4.capture_proxy_traffic_snapshot(
            traffic_meter
        )
        LOG.info(
            "Arkose context: source=%s siteKey=%s blobLength=%s",
            arkose.get("source"),
            arkose.get("siteKey"),
            len(str(arkose.get("blob") or "")),
        )

        token = (
            str(arkose.get("token") or "")
            if state.data.get("status") == "token-ready"
            else ""
        )
        health: dict[str, Any]
        if token:
            health = {"ok": True, "status": "not-required-resumed-token"}
            solve_result = {
                "ok": True,
                "token": token,
                "actions": [],
                "resumedToken": True,
            }
        elif args.solver == "capmonster":
            health = {"ok": True, "status": "external-provider"}
            solve_result = solve_with_capmonster(arkose, args, out, proxy)
            token = str(solve_result["token"])
        else:
            if args.solver == "v11":
                health = v4.wait_rank_v11_service(
                    args.rank_v11_url, args.rank_v11_timeout
                )
                write_json(out / "rank_v11_health.json", health)
                LOG.info(
                    "Local V11 ready: device=%s load=%.3fs warmup=%.3fs",
                    health.get("device"),
                    float(health.get("model_load_seconds") or 0.0),
                    float(health.get("warmup_seconds") or 0.0),
                )
            else:
                health = {"ok": True, "status": "external-classifier"}
            solve_result, arkose = solve_with_browser(
                client,
                arkose,
                args,
                proxy,
                out,
                runtime_proxy_url,
            )
            token = str(solve_result["token"])

        arkose["token"] = token
        state.checkpoint(
            "token-ready",
            arkose=arkose,
            event={
                "completed": f"{args.browser}-{args.solver}",
                "tokenLength": len(token),
            },
        )
        LOG.info("Solver returned Arkose token, length=%s", len(token))
        traffic_snapshots["tokenReady"] = v4.capture_proxy_traffic_snapshot(
            traffic_meter
        )

        outcome = client.submit_captcha(token)
        success = outcome.get("status") == "success" and bool(outcome.get("success"))
        registration = {
            "ok": success,
            "email": identity["email"],
            "battleTag": identity["battle_tag"],
            "successSource": "persistent-http-captcha-gate" if success else None,
            "outcome": outcome,
        }
        write_json(out / "registration_result.json", registration)
        write_json(
            out / "summary.json",
            {
                "ok": success,
                "outputDir": str(out),
                "mode": "persistent-http-v5",
                "solver": args.solver,
                "browser": args.browser,
                "registrationCountry": registration_country,
                "countryProbe": bool(args.country_probe),
                "proxy": proxy.summary(),
                "arkose": v4.public_arkose_context(arkose),
                "solverHealth": health,
                "solverActions": solve_result.get("actions") or [],
                "browserTraffic": solve_result.get("browserTraffic") or {},
                "registration": registration,
                "elapsedSeconds": time.perf_counter() - started,
            },
        )
        if not success:
            LOG.error(
                "captcha-gate did not confirm success: status=%s sample=%r",
                outcome.get("status"),
                outcome.get("sample"),
            )
            return 1

        LOG.info("Registration succeeded through the persisted HTTP session")
        print(f"Account: {identity['email']}")
        print(f"Password: {identity['password']}")
        print(f"BattleTag: {identity['battle_tag']}")
        return 0
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        write_json(
            out / "summary.json",
            {"ok": False, "error": "KeyboardInterrupt", "outputDir": str(out)},
        )
        return 130
    except Exception as exc:
        error_text = v4.redact_proxy_text(
            f"{type(exc).__name__}: {exc}", proxy, args.proxy
        )
        safe_traceback = v4.redact_proxy_text(
            traceback.format_exc(), proxy, args.proxy
        )
        LOG.error("Run failed: %s\n%s", error_text, safe_traceback)
        if state is not None:
            with contextlib.suppress(Exception):
                state.checkpoint(
                    str(state.data.get("status") or "failed"),
                    error=error_text,
                    event={"failed": "runner", "errorType": type(exc).__name__},
                )
        failure = {
            "ok": False,
            "error": error_text,
            "outputDir": str(out),
            "solver": args.solver,
            "browser": args.browser,
            "registrationCountry": registration_country,
            "countryProbe": bool(args.country_probe),
            "proxy": proxy.summary(),
            "elapsedSeconds": time.perf_counter() - started,
        }
        if isinstance(exc, v4.v3.UnsupportedCaptchaQuestion):
            failure["unsupportedCaptcha"] = True
            failure["challenge"] = exc.details
        browser_traffic = v4.read_json_object(out / "browser_traffic.json")
        if browser_traffic:
            failure["browserTraffic"] = browser_traffic
        write_json(out / "summary.json", failure)
        return (
            v4.v3.UNSUPPORTED_CAPTCHA_EXIT_CODE
            if isinstance(exc, v4.v3.UnsupportedCaptchaQuestion)
            else 1
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.session.close()
        if traffic_meter is not None:
            with contextlib.suppress(Exception):
                report = traffic_meter.stop()
                v4.write_proxy_traffic_report(out, report)
                phase_report = v4.build_proxy_traffic_phase_report(
                    traffic_snapshots, report
                )
                v4.write_proxy_traffic_phase_report(out, phase_report)
                v4.log_proxy_traffic_phases(phase_report)
                v4.log_proxy_traffic_targets(report)
                LOG.info(
                    "Proxy traffic total: upload=%.4f MiB download=%.4f MiB "
                    "total=%.4f MiB bytes=%s connections=%s failures=%s",
                    float(report.get("uploadMiB") or 0.0),
                    float(report.get("downloadMiB") or 0.0),
                    float(report.get("totalMiB") or 0.0),
                    int(report.get("totalBytes") or 0),
                    int(report.get("connections") or 0),
                    int(report.get("failures") or 0),
                )


if __name__ == "__main__":
    raise SystemExit(main())
