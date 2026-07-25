# -*- coding: utf-8 -*-
"""Post-registration Battle.net email verification for V5 supplied emails."""

from __future__ import annotations

import contextlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import requests

from v5_email_pool import EmailCredential


LOG = logging.getLogger("http_register_v5.email_verifier")
LOGIN_URL = "https://account.battle.net/overview"
SENDER = "noreply@battle.net"
TOKEN_ENDPOINTS = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.live.com/oauth20_token.srf",
)
MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


NAVIGATION_STATE_JS = r"""return (() => {
  const href = String(document.documentURI || location.href || '');
  const errorText = document.querySelector('#errorShortDescText, .title-text')
    ?.textContent?.trim() || '';
  return {
    href,
    network_error: href.startsWith('about:neterror')
      || !!document.querySelector('#errorPageContainer, .neterror'),
    error_code: errorText
  };
})();"""


LOGIN_STATE_JS = r"""return (() => {
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  };
  const anyVisible = (selector, root = document) =>
    Array.from(root.querySelectorAll(selector)).some(visible);
  const pageText = String(document.body?.innerText || '').toLowerCase();
  const codeInput = anyVisible([
    'input[autocomplete="one-time-code"]',
    'input[id*="security" i]',
    'input[name*="security" i]',
    'input[id*="verification" i]',
    'input[name*="verification" i]',
    'input[id*="otp" i]',
    'input[name*="otp" i]',
    'input[id="code" i]',
    'input[name="code" i]'
  ].join(','));
  const methodChallenge = Array.from(document.querySelectorAll('form')).some(
    (form) => anyVisible('select', form)
      && anyVisible('button[type="submit"], input[type="submit"], button:not([type])', form)
  );
  const emailWords = /e-mail|email|mailbox|邮箱|邮件|verification code|security code/.test(pageText);
  const protectedShell = anyVisible(
    '#code-claim, a[href*="logout"], a[href*="/overview"], '
    + 'a[href*="/details"], #account-settings, .account-overview'
  );
  const href = String(location.href || '');
  return {
    href,
    account_input: anyVisible('#accountName'),
    password_input: anyVisible('#password'),
    email_code_challenge: (codeInput || methodChallenge) && emailWords,
    security_challenge: codeInput || methodChallenge,
    success: protectedShell || (
      /account\.battle\.net\/overview(?:[/?#]|$)/i.test(href)
      && !anyVisible('#accountName, #password')
      && !codeInput
    )
  };
})();"""


VERIFY_STATE_JS = r"""return (() => {
  const banners = Array.from(document.querySelectorAll(
    '.idty-notification-banner__content, .notification, [role="alert"]'
  ));
  const text = banners.map((item) => String(item.innerText || '')).join(' ').toLowerCase();
  return {
    href: String(location.href || ''),
    success: /verified|verification successful|successfully verified|验证成功|已验证/.test(text),
    banner_text: text.slice(0, 300)
  };
})();"""


@dataclass(frozen=True)
class EmailVerificationResult:
    ok: bool
    status: str
    note: str = ""
    scanned_messages: int = 0
    matching_messages: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "note": self.note,
            "scannedMessages": self.scanned_messages,
            "matchingMessages": self.matching_messages,
        }


class AccessTokenExpired(RuntimeError):
    pass


class HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.urls.append(value)


def _all_contexts(page: Any) -> list[Any]:
    contexts = [page]
    with contextlib.suppress(Exception):
        contexts.extend(page.get_all_frames() or [])
    return contexts


def wait_element(page: Any, selector: str, timeout: float) -> Any:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        for context in _all_contexts(page):
            with contextlib.suppress(Exception):
                element = context.ele(selector, timeout=0.25)
                if element:
                    return element
        time.sleep(0.25)
    raise TimeoutError(f"等待元素超时: {selector}")


def click_element(page: Any, selector: str, timeout: float = 30.0) -> None:
    element = wait_element(page, selector, timeout)
    try:
        element.click()
    except Exception:
        element.click(by_js=True)


def close_cookie_banner(page: Any) -> None:
    selectors = (
        "button#onetrust-reject-all-handler",
        "button.ot-reject-all",
        'button[id*="reject"]',
        'button[aria-label*="Reject"]',
    )
    for selector in selectors:
        with contextlib.suppress(Exception):
            element = page.ele(selector, timeout=0.25)
            if element:
                try:
                    element.click()
                except Exception:
                    element.click(by_js=True)
                return


def navigate_with_retry(
    page: Any,
    url: str,
    description: str,
    *,
    attempts: int = 3,
    timeout: float = 60.0,
) -> None:
    last_detail = "unknown"
    for attempt in range(1, attempts + 1):
        error: Optional[Exception] = None
        try:
            page.get(url, wait="interactive", timeout=timeout)
        except Exception as exc:
            error = exc
        try:
            state = page.run_js(NAVIGATION_STATE_JS, timeout=10)
        except Exception as exc:
            state = None
            error = error or exc
        if isinstance(state, dict):
            href = str(state.get("href") or "")
            if not state.get("network_error") and href.startswith(("http://", "https://")):
                return
            last_detail = str(state.get("error_code") or href or last_detail)
        elif error is not None:
            last_detail = type(error).__name__
        if attempt < attempts:
            time.sleep(float(attempt))
    raise RuntimeError(f"{description}连续导航失败: {last_detail}")


def _read_login_state(page: Any) -> dict[str, Any]:
    state = page.run_js(LOGIN_STATE_JS, timeout=5)
    return dict(state) if isinstance(state, dict) else {}


def _wait_login_phase(page: Any, timeout: float) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.1, float(timeout))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            last = _read_login_state(page)
            if last.get("email_code_challenge"):
                return "manual_email_code", last
            if last.get("security_challenge"):
                return "manual_security_check", last
            if last.get("success"):
                return "success", last
            if last.get("password_input"):
                return "password", last
        time.sleep(0.5)
    return "timeout", last


def login_battle_net(
    page: Any,
    email: str,
    password: str,
    *,
    timeout: float,
) -> EmailVerificationResult:
    navigate_with_retry(page, LOGIN_URL, "Battle.net 登录页")
    close_cookie_banner(page)
    account_input = wait_element(page, "#accountName", min(30.0, timeout))
    account_input.input(email, clear=True)
    click_element(page, "#submit")
    phase, _state = _wait_login_phase(page, min(30.0, timeout))
    if phase == "manual_email_code":
        return EmailVerificationResult(
            False,
            "manual_email_code",
            "该账号登录时弹出邮箱验证码，需手动验证",
        )
    if phase == "manual_security_check":
        return EmailVerificationResult(
            False,
            "manual_security_check",
            "该账号登录时弹出安全验证，需手动验证",
        )
    if phase == "success":
        return EmailVerificationResult(True, "logged_in")
    if phase != "password":
        return EmailVerificationResult(False, "login_failed", "登录页未进入密码步骤")

    password_input = wait_element(page, "#password", min(30.0, timeout))
    password_input.input(password, clear=True)
    click_element(page, "#submit")
    phase, _state = _wait_login_phase(page, timeout)
    if phase == "success":
        return EmailVerificationResult(True, "logged_in")
    if phase == "manual_email_code":
        return EmailVerificationResult(
            False,
            "manual_email_code",
            "该账号登录时弹出邮箱验证码，需手动验证",
        )
    if phase == "manual_security_check":
        return EmailVerificationResult(
            False,
            "manual_security_check",
            "该账号登录时弹出安全验证，需手动验证",
        )
    return EmailVerificationResult(False, "login_failed", "Battle.net 登录状态确认超时")


def candidate_urls(content: str) -> Iterable[str]:
    decoded = html.unescape(content or "")
    parser = HrefCollector()
    with contextlib.suppress(Exception):
        parser.feed(decoded)
    yield from parser.urls
    for match in URL_PATTERN.finditer(decoded):
        yield match.group(0).rstrip(".,);]}")


def direct_battlenet_link(candidate: str, depth: int = 0) -> Optional[str]:
    if depth > 2:
        return None
    candidate = html.unescape(candidate).strip().rstrip(".,);]}")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if (
        (parsed.hostname or "").lower() == "account.battle.net"
        and parsed.path.rstrip("/").lower() == "/overview"
        and parse_qs(parsed.query).get("ticket")
    ):
        return candidate
    query = parse_qs(parsed.query)
    for key in ("url", "target", "redirect", "redirecturl", "redirect_uri"):
        for value in query.get(key, ()):
            nested = direct_battlenet_link(value, depth + 1)
            if nested:
                return nested
    return None


def extract_battlenet_link(message: dict[str, Any]) -> Optional[str]:
    body = dict(message.get("body") or {})
    sources = (
        str(body.get("content") or ""),
        str(message.get("bodyPreview") or ""),
    )
    seen: set[str] = set()
    for source in sources:
        for candidate in candidate_urls(source):
            if candidate in seen:
                continue
            seen.add(candidate)
            link = direct_battlenet_link(candidate)
            if link:
                return link
    return None


def _message_sender(message: dict[str, Any]) -> str:
    return str(
        dict(dict(message.get("from") or {}).get("emailAddress") or {}).get(
            "address"
        )
        or ""
    ).strip().lower()


def _message_is_recent(message: dict[str, Any], not_before: datetime) -> bool:
    raw = str(message.get("receivedDateTime") or "")
    if not raw:
        return False
    try:
        received = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return received >= not_before.astimezone(timezone.utc)


def get_access_token(
    session: requests.Session, client_id: str, refresh_token: str
) -> tuple[str, str]:
    errors: list[str] = []
    for endpoint in TOKEN_ENDPOINTS:
        try:
            response = session.post(
                endpoint,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{urlsplit(endpoint).netloc}: {type(exc).__name__}")
            continue
        access_token = data.get("access_token")
        if access_token:
            return str(access_token), str(data.get("refresh_token") or refresh_token)
        errors.append(f"{urlsplit(endpoint).netloc}: token-rejected")
    raise RuntimeError("Microsoft access token 获取失败；" + "；".join(errors))


def find_link(
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
    scanned = 0
    sender_matches = 0
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
            if _message_sender(message) != SENDER:
                continue
            if not _message_is_recent(message, not_before):
                continue
            sender_matches += 1
            link = extract_battlenet_link(message)
            if link:
                return link, scanned, sender_matches
        next_url = payload.get("@odata.nextLink")
    return None, scanned, sender_matches


def poll_verification_link(
    credential: EmailCredential,
    *,
    not_before: datetime,
    timeout: float,
    interval: float = 5.0,
) -> tuple[Optional[str], int, int]:
    deadline = time.monotonic() + max(1.0, float(timeout))
    refresh_token = credential.refresh_token
    scanned_total = 0
    matching_total = 0
    with requests.Session() as session:
        session.trust_env = False
        session.headers["User-Agent"] = "BattleNetV5EmailVerifier/1.0"
        access_token, refresh_token = get_access_token(
            session, credential.client_id, refresh_token
        )
        while time.monotonic() < deadline:
            try:
                link, scanned, matching = find_link(
                    session, access_token, not_before=not_before
                )
            except AccessTokenExpired:
                access_token, refresh_token = get_access_token(
                    session, credential.client_id, refresh_token
                )
                continue
            scanned_total = max(scanned_total, scanned)
            matching_total = max(matching_total, matching)
            if link:
                return link, scanned_total, matching_total
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(float(interval), remaining))
    return None, scanned_total, matching_total


def wait_for_verification_success(page: Any, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            state = page.run_js(VERIFY_STATE_JS, timeout=5)
            if isinstance(state, dict) and state.get("success"):
                return True
        time.sleep(0.5)
    return False


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
    import register_ruyipage_v4 as v4

    page: Any = None
    try:
        LOG.info("启动 RuyiPage 执行注册邮箱验证: %s", credential.email)
        page = v4.launch_ruyi_browser(args, proxy, runtime_proxy_url)
        login_result = login_battle_net(
            page,
            credential.email,
            battle_password,
            timeout=float(args.email_login_timeout),
        )
        if not login_result.ok:
            return login_result
        link, scanned, matching = poll_verification_link(
            credential,
            not_before=not_before,
            timeout=float(args.email_mail_timeout),
        )
        if not link:
            return EmailVerificationResult(
                False,
                "verification_mail_missing",
                "未在等待时间内提取到 Battle.net 验证邮件，需手动验证",
                scanned,
                matching,
            )
        LOG.info(
            "已提取最新 Battle.net 验证链接，扫描邮件=%s 匹配发件人=%s",
            scanned,
            matching,
        )
        navigate_with_retry(page, link, "Battle.net 邮箱验证链接")
        if not wait_for_verification_success(
            page, float(args.email_verification_timeout)
        ):
            return EmailVerificationResult(
                False,
                "verification_not_confirmed",
                "已打开验证链接，但页面未确认验证成功，需手动检查",
                scanned,
                matching,
            )
        return EmailVerificationResult(True, "verified", "", scanned, matching)
    except Exception as exc:
        LOG.warning("注册邮箱自动验证失败: %s", type(exc).__name__)
        return EmailVerificationResult(
            False,
            "verification_error",
            f"邮箱自动验证发生错误（{type(exc).__name__}），需手动检查",
        )
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.quit()


__all__ = [
    "EmailVerificationResult",
    "direct_battlenet_link",
    "extract_battlenet_link",
    "find_link",
    "get_access_token",
    "login_battle_net",
    "poll_verification_link",
    "verify_registered_email",
]
