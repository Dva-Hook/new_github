# -*- coding: utf-8 -*-
"""Selectable V5 runner built on the V4 persistent HTTP registration flow."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import requests

import register_ruyipage_v4 as v4
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
REGISTRATION_COUNTRY = "GBR"
DEFAULT_YESCAPTCHA_API_URL = "https://api.yescaptcha.com/createTask"
DEFAULT_CAPMONSTER_CREATE_URL = "https://api.capmonster.cloud/createTask"
DEFAULT_CAPMONSTER_RESULT_URL = "https://api.capmonster.cloud/getTaskResult"
DEFAULT_QUESTION = (
    "use the arrows to move the characters until they are standing on the same "
    "icons as in the picture on the left"
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
        "--yescaptcha-key",
        default=os.environ.get("YESCAPTCHA_API_KEY", ""),
    )
    parser.add_argument(
        "--yescaptcha-api-url",
        default=DEFAULT_YESCAPTCHA_API_URL,
    )
    parser.add_argument("--yescaptcha-timeout", type=float, default=35.0)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
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


def solve_with_capmonster(
    context: Mapping[str, Any],
    args: argparse.Namespace,
    out: Path,
) -> dict[str, Any]:
    blob = str(context.get("blob") or "")
    site_key = str(context.get("siteKey") or v4.DEFAULT_SITE_KEY)
    surl = str(context.get("surl") or v4.DEFAULT_SURL)
    website_url = str(context.get("websiteURL") or args.entry_url)
    user_agent = str(
        context.get("userAgent")
        or args.protocol_user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
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


def _launch_solver_browser(
    args: argparse.Namespace,
    proxy: v4.ProxySettings,
    runtime_proxy_url: Optional[str],
) -> tuple[Any, Any, Optional[Any]]:
    if args.browser == "ruyipage":
        page = v4.launch_ruyi_browser(args, proxy, runtime_proxy_url)
        catcher = v4.v3.RuyiArkoseImageCatcher(page, label=f"v5-{args.solver}")
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
            import register_ruyipage_v2 as v2

            if args.browser == "cloakbrowser":
                v2.CLICK_STYLE = "js"
            else:
                v2.CLICK_STYLE = args.click_style
            result = v2.auto_solve_solver_tab(page, catcher, args, out)
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
                    "registrationCountry": REGISTRATION_COUNTRY,
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
        LOG.info("Registration country: %s (fixed)", REGISTRATION_COUNTRY)
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
                country=REGISTRATION_COUNTRY,
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
            solve_result = solve_with_capmonster(arkose, args, out)
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
                "registrationCountry": REGISTRATION_COUNTRY,
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
            "registrationCountry": REGISTRATION_COUNTRY,
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
