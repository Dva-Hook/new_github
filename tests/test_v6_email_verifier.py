from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import v6_email_verifier as target
import v5_email_verifier as v5
from v5_email_verifier import EmailVerificationResult
from v6_email_pool import parse_credential_line


def test_access_token_refresh_reuses_granted_scopes_when_scope_is_omitted() -> None:
    calls: list[dict[str, str]] = []

    class Response:
        status_code = 200
        reason = "OK"
        ok = True

        def __init__(self, data: dict[str, str]) -> None:
            self.data = data

        def json(self) -> dict[str, str]:
            if "scope" in self.data:
                return {"error": "invalid_scope"}
            return {
                "access_token": "access-token",
                "refresh_token": "rotated-refresh-token",
            }

    class Session:
        def post(self, endpoint, *, data, **kwargs):
            calls.append(dict(data))
            return Response(dict(data))

    access_token, refresh_token = v5.get_access_token(
        Session(), "client-id", "refresh-token"
    )

    assert (access_token, refresh_token) == (
        "access-token",
        "rotated-refresh-token",
    )
    assert calls == [
        {
            "client_id": "client-id",
            "grant_type": "refresh_token",
            "refresh_token": "refresh-token",
            "scope": "https://graph.microsoft.com/.default offline_access",
        },
        {
            "client_id": "client-id",
            "grant_type": "refresh_token",
            "refresh_token": "refresh-token",
        },
    ]


def test_access_token_refresh_reports_oauth_error_without_echoing_credentials() -> None:
    class Response:
        status_code = 400
        reason = "Bad Request"
        ok = False

        def json(self) -> dict[str, str]:
            return {
                "error": "invalid_grant",
                "error_description": "credential rejected for test",
            }

    class Session:
        def post(self, endpoint, *, data, **kwargs):
            return Response()

    with pytest.raises(RuntimeError) as error:
        v5.get_access_token(Session(), "client-id", "refresh-token")

    message = str(error.value)
    assert "HTTP 400 invalid_grant" in message
    assert "credential rejected for test" in message
    assert "refresh-token" not in message


def test_shared_graph_token_reader_accepts_bounded_request_timeout() -> None:
    calls: list[object] = []

    class Response:
        status_code = 200
        reason = "OK"

        def json(self):
            return {"access_token": "access-token"}

    class Session:
        def post(self, endpoint, *, data, **kwargs):
            calls.append(kwargs["timeout"])
            return Response()

    result = v5.get_access_token(
        Session(),
        "client-id",
        "refresh-token",
        request_timeout=2.5,
    )

    assert result == ("access-token", "refresh-token")
    assert calls == [2.5]


def test_shared_graph_link_reader_accepts_deadline_and_request_timeout() -> None:
    calls: list[object] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"value": []}

    class Session:
        def get(self, url, **kwargs):
            calls.append(kwargs["timeout"])
            return Response()

    result = v5.find_link(
        Session(),
        "access-token",
        not_before=datetime.now(timezone.utc),
        deadline=target.time.monotonic() + 5.0,
        request_timeout=2.5,
    )

    assert result == (None, 0, 0)
    assert calls == [2.5]


def test_oauth2_fallback_uses_common_endpoint_without_scope() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        def json(self):
            return {
                "access_token": "oauth2-access-token",
                "refresh_token": "rotated-refresh-token",
            }

    class Session:
        def post(self, endpoint, *, data, **kwargs):
            calls.append({"endpoint": endpoint, "data": dict(data), "kwargs": kwargs})
            return Response()

    result = target.get_access_token_oauth2(
        Session(), "client-id", "refresh-token"
    )

    assert result == ("oauth2-access-token", "rotated-refresh-token")
    assert calls == [
        {
            "endpoint": target.OAUTH2_COMMON_TOKEN_ENDPOINT,
            "data": {
                "client_id": "client-id",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token",
            },
            "kwargs": {"headers": {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }, "timeout": 20},
        }
    ]


def test_extracts_battlenet_email_security_code() -> None:
    message = {
        "body": {
            "content": "<h2>Here’s your security code:</h2><strong>KQDLX7</strong>"
        },
        "bodyPreview": "Here’s your security code: KQDLX7",
    }

    assert target.extract_battlenet_security_code(message) == "KQDLX7"


def test_extracts_security_code_when_html_splits_the_code_into_nodes() -> None:
    message = {
        "body": {
            "content": (
                "<p>Here is your security code:</p>"
                "<strong>KQ</strong><strong>DLX7</strong>"
            )
        },
        "bodyPreview": "Here is your security code: KQ DLX7",
    }

    assert target.extract_battlenet_security_code(message) == "KQDLX7"


def test_find_security_code_falls_back_to_inbox_messages() -> None:
    received_at = datetime.now(timezone.utc)
    message = {
        "from": {
            "emailAddress": {
                "name": "Battle.net",
                "address": "noreply@battle.net",
            }
        },
        "receivedDateTime": received_at.isoformat().replace("+00:00", "Z"),
        "body": {
            "content": "<p>Here is your security code:</p><strong>KQDLX7</strong>"
        },
        "bodyPreview": "Here is your security code: KQDLX7",
    }

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            if url == target.INBOX_MESSAGES_URL:
                return Response({"value": [message]})
            return Response({"value": []})

    session = Session()
    result = target.find_security_code(
        session,
        "access-token",
        not_before=received_at - timedelta(seconds=1),
    )

    assert result == ("KQDLX7", 1, 1)
    assert session.urls == [target.INBOX_MESSAGES_URL]


def test_find_security_code_fetches_body_only_for_matching_preview() -> None:
    received_at = datetime.now(timezone.utc)
    message = {
        "id": "message-id/1",
        "from": {
            "emailAddress": {
                "name": "Battle.net",
                "address": "noreply@battle.net",
            }
        },
        "receivedDateTime": received_at.isoformat().replace("+00:00", "Z"),
        "bodyPreview": "Your message does not include the code text",
    }
    detail = {"body": {"content": "Your security code: KQDLX7"}}

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == target.INBOX_MESSAGES_URL:
                return Response({"value": [message]})
            return Response(detail)

    session = Session()
    result = target.find_security_code(
        session,
        "access-token",
        not_before=received_at - timedelta(seconds=1),
    )

    assert result == ("KQDLX7", 1, 1)
    assert len(session.calls) == 2
    list_params = session.calls[0][1]["params"]
    assert "body" not in list_params["$select"].split(",")
    assert session.calls[1][0].endswith("/message-id%2F1")


def test_poll_security_code_retries_transient_graph_errors(monkeypatch) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    calls = []

    monkeypatch.setattr(
        target,
        "get_access_token",
        lambda *args, **kwargs: ("access-token", "refresh-token"),
    )

    def find(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise target.requests.Timeout("temporary graph timeout")
        return "KQDLX7", 1, 1

    monkeypatch.setattr(target, "find_security_code", find)
    monkeypatch.setattr(target.time, "sleep", lambda seconds: None)

    result = target.poll_security_code(
        credential,
        not_before=datetime.now(timezone.utc),
        timeout=2.0,
        interval=0.01,
    )

    assert result == ("KQDLX7", 1, 1)
    assert len(calls) == 2


def test_poll_security_code_switches_to_common_oauth2_after_primary_phase(
    monkeypatch,
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    clock = [0.0]
    tokens = []

    monkeypatch.setattr(target, "get_access_token", lambda *args, **kwargs: ("primary", "r1"))

    def oauth2(*args, **kwargs):
        tokens.append("oauth2")
        return "oauth2", "r2"

    monkeypatch.setattr(target, "get_access_token_oauth2", oauth2)

    def find(session, access_token, **kwargs):
        if access_token == "primary":
            clock[0] = 36.0
            return None, 1, 0
        return "KQDLX7", 1, 1

    monkeypatch.setattr(target, "find_security_code", find)
    monkeypatch.setattr(target.time, "monotonic", lambda: clock[0])

    result = target.poll_security_code(
        credential,
        not_before=datetime.now(timezone.utc),
        timeout=60.0,
        interval=0.01,
    )

    assert result == ("KQDLX7", 1, 1)
    assert tokens == ["oauth2"]


def test_poll_security_code_uses_o2_after_primary_token_failure(monkeypatch) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()

    monkeypatch.setattr(
        target,
        "get_access_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("primary token rejected")
        ),
    )
    calls: list[object] = []

    def o2_fallback(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return "KQDLX7", 2, 1

    monkeypatch.setattr(target, "poll_security_code_o2", o2_fallback)

    result = target.poll_security_code(
        credential,
        not_before=datetime.now(timezone.utc),
        timeout=20.0,
    )

    assert result == ("KQDLX7", 2, 1)
    assert calls == [20.0]


def test_v5_link_reader_uses_o2_after_graph_timeout(monkeypatch) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    monkeypatch.setattr(
        v5,
        "poll_verification_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TimeoutError("primary mail timeout")
        ),
    )
    calls: list[object] = []

    def o2_fallback(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return "https://account.battle.net/verify?ticket=test", 4, 1

    monkeypatch.setattr(v5, "poll_verification_link_o2", o2_fallback)
    result = v5.poll_verification_link_with_o2_fallback(
        credential,
        not_before=datetime.now(timezone.utc),
        timeout=20.0,
    )

    assert result == ("https://account.battle.net/verify?ticket=test", 4, 1)
    assert calls == [20.0]


def test_shared_graph_link_poll_limits_token_refreshes(monkeypatch) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    token_calls: list[object] = []

    def refresh(*args, **kwargs):
        token_calls.append(kwargs)
        if len(token_calls) > 2:
            raise AssertionError("shared Graph token refresh loop was not bounded")
        return "access-token", "refresh-token"

    monkeypatch.setattr(v5, "get_access_token", refresh)
    monkeypatch.setattr(
        v5,
        "find_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            v5.AccessTokenExpired("expired")
        ),
    )

    with pytest.raises(TimeoutError, match="Graph"):
        v5.poll_verification_link(
            credential,
            not_before=datetime.now(timezone.utc),
            timeout=10.0,
        )

    assert len(token_calls) == 2


def test_email_verified_state_recognizes_overview_text() -> None:
    class Page:
        def run_js(self, script, timeout=0):
            return {
                "href": "https://account.battle.net/overview",
                "text": "Security Checkup\nSecurity\nCOMPLETE\nEmail Verified",
                "verified": True,
            }

    state = target.read_email_verified_state(Page())

    assert state["verified"] is True


def test_overview_verification_uses_semantic_security_card_signals() -> None:
    script = target.OVERVIEW_EMAIL_VERIFIED_STATE_JS

    assert "account-overview-security" in script
    assert ".security-option" in script
    assert "email\\s+verified" in script
    assert "check-circle" in script
    assert "nth-child" not in script


def test_email_verification_browser_forces_english_locale() -> None:
    calls: list[object] = []

    class Page:
        def set_locale(self, locales) -> None:
            calls.append(("set-locale", locales))

        def run_js(self, script, timeout=0):
            calls.append(("run-js", script, timeout))
            return {"language": "en-US", "languages": ["en-US", "en"]}

    state = v5.configure_email_browser_english(Page())

    assert state == {"language": "en-US", "languages": ["en-US", "en"]}
    assert calls[0] == ("set-locale", ["en-US", "en"])
    assert calls[1][0] == "run-js"


def test_email_verification_browser_rejects_non_english_locale() -> None:
    class Page:
        def set_locale(self, locales) -> None:
            pass

        def run_js(self, script, timeout=0):
            return {"language": "zh-CN", "languages": ["zh-CN", "zh"]}

    with pytest.raises(RuntimeError, match="英文区域设置校验失败"):
        v5.configure_email_browser_english(Page())


def test_wait_element_retries_until_slow_login_field_appears(monkeypatch) -> None:
    clock = [0.0]
    expected = object()

    class Page:
        def __init__(self) -> None:
            self.calls = 0

        def get_all_frames(self):
            return []

        def ele(self, selector, timeout=0):
            assert selector == "#accountName"
            self.calls += 1
            return expected if self.calls == 3 else None

    page = Page()
    monkeypatch.setattr(v5.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        v5.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert v5.wait_element(page, "#accountName", timeout=2.0) is expected
    assert page.calls == 3


def test_verifier_stops_when_overview_already_says_email_verified(
    monkeypatch, tmp_path: Path
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    page = object()
    cache_dir = tmp_path / "profile"
    closed = []

    monkeypatch.setattr(
        target,
        "launch_cached_ruyi_browser",
        lambda *args, **kwargs: (page, cache_dir),
    )
    monkeypatch.setattr(
        target,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target,
        "wait_email_verified",
        lambda *args, **kwargs: {
            "verified": True,
            "href": "https://account.battle.net/overview",
            "text": "Email Verified",
        },
    )
    monkeypatch.setattr(
        target,
        "poll_verification_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("already-verified account must not poll for a link")
        ),
    )
    monkeypatch.setattr(
        target,
        "close_cached_ruyi_browser",
        lambda *args, **kwargs: closed.append(True),
    )

    result = target.verify_registered_email(
        credential,
        "battle-password",
        args=SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        proxy=SimpleNamespace(),
        runtime_proxy_url=None,
        output_dir=tmp_path,
        not_before=datetime.now(timezone.utc),
    )

    assert result.ok is True
    assert result.status == "already_verified"
    assert closed == [True]


def test_request_verification_email_uses_overview_and_email_card_selectors(
    monkeypatch,
) -> None:
    clicked: list[str] = []
    navigated: list[tuple[str, str]] = []
    sleeps: list[float] = []
    events: list[str] = []

    class Page:
        def run_js(self, script, timeout=0):
            assert script == target.EMAIL_RESEND_API_JS
            events.append("api")
            return {"ok": True, "status": 200}

    class Element:
        def __init__(self, name: str) -> None:
            self.name = name

        def click(self, **kwargs) -> None:
            clicked.append(self.name)
            events.append("dom-click")

    elements = {
        target.OVERVIEW_VERIFICATION_BANNER_SELECTOR: Element("overview-banner"),
        target.EMAIL_DETAILS_RESEND_SELECTOR: Element("resend"),
    }
    monkeypatch.setattr(
        target,
        "wait_element",
        lambda page, selector, timeout: elements.get(selector)
        or (_ for _ in ()).throw(TimeoutError(selector)),
    )
    monkeypatch.setattr(
        target,
        "navigate_with_retry",
        lambda page, url, description: navigated.append((url, description)),
    )
    monkeypatch.setattr(target.time, "sleep", lambda seconds: sleeps.append(seconds))

    before = datetime.now(timezone.utc) - timedelta(seconds=31)
    requested_at = target.request_verification_email(Page())

    assert requested_at is not None
    assert requested_at >= before
    assert navigated == [
        (target.EMAIL_DETAILS_URL, "Battle.net 电子邮箱详情页")
    ]
    assert clicked == ["resend"]
    assert events == ["api", "dom-click"]
    assert sleeps == [10.0]


def test_request_verification_email_uses_semantic_dom_fallback_when_selector_changed(
    monkeypatch,
) -> None:
    navigated: list[tuple[str, str]] = []
    scripts: list[str] = []
    sleeps: list[float] = []

    class Page:
        def run_js(self, script, timeout=0):
            scripts.append(script)
            if script == target.EMAIL_RESEND_API_JS:
                return {
                    "ok": False,
                    "status": 403,
                    "error": "rejected-for-test",
                }
            return {
                "clicked": True,
                "candidates": 2,
                "method": "semantic-text",
            }

    monkeypatch.setattr(
        target,
        "wait_element",
        lambda page, selector, timeout: (
            object()
            if selector == target.OVERVIEW_VERIFICATION_BANNER_SELECTOR
            else (_ for _ in ()).throw(TimeoutError(selector))
        ),
    )
    monkeypatch.setattr(
        target,
        "navigate_with_retry",
        lambda page, url, description: navigated.append((url, description)),
    )
    monkeypatch.setattr(target.time, "sleep", lambda seconds: sleeps.append(seconds))

    requested_at = target.request_verification_email(Page())

    assert requested_at is not None
    assert navigated == [
        (target.EMAIL_DETAILS_URL, "Battle.net 电子邮箱详情页")
    ]
    assert scripts == [
        target.EMAIL_RESEND_API_JS,
        target.EMAIL_DETAILS_RESEND_DOM_JS,
    ]
    assert sleeps == [10.0]


def test_open_verification_link_waits_for_document_complete(monkeypatch) -> None:
    events: list[object] = []

    class OverviewTab:
        def close(self) -> None:
            events.append("overview-close")

    class Page:
        def new_tab(self, url, background=False):
            events.append(("new-tab", url, background))
            return OverviewTab()

        def activate(self) -> None:
            events.append("activate-original")

    page = Page()
    navigated: list[tuple[str, str, float]] = []
    waited: list[float] = []

    monkeypatch.setattr(
        target,
        "navigate_with_retry",
        lambda page, url, description, timeout: navigated.append(
            (url, description, timeout)
        ),
    )
    monkeypatch.setattr(
        target,
        "wait_document_complete",
        lambda page, timeout: waited.append(timeout) or True,
    )
    monkeypatch.setattr(
        target,
        "wait_overview_email_verified",
        lambda page, timeout: {
            "verified": True,
            "source": "security-card-text",
        },
    )

    link = "https://account.battle.net/overview?ticket=fresh"
    assert target.open_verification_link(page, link, timeout=20) is True
    assert navigated == [
        (link, "Battle.net 邮箱验证链接", 20.0),
        (target.OVERVIEW_URL, "Battle.net 账号概览页验证状态", 20.0),
    ]
    assert waited == [20, 20]
    assert events == [
        ("new-tab", "about:blank", False),
        "overview-close",
        "activate-original",
    ]


def test_open_verification_link_requires_overview_email_verified(monkeypatch) -> None:
    class OverviewTab:
        def close(self) -> None:
            pass

    class Page:
        def new_tab(self, url, background=False):
            return OverviewTab()

        def activate(self) -> None:
            pass

    monkeypatch.setattr(target, "navigate_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr(target, "wait_document_complete", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        target,
        "wait_overview_email_verified",
        lambda *args, **kwargs: {
            "verified": False,
            "unverified": True,
            "source": "security-card-text",
        },
    )

    assert target.open_verification_link(Page(), "https://example.test/link", 20) is False


def test_verifier_requests_fresh_mail_before_polling(
    monkeypatch, tmp_path: Path
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    page = object()
    cache_dir = tmp_path / "profile"
    registration_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    requested_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    calls: list[str] = []
    poll_thresholds: list[datetime] = []

    monkeypatch.setattr(
        target,
        "launch_cached_ruyi_browser",
        lambda *args, **kwargs: (page, cache_dir),
    )
    monkeypatch.setattr(
        target,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target,
        "wait_email_verified",
        lambda *args, **kwargs: {
            "verified": False,
            "unverified": True,
            "href": "https://account.battle.net/overview",
        },
    )
    monkeypatch.setattr(
        target,
        "request_verification_email",
        lambda *args, **kwargs: calls.append("resend") or requested_at,
    )

    def poll(*args, **kwargs):
        calls.append("poll")
        poll_thresholds.append(kwargs["not_before"])
        return "https://account.battle.net/overview?ticket=fresh", 4, 1

    monkeypatch.setattr(target, "poll_verification_link_attempts", poll)
    monkeypatch.setattr(
        target,
        "open_verification_link",
        lambda *args, **kwargs: calls.append("open-link") or True,
    )
    monkeypatch.setattr(target, "close_cached_ruyi_browser", lambda *a, **k: None)

    result = target.verify_registered_email(
        credential,
        "battle-password",
        args=SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        proxy=SimpleNamespace(),
        runtime_proxy_url=None,
        output_dir=tmp_path,
        not_before=registration_at,
    )

    assert result.ok is True
    assert result.status == "verified"
    assert calls == ["resend", "poll", "open-link"]
    assert poll_thresholds == [requested_at]


def test_poll_verification_link_attempts_reads_exactly_three_times(
    monkeypatch,
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    reads: list[datetime] = []
    sleeps: list[float] = []
    threshold = datetime.now(timezone.utc) - timedelta(minutes=1)

    monkeypatch.setattr(
        target,
        "get_access_token",
        lambda *args, **kwargs: ("access-token", "refresh-token"),
    )

    def find(*args, **kwargs):
        reads.append(kwargs["not_before"])
        return None, len(reads), 0

    monkeypatch.setattr(target, "find_link", find)
    monkeypatch.setattr(target.time, "sleep", lambda seconds: sleeps.append(seconds))

    link, scanned, matching = target.poll_verification_link_attempts(
        credential,
        not_before=threshold,
        attempts=3,
        interval=5.0,
    )

    assert link is None
    assert scanned == 3
    assert matching == 0
    assert reads == [threshold, threshold, threshold]
    assert sleeps == [5.0, 5.0]


def test_poll_verification_link_attempts_limits_token_refreshes(
    monkeypatch,
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    token_calls: list[dict[str, object]] = []

    def refresh(*args, **kwargs):
        token_calls.append(kwargs)
        if len(token_calls) > 2:
            raise AssertionError("Graph token refresh loop was not bounded")
        return "access-token", "refresh-token"

    monkeypatch.setattr(target, "get_access_token", refresh)
    monkeypatch.setattr(
        target,
        "find_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            target.AccessTokenExpired("expired")
        ),
    )

    with pytest.raises(TimeoutError, match="Graph"):
        target.poll_verification_link_attempts(
            credential,
            not_before=datetime.now(timezone.utc),
            attempts=3,
            interval=5.0,
            timeout=10.0,
        )

    assert len(token_calls) == 2


def test_poll_verification_link_attempts_passes_deadline_to_graph_reader(
    monkeypatch,
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        target,
        "get_access_token",
        lambda *args, **kwargs: ("access-token", "refresh-token"),
    )

    def find(*args, **kwargs):
        calls.append(kwargs)
        return None, 0, 0

    monkeypatch.setattr(target, "find_link", find)
    monkeypatch.setattr(target.time, "sleep", lambda seconds: None)

    result = target.poll_verification_link_attempts(
        credential,
        not_before=datetime.now(timezone.utc),
        attempts=1,
        interval=5.0,
        timeout=10.0,
    )

    assert result == (None, 0, 0)
    assert len(calls) == 1
    assert isinstance(calls[0]["deadline"], float)
    assert calls[0]["request_timeout"] == target.GRAPH_LINK_REQUEST_TIMEOUT


def test_verifier_clicks_resend_again_after_three_empty_reads(
    monkeypatch, tmp_path: Path
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    page = object()
    first_requested_at = datetime.now(timezone.utc) - timedelta(seconds=20)
    second_requested_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    request_times = iter((first_requested_at, second_requested_at))
    calls: list[str] = []
    request_options: list[bool] = []

    monkeypatch.setattr(
        target,
        "launch_cached_ruyi_browser",
        lambda *args, **kwargs: (page, tmp_path / "profile"),
    )
    monkeypatch.setattr(
        target,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target,
        "wait_email_verified",
        lambda *args, **kwargs: {"verified": False, "unverified": True},
    )

    def request(*args, **kwargs):
        calls.append("resend")
        request_options.append(kwargs.get("check_overview_banner", True))
        return next(request_times)

    monkeypatch.setattr(target, "request_verification_email", request)
    monkeypatch.setattr(
        target,
        "poll_verification_link_attempts",
        lambda *args, **kwargs: calls.append("poll-3") or (None, 3, 0),
    )
    monkeypatch.setattr(
        target,
        "poll_verification_link",
        lambda *args, **kwargs: calls.append("poll-after-resend")
        or ("https://account.battle.net/overview?ticket=fresh", 4, 1),
    )
    monkeypatch.setattr(
        target,
        "open_verification_link",
        lambda *args, **kwargs: calls.append("open-link") or True,
    )
    monkeypatch.setattr(target, "close_cached_ruyi_browser", lambda *a, **k: None)

    result = target.verify_registered_email(
        credential,
        "battle-password",
        args=SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        proxy=SimpleNamespace(),
        runtime_proxy_url=None,
        output_dir=tmp_path,
        not_before=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert result.ok is True
    assert result.status == "verified"
    assert calls == ["resend", "poll-3", "resend", "poll-after-resend", "open-link"]
    assert request_options == [True, False]


def test_verifier_does_not_poll_when_resend_cannot_be_triggered(
    monkeypatch, tmp_path: Path
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    page = object()

    monkeypatch.setattr(
        target,
        "launch_cached_ruyi_browser",
        lambda *args, **kwargs: (page, tmp_path / "profile"),
    )
    monkeypatch.setattr(
        target,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target,
        "wait_email_verified",
        lambda *args, **kwargs: {"verified": False, "unverified": True},
    )
    monkeypatch.setattr(
        target,
        "request_verification_email",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("verification-resend-link-not-found")
        ),
    )
    monkeypatch.setattr(
        target,
        "poll_verification_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mail must not be polled before resend")
        ),
    )
    monkeypatch.setattr(target, "close_cached_ruyi_browser", lambda *a, **k: None)

    result = target.verify_registered_email(
        credential,
        "battle-password",
        args=SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        proxy=SimpleNamespace(),
        runtime_proxy_url=None,
        output_dir=tmp_path,
        not_before=datetime.now(timezone.utc),
    )

    assert result.ok is False
    assert result.status == "verification_mail_request_failed"


def test_login_state_classifies_battlenet_email_challenge_without_waiting() -> None:
    state = {
        "email_security_stage": "choice",
        "email_code_challenge_present": True,
        "security_challenge_present": True,
    }

    assert (
        v5._classify_login_state(state, accept_password=False)
        == "manual_email_code"
    )
    assert "email_security_stage" in v5.LOGIN_STATE_JS


def test_security_choice_follows_phone_bind_flow_without_immediate_stage_recheck(
    monkeypatch,
) -> None:
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    ).to_v5()
    calls: list[object] = []

    class Submit:
        def click(self, **kwargs) -> None:
            calls.append(("click", kwargs))

    class Page:
        def ele(self, selector, timeout=0):
            assert selector == "#submit"
            return Submit()

    def detect(*args, **kwargs):
        calls.append("detect")
        if calls.count("detect") > 1:
            raise AssertionError("must not check the stale choice DOM after clicking")
        return "choice"

    monkeypatch.setattr(target, "detect_email_security_stage", detect)
    monkeypatch.setattr(
        target,
        "poll_security_code",
        lambda *args, **kwargs: calls.append("poll") or ("A1B2C3", 4, 1),
    )
    monkeypatch.setattr(
        target,
        "fill_email_security_code",
        lambda page, code: calls.append(("fill", code)),
    )
    monkeypatch.setattr(
        target,
        "read_email_verified_state",
        lambda page: {"verified": True, "href": "https://account.battle.net/overview"},
    )
    monkeypatch.setattr(target.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    completed, scanned, matching = target.complete_email_security_challenge(
        Page(),
        credential,
        timeout=120,
        initial_wait=10,
    )

    assert completed is True
    assert (scanned, matching) == (4, 1)
    assert calls == [
        "detect",
        ("click", {}),
        ("sleep", 10),
        "poll",
        ("fill", "A1B2C3"),
    ]


def test_security_code_fill_falls_back_to_six_dom_boxes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Page:
        def run_js(self, script, selector, character, timeout=0):
            calls.append((selector, character))
            return {"ok": True, "value": character}

    monkeypatch.setattr(
        target,
        "wait_element",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("missing")),
    )
    monkeypatch.setattr(target.time, "sleep", lambda seconds: None)

    target.fill_email_security_code(Page(), "A1B2C3")

    assert [character for _, character in calls] == list("A1B2C3")
    assert [selector for selector, _ in calls] == list(target.EMAIL_CODE_BOX_SELECTORS)
