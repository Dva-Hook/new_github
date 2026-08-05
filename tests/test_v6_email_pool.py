from __future__ import annotations

from pathlib import Path

import pytest

from v6_email_pool import (
    is_email_verification_pending,
    load_email_pool,
    parse_credential_line,
    remove_consumed_emails,
    select_email_credential,
)


RAW = (
    "JamieByrdNo94955@outlook.com----hjruhg65186----"
    "9e5f94bc-e8a4-4e73-b8be-63364c29d753----"
    "M.C534_BL2.0.U.MsaArtifacts.token-value$"
)


def test_v6_parser_preserves_original_line_and_maps_mail_fields() -> None:
    credential = parse_credential_line(RAW, source_index=7)

    assert credential.email == "JamieByrdNo94955@outlook.com"
    assert credential.mailbox_password == "hjruhg65186"
    assert credential.client_id == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    assert credential.refresh_token == "M.C534_BL2.0.U.MsaArtifacts.token-value$"
    assert credential.raw_line == RAW
    assert credential.source_index == 7

    v5_credential = credential.to_v5()
    assert v5_credential.email == credential.email
    assert v5_credential.mailbox_password == credential.mailbox_password
    assert v5_credential.refresh_token == credential.refresh_token
    assert v5_credential.client_id == credential.client_id


def test_v6_pool_allocates_one_deterministic_row_per_job(tmp_path: Path) -> None:
    second = (
        "second@example.com----mail-pass-2----client-id-2----refresh-token-2"
    )
    pool = tmp_path / "Email_registing_v6.txt"
    pool.write_text(f"{RAW}\n{second}\n", encoding="utf-8")

    credentials = load_email_pool(pool)
    assert [item.email for item in credentials] == [
        "JamieByrdNo94955@outlook.com",
        "second@example.com",
    ]
    assert select_email_credential(pool, 1).raw_line == RAW
    assert select_email_credential(pool, 2).raw_line == second


def test_v6_pool_rejects_old_delimiter_and_duplicate_email(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="格式错误"):
        parse_credential_line(
            "user@example.com|password|refresh-token|client-id",
            source_index=1,
        )

    pool = tmp_path / "Email_registing_v6.txt"
    pool.write_text(f"{RAW}\n{RAW}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复邮箱"):
        load_email_pool(pool)


def test_v6_remove_consumed_keeps_unconsumed_raw_line(tmp_path: Path) -> None:
    second = (
        "second@example.com----mail-pass-2----client-id-2----refresh-token-2"
    )
    pool = tmp_path / "Email_registing_v6.txt"
    pool.write_text(f"{RAW}\n{second}\n", encoding="utf-8")

    result = remove_consumed_emails(pool, ["jamiebyrdno94955@outlook.com"])

    assert result["removed"] == 1
    assert result["remaining"] == 1
    assert pool.read_text(encoding="utf-8") == second + "\n"


def test_v6_pending_email_verification_requires_an_attempted_failure() -> None:
    failed = {
        "ok": True,
        "emailSource": "pool",
        "emailVerification": {
            "ok": False,
            "status": "verification_mail_missing",
        },
    }
    verified = {
        **failed,
        "emailVerification": {"ok": True, "status": "verified"},
    }
    skipped = {
        **failed,
        "emailVerification": {"ok": True, "status": "skipped"},
    }
    not_requested = {
        **failed,
        "emailVerification": {"ok": False, "status": "not_requested"},
    }

    assert is_email_verification_pending(failed) is True
    assert is_email_verification_pending(verified) is False
    assert is_email_verification_pending(skipped) is False
    assert is_email_verification_pending(not_requested) is False
    assert is_email_verification_pending({**failed, "emailSource": "generated"}) is False
    assert is_email_verification_pending({**failed, "ok": False}) is False
