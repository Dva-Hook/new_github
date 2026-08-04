from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import v6_email_verifier as target
from v5_email_verifier import EmailVerificationResult
from v6_email_pool import parse_credential_line


def test_extracts_battlenet_email_security_code() -> None:
    message = {
        "body": {
            "content": "<h2>Here’s your security code:</h2><strong>KQDLX7</strong>"
        },
        "bodyPreview": "Here’s your security code: KQDLX7",
    }

    assert target.extract_battlenet_security_code(message) == "KQDLX7"


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

    class Element:
        def __init__(self, name: str) -> None:
            self.name = name

        def click(self, **kwargs) -> None:
            clicked.append(self.name)

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

    before = datetime.now(timezone.utc) - timedelta(seconds=31)
    requested_at = target.request_verification_email(object())

    assert requested_at is not None
    assert requested_at >= before
    assert navigated == [
        (target.EMAIL_DETAILS_URL, "Battle.net 电子邮箱详情页")
    ]
    assert clicked == ["resend"]


def test_confirm_email_verified_reloads_live_email_card(monkeypatch) -> None:
    page = object()
    navigated: list[tuple[str, str]] = []

    monkeypatch.setattr(target, "wait_for_verification_success", lambda *a, **k: True)
    monkeypatch.setattr(
        target,
        "navigate_with_retry",
        lambda page, url, description: navigated.append((url, description)),
    )
    monkeypatch.setattr(
        target,
        "wait_email_verified",
        lambda *args, **kwargs: {
            "verified": True,
            "unverified": False,
            "href": target.EMAIL_DETAILS_URL,
        },
    )

    assert target.confirm_email_verified(page, timeout=20) is True
    assert navigated == [
        (target.EMAIL_DETAILS_URL, "Battle.net 电子邮箱详情确认页")
    ]


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

    monkeypatch.setattr(target, "poll_verification_link", poll)
    monkeypatch.setattr(
        target,
        "navigate_with_retry",
        lambda *args, **kwargs: calls.append("open-link"),
    )
    monkeypatch.setattr(target, "confirm_email_verified", lambda *a, **k: True)
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
