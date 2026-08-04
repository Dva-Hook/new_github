# -*- coding: utf-8 -*-
"""Concurrent direct-network verification for V6-exported Battle.net accounts.

Input records use the same three-line format emitted by the V3 verifier and
the V6 registration collector::

    账号：battle@example.com
    密码：battle-password
    API：battle@example.com----mailbox-password----client-id----refresh-token

Each account gets its own RuyiPage profile and output directory. The browser
session is direct (no proxy), while Microsoft Graph is read using the OAuth
credential embedded in that account's API line.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional
from uuid import uuid4

import register_ruyipage_v4 as v4
import v6_email_pool
import v6_email_verifier as v6
from v5_email_verifier import EmailVerificationResult


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ACCOUNTS_FILE = PROJECT_ROOT / "oath2_account.v3.txt"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "email_verify_ruyipage_v3" / "runs"
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "email_verify_browser_profiles"
MAX_PARALLEL = 20

LOG = logging.getLogger("http_register_v6.concurrent_email_verifier")

OVERVIEW_BANNER_STATE_JS = f"""return (() => {{
  const node = document.querySelector({json.dumps(v6.OVERVIEW_VERIFICATION_BANNER_SELECTOR)});
  if (!node) return {{
    present: false,
    visible: false,
    readyState: String(document.readyState || ''),
    text: ''
  }};
  const style = getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return {{
    present: true,
    visible: style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0,
    readyState: String(document.readyState || ''),
    text: String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim()
  }};
}})();"""

_LABEL_PATTERNS = {
    "email": re.compile(r"^(?:账号|帳號|account)\s*[:：]\s*(.+)$", re.IGNORECASE),
    "password": re.compile(r"^(?:密码|密碼|password)\s*[:：]\s*(.*)$", re.IGNORECASE),
    "api": re.compile(r"^api\s*[:：]\s*(.*)$", re.IGNORECASE),
}


@dataclass(frozen=True)
class AccountRecord:
    email: str
    password: str
    credential: v6_email_pool.EmailCredential
    source_index: int

    @property
    def api_line(self) -> str:
        return self.credential.raw_line


@dataclass(frozen=True)
class AccountResult:
    account: AccountRecord
    result: EmailVerificationResult
    elapsed_seconds: float
    output_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceIndex": self.account.source_index,
            "email": self.account.email,
            "ok": self.result.ok,
            "status": self.result.status,
            "note": self.result.note,
            "scannedMessages": self.result.scanned_messages,
            "matchingMessages": self.result.matching_messages,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "outputDir": str(self.output_dir),
        }


def _match_label(line: str, kind: str) -> Optional[str]:
    match = _LABEL_PATTERNS[kind].match(line)
    return match.group(1).strip() if match else None


def parse_account_records(text: str) -> list[AccountRecord]:
    """Parse V3 blocks and keep the exact API line for later Graph access."""

    records: list[AccountRecord] = []
    seen: set[str] = set()
    pending_email: Optional[str] = None
    pending_password: Optional[str] = None
    pending_line = 0

    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        email = _match_label(line, "email")
        if email is not None:
            if pending_email is not None:
                raise ValueError(
                    f"line {line_number}: previous account at line {pending_line} is incomplete"
                )
            if not v6_email_pool.EMAIL_RE.fullmatch(email):
                raise ValueError(f"line {line_number}: invalid account email")
            pending_email = email
            pending_password = None
            pending_line = line_number
            continue
        password = _match_label(line, "password")
        if password is not None:
            if pending_email is None:
                raise ValueError(f"line {line_number}: password has no account")
            if pending_password is not None:
                raise ValueError(f"line {line_number}: duplicate password")
            if not password:
                raise ValueError(f"line {line_number}: password is empty")
            pending_password = password
            continue
        api_line = _match_label(line, "api")
        if api_line is not None:
            if pending_email is None or pending_password is None:
                raise ValueError(f"line {line_number}: API appears before a complete account")
            credential = v6_email_pool.parse_credential_line(
                api_line,
                source_index=line_number,
            )
            if credential.email.casefold() != pending_email.casefold():
                raise ValueError(
                    f"line {line_number}: account/API email mismatch: "
                    f"{pending_email} != {credential.email}"
                )
            normalized = pending_email.casefold()
            if normalized in seen:
                raise ValueError(f"duplicate account email: {pending_email}")
            seen.add(normalized)
            records.append(
                AccountRecord(
                    email=pending_email,
                    password=pending_password,
                    credential=credential,
                    source_index=pending_line,
                )
            )
            pending_email = None
            pending_password = None
            pending_line = 0
            continue
        raise ValueError(f"line {line_number}: unsupported account-file row: {line!r}")

    if pending_email is not None:
        raise ValueError(f"line {pending_line}: account is missing password or API")
    if not records:
        raise ValueError("account file contains no accounts")
    return records


def read_account_records(path: Path) -> list[AccountRecord]:
    if not path.is_file():
        raise FileNotFoundError(f"account file does not exist: {path}")
    return parse_account_records(path.read_text(encoding="utf-8-sig"))


def select_account_records(
    accounts: list[AccountRecord], account_index: int
) -> list[AccountRecord]:
    """Select one deterministic one-based record for a GitHub matrix job."""

    index = int(account_index)
    if index == 0:
        return list(accounts)
    if index < 1 or index > len(accounts):
        raise IndexError(
            f"--account-index must be 1..{len(accounts)} (or 0 for all accounts)"
        )
    return [accounts[index - 1]]


def read_banner_state(page: Any) -> dict[str, Any]:
    state = page.run_js(OVERVIEW_BANNER_STATE_JS, timeout=10)
    return dict(state) if isinstance(state, dict) else {}


def has_visible_unverified_banner(page: Any, timeout: float = 12.0) -> bool:
    """Return only the requested overview-banner signal, not generic page text."""

    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            state = read_banner_state(page)
            if state.get("visible"):
                return True
            if state.get("present") is False and state.get("readyState") == "complete":
                # The overview has rendered without the exact unverified banner.
                return False
        time.sleep(0.5)
    return False


def _email_verifier_args(args: argparse.Namespace, cache_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        headless=bool(args.headless),
        no_resource_blocking=False,
        email_login_timeout=float(args.email_login_timeout),
        email_mail_timeout=float(args.email_mail_timeout),
        email_verification_timeout=float(args.email_verification_timeout),
        email_browser_cache_dir=str(cache_dir),
    )


def _complete_login_challenge(
    page: Any,
    account: AccountRecord,
    args: argparse.Namespace,
) -> EmailVerificationResult:
    login_result = v6.login_battle_net(
        page,
        account.credential.to_v5().email,
        account.password,
        timeout=float(args.email_login_timeout),
    )
    if login_result.ok:
        return login_result
    if login_result.status not in {"manual_email_code", "manual_security_check", "login_failed"}:
        return login_result
    completed, scanned, matching = v6.complete_email_security_challenge(
        page,
        account.credential.to_v5(),
        timeout=float(args.email_mail_timeout),
    )
    if completed:
        return EmailVerificationResult(
            True,
            "logged_in_after_email_code",
            "",
            scanned,
            matching,
        )
    return EmailVerificationResult(
        False,
        login_result.status,
        login_result.note,
        scanned,
        matching,
    )


def _verify_after_login(
    page: Any,
    account: AccountRecord,
    args: argparse.Namespace,
    *,
    not_before: datetime,
) -> EmailVerificationResult:
    login_result = _complete_login_challenge(page, account, args)
    if not login_result.ok:
        return login_result

    # An already verified account may not send a verification message at all.
    state = v6.wait_email_verified(page, timeout=12.0)
    if state.get("verified"):
        return EmailVerificationResult(
            True,
            "already_verified",
            "",
            login_result.scanned_messages,
            login_result.matching_messages,
        )

    # This verifier intentionally treats the absence of the requested overview
    # banner as a successful account state, as requested for the standalone flow.
    if not has_visible_unverified_banner(page):
        LOG.info("%s: no visible unverified-email banner; treating login as successful", account.email)
        return EmailVerificationResult(
            True,
            "no_unverified_banner",
            "",
            login_result.scanned_messages,
            login_result.matching_messages,
        )

    requested_at = v6.request_verification_email(page, check_overview_banner=True)
    if requested_at is None:
        return EmailVerificationResult(True, "already_verified")

    credential = account.credential.to_v5()
    mail_not_before = max(not_before, requested_at)
    timeout = max(1.0, float(args.email_mail_timeout))
    deadline = time.monotonic() + timeout
    scanned = login_result.scanned_messages
    matching = login_result.matching_messages
    link, scanned_now, matching_now = v6.poll_verification_link_attempts(
        credential,
        not_before=mail_not_before,
        attempts=3,
        interval=5.0,
    )
    scanned = max(scanned, scanned_now)
    matching = max(matching, matching_now)

    if not link:
        LOG.warning("%s: three empty mailbox reads; requesting one more message", account.email)
        retry_requested_at = v6.request_verification_email(
            page,
            check_overview_banner=False,
        )
        if retry_requested_at is None:
            return EmailVerificationResult(True, "already_verified", "", scanned, matching)
        link, scanned_now, matching_now = v6.poll_verification_link(
            credential,
            not_before=max(mail_not_before, retry_requested_at),
            timeout=max(1.0, deadline - time.monotonic()),
        )
        scanned = max(scanned, scanned_now)
        matching = max(matching, matching_now)

    if not link:
        return EmailVerificationResult(
            False,
            "verification_mail_missing",
            "unverified-email banner remained, but no verification link was found",
            scanned,
            matching,
        )
    if not v6.open_verification_link(
        page,
        link,
        float(args.email_verification_timeout),
    ):
        return EmailVerificationResult(
            False,
            "verification_not_confirmed",
            "verification link opened but the page did not finish loading",
            scanned,
            matching,
        )
    return EmailVerificationResult(True, "verified", "", scanned, matching)


def verify_account(
    account: AccountRecord,
    *,
    args: argparse.Namespace,
    run_dir: Path,
    cache_root: Path,
) -> AccountResult:
    started = time.perf_counter()
    account_dir = run_dir / f"account_{account.source_index:04d}"
    account_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_root / f"account_{account.source_index:04d}"
    job_args = _email_verifier_args(args, cache_dir)
    page: Any = None
    actual_cache_dir = cache_dir
    result = EmailVerificationResult(False, "not_started", "")
    direct_proxy = v4.ProxySettings(None, "direct")
    try:
        LOG.info("[%s] starting direct RuyiPage verification", account.email)
        page, actual_cache_dir = v6.launch_cached_ruyi_browser(
            job_args,
            direct_proxy,
            None,
            account_dir,
        )
        result = _verify_after_login(
            page,
            account,
            args,
            not_before=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        if result.ok:
            LOG.info("[%s] verification finished: %s", account.email, result.status)
        else:
            LOG.warning("[%s] verification failed: %s", account.email, result.note)
        return AccountResult(
            account,
            result,
            time.perf_counter() - started,
            account_dir,
        )
    except Exception as exc:
        LOG.exception("[%s] verification raised %s", account.email, type(exc).__name__)
        result = EmailVerificationResult(
            False,
            "verification_error",
            f"{type(exc).__name__}: {exc}",
        )
        return AccountResult(account, result, time.perf_counter() - started, account_dir)
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                v6.close_cached_ruyi_browser(page, actual_cache_dir)


def _setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [EMAIL-V3] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def render_account_records(accounts: Iterable[AccountRecord]) -> str:
    return "".join(
        f"账号：{account.email}\n密码：{account.password}\nAPI：{account.api_line}\n\n"
        for account in accounts
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Concurrent direct RuyiPage verification for V6-exported accounts"
    )
    parser.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE))
    parser.add_argument(
        "--account-index",
        type=int,
        default=0,
        help="one-based account index for a GitHub matrix job; 0 processes all accounts",
    )
    parser.add_argument("--max-parallel", type=int, default=MAX_PARALLEL)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--email-login-timeout", type=float, default=120.0)
    parser.add_argument("--email-mail-timeout", type=float, default=180.0)
    parser.add_argument("--email-verification-timeout", type=float, default=30.0)
    parser.add_argument("--check-inputs", action="store_true")
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if not 1 <= int(args.max_parallel) <= MAX_PARALLEL:
        raise ValueError(f"--max-parallel must be between 1 and {MAX_PARALLEL}")
    if min(
        float(args.email_login_timeout),
        float(args.email_mail_timeout),
        float(args.email_verification_timeout),
    ) <= 0:
        raise ValueError("email timeouts must be positive")


def run(args: argparse.Namespace) -> int:
    _validate_args(args)
    accounts_path = Path(args.accounts).expanduser().resolve()
    output_root = Path(args.output_dir).expanduser().resolve()
    cache_root = Path(args.cache_dir).expanduser().resolve()
    all_accounts = read_account_records(accounts_path)
    accounts = select_account_records(all_accounts, int(args.account_index))
    if args.check_inputs:
        for index, account in enumerate(accounts, 1):
            print(f"{index}: {account.email}")
        return 0

    run_dir = output_root / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(run_dir / "run.log")
    _write_json(
        run_dir / "input_summary.json",
        {
            "accountsFile": str(accounts_path),
            "accountCount": len(accounts),
            "sourceAccountCount": len(all_accounts),
            "accountIndex": int(args.account_index),
            "maxParallel": int(args.max_parallel),
            "network": "direct",
            "browser": "ruyipage",
            "apiCredentialFields": "email----mailbox_password----client_id----refresh_token",
        },
    )
    LOG.info("input accounts=%d maxParallel=%d network=direct", len(accounts), args.max_parallel)

    results: list[AccountResult] = []
    with ThreadPoolExecutor(max_workers=int(args.max_parallel), thread_name_prefix="email-v3") as executor:
        futures = {
            executor.submit(
                verify_account,
                account,
                args=args,
                run_dir=run_dir,
                cache_root=cache_root,
            ): account
            for account in accounts
        }
        for future in as_completed(futures):
            account = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                LOG.exception("[%s] worker crashed: %s", account.email, exc)
                results.append(
                    AccountResult(
                        account,
                        EmailVerificationResult(False, "worker_error", str(exc)),
                        0.0,
                        run_dir / f"account_{account.source_index:04d}",
                    )
                )

    results.sort(key=lambda item: item.account.source_index)
    rendered = "\n".join(json.dumps(item.to_dict(), ensure_ascii=False) for item in results)
    (run_dir / "results.jsonl").write_text(rendered + ("\n" if rendered else ""), encoding="utf-8")
    successful_accounts = [item.account for item in results if item.result.ok]
    (run_dir / "verified_accounts.txt").write_text(
        render_account_records(successful_accounts),
        encoding="utf-8",
    )
    summary = {
        "total": len(results),
        "success": sum(item.result.ok for item in results),
        "failure": sum(not item.result.ok for item in results),
        "maxParallel": int(args.max_parallel),
        "network": "direct",
        "browser": "ruyipage",
        "runDir": str(run_dir),
    }
    _write_json(run_dir / "summary.json", summary)
    LOG.info("complete success=%d failure=%d output=%s", summary["success"], summary["failure"], run_dir)
    return 0 if summary["failure"] == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"input/runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
