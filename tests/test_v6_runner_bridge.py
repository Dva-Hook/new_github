from __future__ import annotations

from types import SimpleNamespace

import pytest

import register_ruyipage_v6 as v6
from v5_email_pool import EmailCredential as V5EmailCredential
from v6_email_pool import parse_credential_line


def test_verification_bridge_converts_v6_order_to_v5_credential(monkeypatch) -> None:
    captured = {}

    def fake_verify(credential, account_password, **kwargs):
        captured["credential"] = credential
        captured["account_password"] = account_password
        captured["kwargs"] = kwargs
        return "verified"

    monkeypatch.setattr(v6, "_V5_VERIFY_REGISTERED_EMAIL", fake_verify)
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=3,
    )

    result = v6._verify_registered_email_v6(
        credential,
        "battle-password",
        args="args",
    )

    assert result == "verified"
    assert isinstance(captured["credential"], V5EmailCredential)
    assert captured["credential"].client_id == "client-id"
    assert captured["credential"].refresh_token == "refresh-token"
    assert captured["account_password"] == "battle-password"
    assert captured["kwargs"] == {"args": "args"}


def test_v6_parser_names_the_v6_pool_file() -> None:
    parser = v6._build_parser_v6()
    action = next(
        item for item in parser._actions if item.dest == "email_source"
    )

    assert "Email_registing_v6.txt" in action.help
    assert "Email_registing.txt" not in action.help


def test_v6_parser_verifies_email_by_default(monkeypatch) -> None:
    monkeypatch.delenv("V6_VERIFY_EMAIL", raising=False)
    monkeypatch.delenv("V5_VERIFY_EMAIL", raising=False)

    args = v6._build_parser_v6().parse_args([])

    assert args.verify_email == "yes"


def test_verification_bridge_can_skip_browser_verification(monkeypatch) -> None:
    monkeypatch.setattr(
        v6,
        "_V5_VERIFY_REGISTERED_EMAIL",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("mail verifier must not run")
        ),
    )
    credential = parse_credential_line(
        "mail@example.com----mail-pass----client-id----refresh-token",
        source_index=1,
    )

    result = v6._verify_registered_email_v6(
        credential,
        "battle-password",
        args=SimpleNamespace(verify_email="no"),
    )

    assert result.ok is True
    assert result.status == "skipped"


@pytest.mark.parametrize(
    "message",
    [
        "bootstrap ended on unexpected form 'login': Welcome back mail@example.com",
        "引导流程结束在意外表单 'login'：Battle.net Login Welcome back mail@example.com",
    ],
)
def test_login_form_bootstrap_is_terminal_exit_43(monkeypatch, message) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(v6, "_V5_RUN_TO_CAPTCHA", fail)
    client = SimpleNamespace(
        state=SimpleNamespace(data={"identity": {"email": "mail@example.com"}})
    )

    with pytest.raises(SystemExit) as caught:
        v6._run_to_captcha_v6(client, country="USA")

    assert caught.value.code == v6.EXIT_EMAIL_ALREADY_REGISTERED


def test_other_bootstrap_errors_remain_retryable(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("bootstrap GET failed: HTTP 503")

    monkeypatch.setattr(v6, "_V5_RUN_TO_CAPTCHA", fail)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        v6._run_to_captcha_v6(SimpleNamespace(), country="USA")
