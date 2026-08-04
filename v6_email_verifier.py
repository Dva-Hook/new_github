# -*- coding: utf-8 -*-
"""V6 post-registration email verification extensions.

V5 always waits for a verification-link message after logging in. Battle.net
may already report ``Email Verified`` and therefore send no link. V6 checks the
live account state first and can also complete Battle.net's six-character
e-mail security challenge through the supplied Microsoft Graph credential.
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from v5_email_pool import EmailCredential
from v5_email_verifier import (
    AccessTokenExpired,
    EmailVerificationResult,
    MESSAGES_URL,
    close_cached_ruyi_browser,
    get_access_token,
    launch_cached_ruyi_browser,
    login_battle_net,
    navigate_with_retry,
    poll_verification_link,
    wait_for_verification_success,
)


LOG = logging.getLogger("http_register_v6.email_verifier")
EMAIL_CODE_BOX_SELECTORS = tuple(
    "#password-form > div.control-group.input-container.has-code-input "
    f"> div > div > div:nth-child({index})"
    for index in range(2, 8)
)

EMAIL_VERIFIED_STATE_JS = r"""return (() => {
  const text = String(document.body?.innerText || '');
  const normalized = text.replace(/\s+/g, ' ').trim();
  const verified = /\bemail\s+verified\b/i.test(normalized)
    || /邮箱(?:已经|已)验证|电子邮箱(?:已经|已)验证/.test(normalized);
  const unverified = /\bemail\s+(?:not\s+verified|unverified)\b/i.test(normalized)
    || /\bverify\s+(?:your\s+)?email\b/i.test(normalized)
    || /邮箱未验证|验证(?:您的)?邮箱/.test(normalized);
  return {
    href: String(location.href || ''),
    text: normalized.slice(0, 3000),
    verified: verified && !unverified,
    unverified
  };
})();"""

EMAIL_SECURITY_STAGE_JS = r"""return (() => {
  const href = String(location.href || '');
  const selectors = %s;
  const codeCount = selectors.filter((selector) => {
    const wrapper = document.querySelector(selector);
    return !!(wrapper && (
      wrapper.matches('input,[contenteditable="true"]')
      || wrapper.querySelector('input,[contenteditable="true"]')
    ));
  }).length;
  const selected = Array.from(document.querySelectorAll(
    'select option:checked, input[type="radio"]:checked'
  )).map((node) => `${node.value || ''} ${node.textContent || ''}`).join(' ');
  const bodyText = String(document.body?.innerText || '');
  const submit = !!document.querySelector('#submit');
  const password = !!document.querySelector('#password');
  const hasChoice = !!document.querySelector(
    'select, input[type="radio"], [role="listbox"]'
  );
  let stage = '';
  if (/\/challenge\//i.test(href)) {
    if (codeCount >= 6 || document.querySelector('#password-form .has-code-input')) {
      stage = 'code';
    } else if (submit && !password && (/mail/i.test(selected) || bodyText.includes('@') || hasChoice)) {
      stage = 'choice';
    }
  }
  return {href, stage, codeCount};
})();""" % repr(list(EMAIL_CODE_BOX_SELECTORS)).replace("'", '"')


def read_email_verified_state(page: Any) -> dict[str, Any]:
    state = page.run_js(EMAIL_VERIFIED_STATE_JS, timeout=10)
    return dict(state) if isinstance(state, dict) else {}


def wait_email_verified(page: Any, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            last = read_email_verified_state(page)
            if last.get("verified") or last.get("unverified"):
                return last
        time.sleep(0.5)
    return last


def _message_text(message: dict[str, Any]) -> str:
    content = str(dict(message.get("body") or {}).get("content") or "")
    content = html.unescape(re.sub(r"<[^>]+>", " ", content))
    preview = str(message.get("bodyPreview") or "")
    return re.sub(r"\s+", " ", f"{content} {preview}").strip()


def extract_battlenet_security_code(message: dict[str, Any]) -> Optional[str]:
    text = unicodedata.normalize("NFKC", _message_text(message))
    match = re.search(
        r"(?:security\s+code|verification\s+code|"
        r"验证码|驗證碼|c[oó]digo\s+de\s+seguran[cç]a|보안\s*코드)"
        r".{0,160}?\b([A-Z0-9]{6})\b",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).upper() if match else None


def _parse_graph_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_security_code(
    session: requests.Session,
    access_token: str,
    *,
    not_before: datetime,
) -> tuple[Optional[str], int, int]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="html"',
    }
    params = {
        "$top": "50",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
        "$orderby": "receivedDateTime desc",
    }
    next_url: Optional[str] = MESSAGES_URL
    threshold = not_before.astimezone(timezone.utc)
    scanned = 0
    matching = 0
    pages = 0
    while next_url and pages < 3:
        pages += 1
        response = session.get(
            next_url,
            headers=headers,
            params=params if next_url == MESSAGES_URL else None,
            timeout=30,
        )
        if response.status_code == 401:
            raise AccessTokenExpired("Microsoft access token 已过期")
        response.raise_for_status()
        payload = response.json()
        for message in payload.get("value", []):
            scanned += 1
            sender = dict(
                dict(message.get("from") or {}).get("emailAddress") or {}
            )
            sender_address = str(sender.get("address") or "").strip().casefold()
            sender_name = str(sender.get("name") or "").strip().casefold()
            if sender_address != "noreply@battle.net" and sender_name != "battle.net":
                continue
            matching += 1
            received = _parse_graph_datetime(message.get("receivedDateTime"))
            if received is None or received < threshold:
                continue
            code = extract_battlenet_security_code(message)
            if code:
                return code, scanned, matching
        next_url = payload.get("@odata.nextLink")
    return None, scanned, matching


def poll_security_code(
    credential: EmailCredential,
    *,
    not_before: datetime,
    timeout: float,
    interval: float = 5.0,
) -> tuple[str, int, int]:
    deadline = time.monotonic() + max(1.0, float(timeout))
    refresh_token = credential.refresh_token
    scanned_total = 0
    matching_total = 0
    with requests.Session() as session:
        session.trust_env = False
        session.headers["User-Agent"] = "BattleNetV6EmailSecurity/1.0"
        access_token, refresh_token = get_access_token(
            session, credential.client_id, refresh_token
        )
        while time.monotonic() < deadline:
            try:
                code, scanned, matching = find_security_code(
                    session,
                    access_token,
                    not_before=not_before,
                )
            except AccessTokenExpired:
                access_token, refresh_token = get_access_token(
                    session, credential.client_id, refresh_token
                )
                continue
            scanned_total = max(scanned_total, scanned)
            matching_total = max(matching_total, matching)
            if code:
                return code, scanned_total, matching_total
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(float(interval), remaining))
    raise TimeoutError("等待 Battle.net 邮箱安全码超时")


def detect_email_security_stage(page: Any, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            state = page.run_js(EMAIL_SECURITY_STAGE_JS, timeout=5)
            if isinstance(state, dict) and state.get("stage") in {"choice", "code"}:
                return str(state["stage"])
        time.sleep(0.25)
    return ""


def fill_email_security_code(page: Any, code: str) -> None:
    normalized = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", normalized):
        raise ValueError("邮箱安全码必须是 6 位字母或数字")
    first = page.ele(EMAIL_CODE_BOX_SELECTORS[0], timeout=15)
    try:
        first.click()
    except Exception:
        first.click(by_js=True)
    page.actions.type(normalized, interval=120).perform()


def complete_email_security_challenge(
    page: Any,
    credential: EmailCredential,
    *,
    timeout: float,
) -> tuple[bool, int, int]:
    stage = detect_email_security_stage(page)
    if not stage:
        return False, 0, 0
    requested_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    if stage == "choice":
        LOG.info("检测到 Battle.net 邮箱安全验证方式选择页，自动提交 E-Mail")
        submit = page.ele("#submit", timeout=15)
        try:
            submit.click()
        except Exception:
            submit.click(by_js=True)
        code_stage = detect_email_security_stage(page, timeout=20.0)
        if code_stage != "code":
            raise RuntimeError("邮箱安全验证未进入验证码输入页")
    else:
        requested_at -= timedelta(minutes=10)
    code, scanned, matching = poll_security_code(
        credential,
        not_before=requested_at,
        timeout=timeout,
    )
    LOG.info(
        "已获取 Battle.net 六位邮箱安全码: scanned=%s matching=%s",
        scanned,
        matching,
    )
    fill_email_security_code(page, code)
    deadline = time.monotonic() + max(10.0, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            state = read_email_verified_state(page)
            href = str(state.get("href") or "")
            if state.get("verified") or (
                "/challenge/" not in href and "/login/" not in href
            ):
                return True, scanned, matching
        time.sleep(0.5)
    return False, scanned, matching


def verify_registered_email(
    credential: EmailCredential,
    battle_password: str,
    *,
    args: Any,
    proxy: Any,
    runtime_proxy_url: Optional[str],
    output_dir: Path,
    not_before: datetime,
) -> EmailVerificationResult:
    page: Any = None
    cache_dir: Optional[Path] = None
    scanned = 0
    matching = 0
    try:
        LOG.info("启动 V6 RuyiPage 执行注册邮箱状态确认: %s", credential.email)
        page, cache_dir = launch_cached_ruyi_browser(
            args,
            proxy,
            runtime_proxy_url,
            output_dir,
        )
        login_result = login_battle_net(
            page,
            credential.email,
            battle_password,
            timeout=float(args.email_login_timeout),
        )
        if not login_result.ok and login_result.status in {
            "manual_email_code",
            "manual_security_check",
            "login_failed",
        }:
            completed, scanned, matching = complete_email_security_challenge(
                page,
                credential,
                timeout=float(args.email_mail_timeout),
            )
            if completed:
                login_result = EmailVerificationResult(True, "logged_in")
        if not login_result.ok:
            return login_result

        state = wait_email_verified(page, timeout=12.0)
        if state.get("verified"):
            LOG.info("Battle.net 账号页面已确认 Email Verified: %s", credential.email)
            return EmailVerificationResult(
                True,
                "already_verified",
                "",
                scanned,
                matching,
            )

        link, link_scanned, link_matching = poll_verification_link(
            credential,
            not_before=not_before,
            timeout=float(args.email_mail_timeout),
        )
        scanned = max(scanned, link_scanned)
        matching = max(matching, link_matching)
        if not link:
            state = wait_email_verified(page, timeout=3.0)
            if state.get("verified"):
                return EmailVerificationResult(
                    True,
                    "already_verified",
                    "",
                    scanned,
                    matching,
                )
            return EmailVerificationResult(
                False,
                "verification_mail_missing",
                "账号页未显示 Email Verified，且等待时间内没有验证邮件",
                scanned,
                matching,
            )

        LOG.info(
            "已提取最新 Battle.net 验证链接: scanned=%s matching=%s",
            scanned,
            matching,
        )
        navigate_with_retry(page, link, "Battle.net 邮箱验证链接")
        if not wait_for_verification_success(
            page, float(args.email_verification_timeout)
        ) and not wait_email_verified(page, timeout=3.0).get("verified"):
            return EmailVerificationResult(
                False,
                "verification_not_confirmed",
                "已打开验证链接，但页面未确认验证成功",
                scanned,
                matching,
            )
        return EmailVerificationResult(True, "verified", "", scanned, matching)
    except Exception as exc:
        LOG.warning("V6 注册邮箱自动验证失败: %s", type(exc).__name__)
        return EmailVerificationResult(
            False,
            "verification_error",
            f"邮箱自动验证发生错误（{type(exc).__name__}），需手动检查",
            scanned,
            matching,
        )
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                close_cached_ruyi_browser(
                    page,
                    cache_dir or Path(args.email_browser_cache_dir),
                )


__all__ = [
    "complete_email_security_challenge",
    "detect_email_security_stage",
    "extract_battlenet_security_code",
    "find_security_code",
    "poll_security_code",
    "read_email_verified_state",
    "verify_registered_email",
    "wait_email_verified",
]

