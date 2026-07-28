# -*- coding: utf-8 -*-
"""Post-registration Battle.net email verification for V5 supplied emails."""

from __future__ import annotations

import contextlib
import html
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import requests

from v5_email_pool import EmailCredential
from v5_resource_policy import install_ruyi_tracking_filter


LOG = logging.getLogger("http_register_v5.email_verifier")
LOGIN_URL = "https://account.battle.net/overview"
SENDER = "noreply@battle.net"
TOKEN_ENDPOINTS = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.live.com/oauth20_token.srf",
)
MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DEFAULT_EMAIL_BROWSER_CACHE_DIR = (
    Path(__file__).resolve().parent / ".cache" / "v5_email_browser_profile"
)
PROFILE_MARKER_NAME = ".v5_email_verification_profile"
FIREFOX_PROXY_PREF_LINE = re.compile(
    r"^\s*(?:user_)?pref\(\s*['\"]network\.proxy\.[^'\"]+['\"]\s*,",
    re.IGNORECASE,
)


def clear_cached_proxy_preferences(cache_dir: Path) -> list[str]:
    """Remove persisted Firefox proxy preferences without touching HTTP cache2."""

    cache_dir = cache_dir.expanduser().resolve()
    removed: list[str] = []
    for filename in ("prefs.js", "user.js"):
        path = cache_dir / filename
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        kept: list[str] = []
        removed_count = 0
        for line in original.splitlines(keepends=True):
            if FIREFOX_PROXY_PREF_LINE.match(line):
                removed_count += 1
            else:
                kept.append(line)
        if not removed_count:
            continue
        path.write_text("".join(kept), encoding="utf-8")
        removed.extend(f"{filename}:network.proxy.*" for _ in range(removed_count))
    return removed


def sanitize_cached_profile(
    cache_dir: Path,
    *,
    lock_timeout: float = 15.0,
    poll_interval: float = 0.2,
) -> list[str]:
    """Remove identity and stale proxy state while preserving Firefox HTTP cache2."""

    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    marker = cache_dir / PROFILE_MARKER_NAME
    existing_entries = list(cache_dir.iterdir())
    if (
        existing_entries
        and not marker.is_file()
        and cache_dir != DEFAULT_EMAIL_BROWSER_CACHE_DIR.resolve()
    ):
        raise RuntimeError(
            f"自定义邮箱验证缓存目录已有文件且不是 V5 管理的 profile: {cache_dir}"
        )
    if not marker.is_file():
        marker.write_text(
            "managed V5 email verification profile; keep cache2, clear identity state\n",
            encoding="ascii",
        )

    lock_path = cache_dir / "parent.lock"
    lock_deadline = time.monotonic() + max(0.0, float(lock_timeout))
    while lock_path.exists() or lock_path.is_symlink():
        try:
            lock_path.unlink()
        except PermissionError as exc:
            if time.monotonic() >= lock_deadline:
                raise RuntimeError(
                    f"邮箱验证缓存 profile 仍被浏览器占用: {cache_dir}"
                ) from exc
            time.sleep(max(0.0, float(poll_interval)))

    identity_paths = (
        "cookies.sqlite",
        "cookies.sqlite-shm",
        "cookies.sqlite-wal",
        "webappsstore.sqlite",
        "webappsstore.sqlite-shm",
        "webappsstore.sqlite-wal",
        "storage.sqlite",
        "storage.sqlite-shm",
        "storage.sqlite-wal",
        "formhistory.sqlite",
        "formhistory.sqlite-shm",
        "formhistory.sqlite-wal",
        "credentialstate.sqlite",
        "credentialstate.sqlite-shm",
        "credentialstate.sqlite-wal",
        "logins.json",
        "logins.db",
        "logins.db-shm",
        "logins.db-wal",
        "sessionCheckpoints.json",
        "sessionstore.jsonlz4",
        "sessionstore-backups",
        "sessionstore-logs",
        str(Path("storage") / "default"),
        str(Path("storage") / "temporary"),
        str(Path("storage") / "ls-archive.sqlite"),
    )
    removed: list[str] = []
    for relative_path in identity_paths:
        path = cache_dir / relative_path
        existed = path.exists() or path.is_symlink()
        removal_deadline = time.monotonic() + max(0.0, float(lock_timeout))
        while path.exists() or path.is_symlink():
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except PermissionError as exc:
                if time.monotonic() >= removal_deadline:
                    raise RuntimeError(
                        f"邮箱验证缓存 profile 的身份状态仍被浏览器占用: {path}"
                    ) from exc
                time.sleep(max(0.0, float(poll_interval)))
        if existed:
            removed.append(relative_path)
    removed.extend(clear_cached_proxy_preferences(cache_dir))
    return removed


def launch_cached_ruyi_browser(
    args: Any,
    proxy: Any,
    runtime_proxy_url: Optional[str],
    output_dir: Path,
) -> tuple[Any, Path]:
    """Launch the email verifier with a sanitized, reusable Firefox profile."""

    try:
        import ruyipage
    except ImportError as exc:
        raise RuntimeError(
            "缺少 ruyipage；运行 python -m pip install ruyiPage --upgrade，"
            "然后运行 python -m ruyipage install"
        ) from exc

    cache_dir = Path(
        getattr(args, "email_browser_cache_dir", DEFAULT_EMAIL_BROWSER_CACHE_DIR)
    ).expanduser().resolve()
    removed = sanitize_cached_profile(cache_dir)
    LOG.info(
        "邮箱验证浏览器 profile 已就绪: cache=%s sanitizedEntries=%d cache2=%s",
        cache_dir,
        len(removed),
        (cache_dir / "cache2").exists(),
    )

    snapshot_dir = output_dir / "email_verification_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    launch_proxy = runtime_proxy_url if runtime_proxy_url is not None else proxy.url
    try:
        page = ruyipage.launch(
            headless=bool(args.headless),
            private=False,
            user_dir=str(cache_dir),
            proxy=launch_proxy,
            window_size=(1920, 1080),
            timeout_page_load=60,
            timeout_script=60,
            close_on_exit=True,
            failure_snapshot=True,
            snapshot_dir=str(snapshot_dir),
        )
    except Exception:
        with contextlib.suppress(Exception):
            sanitize_cached_profile(cache_dir)
        raise
    with contextlib.suppress(Exception):
        page.close_other_tabs()
    with contextlib.suppress(Exception):
        page.set_bypass_csp(True)
    if not bool(getattr(args, "no_resource_blocking", False)):
        install_ruyi_tracking_filter(page, LOG)
    return page, cache_dir


def close_cached_ruyi_browser(page: Any, cache_dir: Path) -> None:
    """Close Firefox, then strip identity databases before persisting its cache."""

    try:
        try:
            page.quit(timeout=10, force=True)
        except TypeError:
            page.quit()
    finally:
        removed = sanitize_cached_profile(cache_dir)
        LOG.info(
            "邮箱验证浏览器已关闭并清理身份状态: removed=%d cache2=%s",
            len(removed),
            (cache_dir / "cache2").exists(),
        )


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


CLEAR_ACCOUNT_SESSION_JS = r"""return (async () => {
  const result = {
    ok: true,
    localCleared: false,
    sessionCleared: false,
    indexedDbCleared: false,
    indexedDbDeleted: []
  };
  try {
    localStorage.clear();
    result.localCleared = true;
  } catch (error) {
    result.ok = false;
    result.localError = String(error);
  }
  try {
    sessionStorage.clear();
    result.sessionCleared = true;
  } catch (error) {
    result.ok = false;
    result.sessionError = String(error);
  }
  try {
    if (indexedDB && indexedDB.databases) {
      const databases = await indexedDB.databases();
      await Promise.all(databases.map((database) => new Promise((resolve) => {
        if (!database.name) return resolve();
        const request = indexedDB.deleteDatabase(database.name);
        request.onsuccess = request.onerror = request.onblocked = () => resolve();
        result.indexedDbDeleted.push(database.name);
      })));
    }
    result.indexedDbCleared = true;
  } catch (error) {
    result.ok = false;
    result.indexedDbError = String(error);
  }
  return result;
})();"""


LOGIN_STATE_JS = r"""return (() => {
  const isVisible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  };
  const anyVisible = (selector, root = document) =>
    Array.from(root.querySelectorAll(selector)).some(isVisible);
  const accountInput = anyVisible('#accountName');
  const passwordInput = anyVisible('#password');
  const loginForm = accountInput || passwordInput;
  const successMarker = anyVisible(
    '#code-claim, a[href*="logout"], #account-settings, .account-overview'
  );
  const protectedShell = anyVisible(
    'a[href*="/overview"], a[href*="/details"]'
  );
  const pageText = String(document.body?.innerText || '').toLowerCase();
  const codeChallenge = anyVisible([
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
  const dynamicChallenge = Array.from(document.querySelectorAll('form')).some(
    (form) => anyVisible('.challenge-body input, .challenge-body select', form)
      && anyVisible('button[type="submit"], input[type="submit"], button:not([type])', form)
  );
  const securityChallenge = codeChallenge || dynamicChallenge
    || (!loginForm && !protectedShell && methodChallenge);
  const emailWords = /e-mail|email|mailbox|邮箱|邮件|verification code|security code/.test(pageText);
  const errorNodes = Array.from(document.querySelectorAll(
    'form .error-message, form .field-validation-error, form .alert-danger, '
    + 'form .alert-error, form [role="alert"]'
  )).filter(isVisible);
  const loginErrorText = errorNodes
    .map((item) => String(item.innerText || item.textContent || '').trim())
    .filter(Boolean).join(' ').slice(0, 240);
  const href = String(location.href || '');
  return {
    href,
    account_input_present: accountInput,
    password_input_present: passwordInput,
    login_form_present: loginForm,
    success_marker_present: successMarker,
    protected_shell_present: protectedShell,
    email_code_challenge_present: securityChallenge && emailWords,
    security_challenge_present: securityChallenge,
    overview_url: /account\.battle\.net\/overview(?:[/?#]|$)/i.test(href),
    login_error_text: loginErrorText,
    success: !securityChallenge && (successMarker || protectedShell || (
      /account\.battle\.net\/overview(?:[/?#]|$)/i.test(href)
      && !loginForm
    ))
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


def delete_account_cookies(page: Any) -> None:
    with contextlib.suppress(Exception):
        page.delete_cookies()
    for domain in (
        "battle.net",
        ".battle.net",
        "account.battle.net",
        ".account.battle.net",
        "us.account.battle.net",
        ".us.account.battle.net",
        "eu.account.battle.net",
        ".eu.account.battle.net",
        "kr.account.battle.net",
        ".kr.account.battle.net",
    ):
        with contextlib.suppress(Exception):
            page.delete_cookies(domain=domain)


def clear_account_session_storage(
    page: Any,
    description: str,
    *,
    warn_on_failure: bool = True,
) -> None:
    try:
        result = page.run_js(CLEAR_ACCOUNT_SESSION_JS, timeout=10)
        if warn_on_failure and (
            not isinstance(result, dict) or not result.get("ok")
        ):
            LOG.warning("%s Web Storage/IndexedDB 未完全清理: %s", description, result)
    except Exception as exc:
        if warn_on_failure:
            LOG.warning("清理 %s Web Storage/IndexedDB 失败: %s", description, exc)


def prepare_clean_login_page(page: Any) -> None:
    with contextlib.suppress(Exception):
        page.close_other_tabs()
    delete_account_cookies(page)
    clear_account_session_storage(page, "当前页", warn_on_failure=False)
    navigate_with_retry(page, LOGIN_URL, "Battle.net 登录页")
    delete_account_cookies(page)
    clear_account_session_storage(page, "登录域")
    navigate_with_retry(page, LOGIN_URL, "Battle.net 登录页")


def _read_login_state(page: Any) -> dict[str, Any]:
    state = page.run_js(LOGIN_STATE_JS, timeout=5)
    return dict(state) if isinstance(state, dict) else {}


def is_login_success_state(state: dict[str, Any]) -> bool:
    if state.get("security_challenge_present"):
        return False
    return bool(
        state.get("protected_shell_present")
        or state.get("success_marker_present")
        or (state.get("overview_url") and not state.get("login_form_present"))
        or state.get("success")
    )


def _classify_login_state(
    state: dict[str, Any],
    *,
    accept_password: bool,
) -> Optional[str]:
    if state.get("email_code_challenge_present"):
        return "manual_email_code"
    if state.get("security_challenge_present"):
        return "manual_security_check"
    if is_login_success_state(state):
        return "success"
    if state.get("login_error_text"):
        return "login_error"
    if accept_password and state.get("password_input_present"):
        return "password"
    return None


def _wait_login_phase(
    page: Any,
    timeout: float,
    *,
    accept_password: bool,
    poll_interval: float = 0.5,
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.1, float(timeout))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            last = _read_login_state(page)
            phase = _classify_login_state(
                last,
                accept_password=accept_password,
            )
            if phase:
                return phase, last
        if poll_interval > 0:
            time.sleep(poll_interval)
    return "timeout", last


def _manual_login_result(phase: str) -> EmailVerificationResult:
    if phase == "manual_email_code":
        return EmailVerificationResult(
            False,
            "manual_email_code",
            "该账号登录时弹出邮箱验证码，需手动验证",
        )
    return EmailVerificationResult(
        False,
        "manual_security_check",
        "该账号登录时弹出安全验证，需手动验证",
    )


def login_battle_net(
    page: Any,
    email: str,
    password: str,
    *,
    timeout: float,
) -> EmailVerificationResult:
    LOG.info("打开 Battle.net 登录页: %s", email)
    prepare_clean_login_page(page)
    close_cookie_banner(page)
    account_input = wait_element(page, "#accountName", min(30.0, timeout))
    account_input.input(email, clear=True)
    click_element(page, "#submit")
    phase, state = _wait_login_phase(
        page,
        min(30.0, timeout),
        accept_password=True,
    )
    if phase in {"manual_email_code", "manual_security_check"}:
        return _manual_login_result(phase)
    if phase == "success":
        return EmailVerificationResult(True, "logged_in")
    if phase == "login_error":
        return EmailVerificationResult(
            False,
            "login_rejected",
            f"Battle.net 登录失败：{state.get('login_error_text')}",
        )
    if phase != "password":
        return EmailVerificationResult(False, "login_failed", "登录页未进入密码步骤")

    password_input = wait_element(page, "#password", min(30.0, timeout))
    password_input.input(password, clear=True)
    click_element(page, "#submit")
    LOG.info("已提交密码，等待 Battle.net 登录完成")
    phase, state = _wait_login_phase(
        page,
        timeout,
        accept_password=False,
    )
    if phase == "success":
        LOG.info("Battle.net 登录成功: %s", email)
        return EmailVerificationResult(True, "logged_in")
    if phase in {"manual_email_code", "manual_security_check"}:
        return _manual_login_result(phase)
    if phase == "login_error":
        return EmailVerificationResult(
            False,
            "login_rejected",
            f"Battle.net 登录失败：{state.get('login_error_text')}",
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
    page: Any = None
    cache_dir: Optional[Path] = None
    try:
        LOG.info("启动 RuyiPage 执行注册邮箱验证: %s", credential.email)
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
                close_cached_ruyi_browser(page, cache_dir or DEFAULT_EMAIL_BROWSER_CACHE_DIR)
        elif cache_dir is not None:
            with contextlib.suppress(Exception):
                sanitize_cached_profile(cache_dir)


__all__ = [
    "EmailVerificationResult",
    "clear_cached_proxy_preferences",
    "close_cached_ruyi_browser",
    "direct_battlenet_link",
    "extract_battlenet_link",
    "find_link",
    "get_access_token",
    "launch_cached_ruyi_browser",
    "login_battle_net",
    "poll_verification_link",
    "sanitize_cached_profile",
    "verify_registered_email",
]
