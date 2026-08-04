from __future__ import annotations

from datetime import datetime, timezone
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

