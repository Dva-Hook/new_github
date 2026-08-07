# -*- coding: utf-8 -*-
"""HTTP 持久化注册上下文 + RuyiPage 多轮 FunCaptcha 题图采集。

这个脚本只把自动生成的注册会话推进到 captcha-gate，然后用 RuyiPage
加载同一份 Arkose 上下文。它会保存每一轮的完整页面截图和 Arkose 题图，
点击当前页面可用的 Submit 后继续等待下一轮，直到挑战终止或达到轮数上限。

它不会调用 BattleProtocolClient.submit_captcha()，也不会把 token 注入注册
会话；因此该工作流是快照/数据采集工作流，不是完整注册工作流。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import register_ruyipage_v4 as v4
import register_ruyipage_v5 as v5
from battle_protocol_flow_v4 import BattleProtocolClient, PersistentFlowState


LOG = logging.getLogger("funcaptcha_http_snapshots")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "funcaptcha_http_snapshot_debug"
TERMINAL_STATUSES = frozenset(
    {
        "onFailed",
        "onError",
        "run-error",
        "setConfig-error",
        "script-error",
        "onCompleted",
    }
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
    formatter = logging.Formatter("%(asctime)s [HTTP-SNAPSHOT] %(message)s", "%H:%M:%S")
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
    parser = v5.build_parser()
    parser.description = "HTTP 持久化注册到 captcha-gate + RuyiPage 多轮题图采集"
    parser.set_defaults(
        output_dir=os.environ.get("SNAPSHOT_OUTPUT_DIR", str(DEFAULT_OUTPUT_ROOT)),
        solver="v11",
        browser="ruyipage",
        email_source="generated",
        country=os.environ.get("SNAPSHOT_COUNTRY", "USA"),
        headless=bool(str(os.environ.get("HEADLESS", "1")).strip() not in {"", "0", "false", "no"}),
        debug_screenshots=True,
    )
    parser.add_argument(
        "--capture-submit-timeout",
        type=float,
        default=float(os.environ.get("CAPTURE_SUBMIT_TIMEOUT", "6")),
        help="等待并点击当前 Arkose Submit 按钮的秒数",
    )
    parser.add_argument(
        "--capture-submit-wait",
        type=float,
        default=float(os.environ.get("CAPTURE_SUBMIT_WAIT", "1.2")),
        help="点击 Submit 后等待下一轮题图/终态的秒数",
    )
    parser.add_argument(
        "--capture-image-timeout",
        type=float,
        default=float(os.environ.get("CAPTURE_IMAGE_TIMEOUT", "30")),
        help="每轮等待新题图的秒数",
    )
    return parser


def public_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Do not put the generated password into an uploaded snapshot artifact."""
    return {
        "email": str(identity.get("email") or ""),
        "battleTag": str(identity.get("battle_tag") or ""),
        "country": str(identity.get("country") or ""),
    }


def read_challenge_question(page: Any) -> dict[str, Any]:
    script = """return (() => {
      const selectors = [
        '#root h2 span',
        '[role="text"]',
        '#root h2',
        'h2'
      ];
      for (const selector of selectors) {
        const node = document.querySelector(selector);
        const text = node && (node.innerText || node.textContent || '').trim();
        if (text) return {selector, text};
      }
      return {selector: null, text: ''};
    })();"""
    for context in v4.base.all_contexts(page):
        with contextlib.suppress(Exception):
            value = context.run_js(script, timeout=3)
            if isinstance(value, dict) and value.get("text"):
                return {"context": str(getattr(context, "url", "") or ""), **value}
    return {"context": "", "selector": None, "text": ""}


def solver_terminal_reason(page: Any) -> str:
    state = v4.v3.solver_state(page)
    status = str(state.get("status") or "").strip()
    if status in TERMINAL_STATUSES:
        if status == "onCompleted":
            payload = state.get("completedPayload") or {}
            if isinstance(payload, dict) and payload.get("tokenLength"):
                return "Arkose completed (token observed; token intentionally not used)"
            return "Arkose completed"
        detail = str(state.get("error") or "").strip()
        return f"Arkose terminal status: {status}{(': ' + detail) if detail else ''}"
    rejection = v4.v3.completion_rejection_reason(state.get("completedPayload"))
    if rejection:
        return f"Arkose completion rejected: {rejection}"
    captcha_state = v4.base.captcha_state(page)
    if captcha_state in {"success", "rejected"}:
        return f"captcha state: {captcha_state}"
    return ""


def wait_next_image(
    catcher: Any,
    seen_records: set[str],
    timeout: float,
    page: Any,
) -> Optional[dict[str, Any]]:
    """Wait by request id, so identical images from separate rounds are retained."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if solver_terminal_reason(page):
                return None
        with catcher._lock:
            records = [dict(item) for item in catcher.captured_images if item.get("body_bytes")]
        records.sort(key=lambda item: (item.get("timestamp") or 0.0, item.get("requestId") or ""))
        for record in records:
            data = record.get("body_bytes") or b""
            key = str(record.get("requestId") or "") or hashlib.sha256(data).hexdigest()
            if key in seen_records:
                continue
            size = record.get("size") or v4.v3.image_size(data)
            if size:
                width, height = size
                url = str(record.get("url") or "").lower()
                is_rtig = "/rtig/image" in url
                valid = (is_rtig and 300 <= height <= 650) or (width >= 800 and 300 <= height <= 650)
                seen_records.add(key)
                if not valid:
                    continue
            else:
                seen_records.add(key)
            return record
        with contextlib.suppress(Exception):
            catcher._event.wait(0.35)
            catcher._event.clear()
        time.sleep(0.05)
    return None


def save_capture_record(record: dict[str, Any], images_dir: Path, wave: int) -> Path:
    images_dir.mkdir(parents=True, exist_ok=True)
    data = record.get("body_bytes") or b""
    digest = str(record.get("sha256") or hashlib.sha256(data).hexdigest())
    extension = v4.v3.image_ext(str(record.get("mime") or ""), data)
    path = images_dir / f"captcha_wave_{wave:02d}_{digest[:12]}{extension}"
    path.write_bytes(data)
    metadata = {key: value for key, value in record.items() if key != "body_bytes"}
    metadata.update({"file": str(path), "sha256": digest, "bytes": len(data), "wave": wave})
    write_json(images_dir / f"captcha_wave_{wave:02d}_{digest[:12]}.json", metadata)
    return path


def capture_challenge(
    page: Any,
    catcher: Any,
    args: argparse.Namespace,
    out: Path,
) -> dict[str, Any]:
    images_dir = out / "captcha_images"
    screenshots_dir = out / "solver_screenshots"
    records: list[dict[str, Any]] = []
    seen_records: set[str] = set()

    if not v4.v3.ensure_verify_or_image(page, catcher, float(args.verify_timeout)):
        LOG.warning("点击 Verify 后暂未观察到题图，继续等待")

    for wave in range(max(1, int(args.max_waves))):
        record = wait_next_image(catcher, seen_records, float(args.capture_image_timeout), page)
        if record is None:
            reason = solver_terminal_reason(page) or "等待新题图超时"
            LOG.info("题图采集结束：%s", reason)
            return {"ok": bool(records), "reason": reason, "rounds": records}

        image_path = save_capture_record(record, images_dir, wave)
        before_path = screenshots_dir / f"wave_{wave:02d}_before_submit.png"
        after_path = screenshots_dir / f"wave_{wave:02d}_after_submit.png"
        v4.base.screenshot(page, before_path, full_page=True)
        question = read_challenge_question(page)
        state_before = v4.v3.solver_state(page)
        LOG.info(
            "已保存第 %s 轮题图：%s，尺寸=%s，question=%s",
            wave + 1,
            image_path,
            record.get("size"),
            question.get("text") or "<empty>",
        )

        submit_ok = v4.v3.click_submit(page, timeout=float(args.capture_submit_timeout))
        v4.base.screenshot(page, after_path, full_page=True)
        action = {
            "wave": wave,
            "image": str(image_path),
            "sha256": record.get("sha256"),
            "requestId": record.get("requestId"),
            "question": question,
            "submitClicked": bool(submit_ok),
            "stateBefore": state_before,
            "stateAfterSubmit": v4.v3.solver_state(page),
            "beforeScreenshot": str(before_path),
            "afterScreenshot": str(after_path),
        }
        records.append(action)
        write_json(out / "captured_rounds.json", records)

        if not submit_ok:
            LOG.warning("第 %s 轮没有可点击的 Submit，停止采集", wave + 1)
            return {"ok": bool(records), "reason": "submit-not-clicked", "rounds": records}

        time.sleep(max(0.0, float(args.capture_submit_wait)))
        reason = solver_terminal_reason(page)
        if reason:
            LOG.info("挑战提交后进入终态：%s", reason)
            return {"ok": bool(records), "reason": reason, "rounds": records}

    return {
        "ok": bool(records),
        "reason": f"达到 max_waves={args.max_waves}",
        "rounds": records,
    }


def main() -> int:
    force_utf8_stdio()
    args = build_parser().parse_args()
    # 快照工作流固定使用 V6 同款自动邮箱、HTTP 持久化和 RuyiPage。
    args.email_source = "generated"
    args.solver = "v11"
    args.browser = "ruyipage"
    args.debug_screenshots = True
    v4.configure_v3_clicks(args)
    config = v5.validate_configuration(args)
    output_root = Path(args.output_dir).expanduser().resolve()
    out = output_root / run_id()
    out.mkdir(parents=True, exist_ok=True)
    args.output_dir = str(out)
    setup_logging(out / "run.log")

    state: Optional[PersistentFlowState] = None
    page = None
    catcher = None
    optimizer = None
    meter = None
    started = time.perf_counter()
    try:
        proxy = v4.parse_proxy(args.proxy)
        identity = v4.configured_identity(args)
        state = PersistentFlowState.create(
            out / "persistent_state.json",
            identity=identity,
            profile={
                "mode": "http-persistent-ruyipage-snapshot",
                "registrationCountry": str(args.country).upper(),
                "solver": "capture-only",
                "browser": "ruyipage",
                "proxy": proxy.summary(),
            },
        )
        write_json(out / "account_generated.json", public_identity(identity))
        write_json(out / "snapshot_configuration.json", config)
        LOG.info("输出目录：%s", out)
        LOG.info("架构：自动生成邮箱 -> HTTP 持久化到 captcha-gate -> RuyiPage 多轮抓图")
        LOG.info("账号标识：%s", identity.get("email"))

        runtime_proxy_url: Optional[str] = proxy.url
        if proxy.enabled and proxy.url:
            meter = v5.ProxyTrafficMeter(proxy.url, direct_hosts=config["proxyDirectHosts"])
            runtime_proxy_url = meter.start()
            LOG.info("代理流量计已启动：%s", proxy.display)

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
        client.run_to_captcha(
            country=str(args.country).upper(),
            opt_in=False,
            country_probe=bool(args.country_probe),
        )
        arkose = dict(state.data.get("arkose") or {})
        if not arkose.get("blob"):
            arkose = client.recover_arkose_from_last_response()
        if not arkose.get("blob"):
            raise RuntimeError("HTTP captcha-gate 响应中没有 Arkose blob")
        write_json(out / "arkose_context.json", v4.public_arkose_context(arkose))
        LOG.info(
            "已取得 Arkose 上下文：siteKey=%s，blob 长度=%s",
            arkose.get("siteKey"),
            len(str(arkose.get("blob") or "")),
        )

        page, catcher, optimizer = v5._launch_solver_browser(args, proxy, runtime_proxy_url)
        catcher.start()
        harness = v4.base.build_solver_harness(
            str(arkose.get("siteKey") or v4.DEFAULT_SITE_KEY),
            str(arkose.get("blob") or ""),
            str(arkose.get("surl") or v4.DEFAULT_SURL),
        )
        origin = v4.replace_document_low_traffic(
            page,
            str(arkose.get("websiteURL") or args.entry_url),
            harness,
        )
        write_json(out / "solver" / "origin.json", origin)
        v4.base.screenshot(page, out / "solver_screenshots" / "harness_loaded.png", full_page=True)

        result = capture_challenge(page, catcher, args, out)
        result["durationSeconds"] = round(time.perf_counter() - started, 3)
        result["account"] = public_identity(identity)
        if meter is not None:
            with contextlib.suppress(Exception):
                result["proxyTraffic"] = v4.capture_proxy_traffic_snapshot(meter)
        write_json(out / "summary.json", result)
        return 0 if result.get("ok") else 1
    except Exception as exc:
        LOG.exception("运行失败：%s", exc)
        write_json(
            out / "summary.json",
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "durationSeconds": round(time.perf_counter() - started, 3),
            },
        )
        return 1
    finally:
        with contextlib.suppress(Exception):
            if catcher:
                catcher.stop()
        with contextlib.suppress(Exception):
            if optimizer:
                v4.stop_browser_optimizer(optimizer, out)
        if args.keep_open and page is not None:
            with contextlib.suppress(EOFError):
                input("RuyiPage 浏览器保持打开，按 Enter 关闭……")
        with contextlib.suppress(Exception):
            if page:
                page.quit()
        with contextlib.suppress(Exception):
            if meter is not None:
                meter.stop()


if __name__ == "__main__":
    raise SystemExit(main())
