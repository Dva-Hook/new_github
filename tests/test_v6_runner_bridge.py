from __future__ import annotations

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
