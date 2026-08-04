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
    find_link,
    get_access_token,
    launch_cached_ruyi_browser,
    login_battle_net,
    navigate_with_retry,
    poll_verification_link,
    wait_element,
)


LOG = logging.getLogger("http_register_v6.email_verifier")
OVERVIEW_VERIFICATION_BANNER_SELECTOR = (
    "#app > main > section.main-content-section > div > "
    "div.blz-alert.top-banner-alert > meka-notification-banner > div > span > "
    "span.banner-link > a"
)
EMAIL_DETAILS_URL = "https://account.battle.net/details#email-card"
EMAIL_DETAILS_RESEND_SELECTOR = (
    "#email-card > div.blz-card-alert > div > div > "
    "meka-notification-banner > div > span > span > span:nth-child(2) > a"
)
EMAIL_DETAILS_RESEND_FALLBACK_SELECTORS = (
    "#email-card .blz-card-alert meka-notification-banner a",
    "#email-card .blz-card-alert a",
)
EMAIL_DETAILS_RESEND_DOM_JS = r"""return (() => {
  const anchors = Array.from(document.querySelectorAll(
    '#email-card .blz-card-alert a, #email-card meka-notification-banner a'
  ));
  const normalized = (node) => String(
    node?.innerText || node?.textContent || node?.getAttribute?.('aria-label') || ''
  ).replace(/\s+/g, ' ').trim();
  const resendPattern = /\bresend\b|send\s+(?:the\s+)?verification|重新发送|重发|再次发送|发送验证/i;
  const semantic = anchors.find((node) => resendPattern.test(normalized(node)));
  const candidate = semantic || (anchors.length === 1 ? anchors[0] : null);
  if (!candidate) {
    return {clicked: false, candidates: anchors.length};
  }
  candidate.scrollIntoView({block: 'center', inline: 'center'});
  candidate.click();
  return {
    clicked: true,
    candidates: anchors.length,
    method: semantic ? 'semantic-text' : 'single-alert-link'
  };
})();"""
EMAIL_CODE_BOX_SELECTORS = tuple(
    "#password-form > div.control-group.input-container.has-code-input "
    f"> div > div > div:nth-child({index})"
    for index in range(2, 8)
)

EMAIL_VERIFIED_STATE_JS = r"""return (() => {
  const text = String(document.body?.innerText || '');
  const normalized = text.replace(/\s+/g, ' ').trim();
  const verified = /\bemail\s+verified\b/i.test(normalized)
    || /(?:电子)?邮箱(?:已经|已)验证/.test(normalized)
    || /(?:电子)?邮箱.{0,160}?(?:已经|已)验证/.test(normalized);
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


def _optional_element(page: Any, selector: str, timeout: float) -> Any:
    try:
        return wait_element(page, selector, timeout)
    except TimeoutError:
        return None


def _click_element(element: Any) -> None:
    try:
        element.click()
    except Exception:
        element.click(by_js=True)


def request_verification_email(
    page: Any,
    *,
    check_overview_banner: bool = True,
) -> Optional[datetime]:
    """Open the e-mail card and request a fresh Battle.net verification link.

    The overview banner is treated as a useful unverified-state marker, not as
    the only way to reach the e-mail card. Going to the canonical details URL
    keeps the flow working when login lands on another account page or when the
    overview banner is rendered late.
    """

    if check_overview_banner:
        banner = _optional_element(
            page,
            OVERVIEW_VERIFICATION_BANNER_SELECTOR,
            timeout=6.0,
        )
        if banner is not None:
            LOG.info("检测到 Battle.net 账号概览页的邮箱未验证横幅")
        else:
            LOG.warning("账号页未找到邮箱未验证横幅，继续直接检查电子邮箱卡片")

    navigate_with_retry(page, EMAIL_DETAILS_URL, "Battle.net 电子邮箱详情页")

    # Capture the boundary before any click path (including the JavaScript
    # semantic fallback, which performs the click inside run_js).
    requested_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    resend = _optional_element(
        page,
        EMAIL_DETAILS_RESEND_SELECTOR,
        timeout=20.0,
    )
    used_selector = EMAIL_DETAILS_RESEND_SELECTOR
    if resend is None:
        dom_result: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            raw_result = page.run_js(EMAIL_DETAILS_RESEND_DOM_JS, timeout=10)
            if isinstance(raw_result, dict):
                dom_result = dict(raw_result)
        if dom_result.get("clicked"):
            used_selector = f"dom:{dom_result.get('method') or 'fallback'}"
        else:
            for selector in EMAIL_DETAILS_RESEND_FALLBACK_SELECTORS:
                resend = _optional_element(page, selector, timeout=2.0)
                if resend is not None:
                    used_selector = selector
                    break
    clicked_by_dom = used_selector.startswith("dom:")
    if resend is None and not clicked_by_dom:
        state = wait_email_verified(page, timeout=2.0)
        if state.get("verified"):
            return None
        raise RuntimeError("verification-resend-link-not-found")

    # The timestamp is captured before the click so a very fast Graph delivery
    # cannot race ahead of the filter boundary. Match the established V5-style
    # mailbox flow by giving Battle.net five seconds to enqueue the message
    # before the first Graph read.
    if not clicked_by_dom:
        _click_element(resend)
    LOG.info("已点击 Battle.net 重新发送验证邮件链接: selector=%s", used_selector)
    time.sleep(5.0)
    LOG.info("重新发送后已等待 5 秒，开始读取验证邮件")
    return requested_at


def poll_verification_link_attempts(
    credential: EmailCredential,
    *,
    not_before: datetime,
    attempts: int = 3,
    interval: float = 5.0,
) -> tuple[Optional[str], int, int]:
    """Read Graph exactly ``attempts`` times before the resend recovery path."""

    total_attempts = max(1, int(attempts))
    refresh_token = credential.refresh_token
    scanned_total = 0
    matching_total = 0
    with requests.Session() as session:
        session.trust_env = False
        session.headers["User-Agent"] = "BattleNetV6EmailVerifier/1.0"
        access_token, refresh_token = get_access_token(
            session,
            credential.client_id,
            refresh_token,
        )
        for attempt in range(1, total_attempts + 1):
            while True:
                try:
                    link, scanned, matching = find_link(
                        session,
                        access_token,
                        not_before=not_before,
                    )
                    break
                except AccessTokenExpired:
                    access_token, refresh_token = get_access_token(
                        session,
                        credential.client_id,
                        refresh_token,
                    )
            scanned_total = max(scanned_total, scanned)
            matching_total = max(matching_total, matching)
            LOG.info(
                "第 %s/%s 次读取验证邮件: scanned=%s matching=%s found=%s",
                attempt,
                total_attempts,
                scanned,
                matching,
                bool(link),
            )
            if link:
                return link, scanned_total, matching_total
            if attempt < total_attempts:
                time.sleep(max(0.0, float(interval)))
    return None, scanned_total, matching_total


def wait_document_complete(page: Any, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            state = page.run_js(
                "return {href: String(location.href || ''), "
                "readyState: String(document.readyState || '')};",
                timeout=5,
            )
            if (
                isinstance(state, dict)
                and state.get("readyState") == "complete"
                and str(state.get("href") or "").startswith(("http://", "https://"))
            ):
                return True
        time.sleep(0.25)
    return False


def open_verification_link(page: Any, link: str, timeout: float) -> bool:
    navigate_with_retry(
        page,
        link,
        "Battle.net 邮箱验证链接",
        timeout=max(10.0, float(timeout)),
    )
    if not wait_document_complete(page, timeout):
        return False
    LOG.info("验证链接已在当前登录浏览器中加载完成")
    return True


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

        try:
            requested_at = request_verification_email(page)
        except Exception as exc:
            LOG.warning(
                "Battle.net 验证邮件发送触发失败: %s",
                type(exc).__name__,
            )
            return EmailVerificationResult(
                False,
                "verification_mail_request_failed",
                "账号未验证，但未能点击 Battle.net 重新发送验证邮件链接",
                scanned,
                matching,
            )
        if requested_at is None:
            LOG.info("进入邮箱详情页后账号已显示 Email Verified: %s", credential.email)
            return EmailVerificationResult(
                True,
                "already_verified",
                "",
                scanned,
                matching,
            )

        mail_not_before = max(not_before, requested_at)
        mail_timeout = max(1.0, float(args.email_mail_timeout))
        mail_deadline = time.monotonic() + mail_timeout
        link, link_scanned, link_matching = poll_verification_link_attempts(
            credential,
            not_before=mail_not_before,
            attempts=3,
            interval=5.0,
        )
        scanned = max(scanned, link_scanned)
        matching = max(matching, link_matching)
        if not link:
            LOG.warning("连续 3 次未读取到验证邮件，重新点击一次发送链接")
            try:
                retry_requested_at = request_verification_email(
                    page,
                    check_overview_banner=False,
                )
                if retry_requested_at is None:
                    return EmailVerificationResult(
                        True,
                        "already_verified",
                        "",
                        scanned,
                        matching,
                    )
            except Exception as exc:
                LOG.warning(
                    "第二次触发 Battle.net 验证邮件失败，继续等待第一次请求: %s",
                    type(exc).__name__,
                )
            remaining_timeout = max(1.0, mail_deadline - time.monotonic())
            link, retry_scanned, retry_matching = poll_verification_link(
                credential,
                not_before=mail_not_before,
                timeout=remaining_timeout,
            )
            scanned = max(scanned, retry_scanned)
            matching = max(matching, retry_matching)
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
        if not open_verification_link(
            page,
            link,
            float(args.email_verification_timeout),
        ):
            return EmailVerificationResult(
                False,
                "verification_not_confirmed",
                "已打开验证链接，但页面未在等待时间内加载完成",
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
    "poll_verification_link_attempts",
    "poll_security_code",
    "read_email_verified_state",
    "request_verification_email",
    "open_verification_link",
    "verify_registered_email",
    "wait_document_complete",
    "wait_email_verified",
]
