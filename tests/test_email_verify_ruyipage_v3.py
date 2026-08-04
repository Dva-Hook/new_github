from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import email_verify_ruyipage_v3 as target
from v5_email_verifier import EmailVerificationResult


API_LINE = "mail@example.com----mail-pass----client-id----refresh-token"


def test_parse_v3_account_blocks_preserves_v6_api_record() -> None:
    records = target.parse_account_records(
        "账号：mail@example.com\n"
        "密码：battle-pass\n"
        f"API：{API_LINE}\n"
    )

    assert len(records) == 1
    assert records[0].email == "mail@example.com"
    assert records[0].password == "battle-pass"
    assert records[0].credential.client_id == "client-id"
    assert records[0].api_line == API_LINE
    assert records[0].source_index == 1


def test_parse_v3_account_blocks_rejects_duplicate_email() -> None:
    text = (
        "account: mail@example.com\npassword: one\nAPI: "
        f"{API_LINE}\n\n"
        "account: MAIL@example.com\npassword: two\nAPI: "
        f"{API_LINE}\n"
    )

    try:
        target.parse_account_records(text)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate account email was accepted")


def test_no_visible_banner_is_a_success_without_resend(monkeypatch) -> None:
    page = object()
    account = target.parse_account_records(
        "account: mail@example.com\npassword: battle-pass\nAPI: "
        f"{API_LINE}\n"
    )[0]
    calls: list[str] = []

    monkeypatch.setattr(
        target.v6,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target.v6,
        "wait_email_verified",
        lambda *args, **kwargs: {"verified": False, "unverified": False},
    )
    monkeypatch.setattr(
        target,
        "has_visible_unverified_banner",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        target.v6,
        "request_verification_email",
        lambda *args, **kwargs: calls.append("resend"),
    )

    result = target._verify_after_login(
        page,
        account,
        SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        not_before=datetime.now(timezone.utc),
    )

    assert result.ok is True
    assert result.status == "no_unverified_banner"
    assert calls == []


def test_banner_path_delegates_to_v6_dual_resend_flow(monkeypatch) -> None:
    page = object()
    account = target.parse_account_records(
        "account: mail@example.com\npassword: battle-pass\nAPI: "
        f"{API_LINE}\n"
    )[0]
    calls: list[str] = []
    requested_at = datetime.now(timezone.utc)

    monkeypatch.setattr(
        target.v6,
        "login_battle_net",
        lambda *args, **kwargs: EmailVerificationResult(True, "logged_in"),
    )
    monkeypatch.setattr(
        target.v6,
        "wait_email_verified",
        lambda *args, **kwargs: {"verified": False, "unverified": True},
    )
    monkeypatch.setattr(target, "has_visible_unverified_banner", lambda *a, **k: True)
    monkeypatch.setattr(
        target.v6,
        "request_verification_email",
        lambda *args, **kwargs: calls.append("resend") or requested_at,
    )
    monkeypatch.setattr(
        target.v6,
        "poll_verification_link_attempts",
        lambda *args, **kwargs: calls.append("poll") or (
            "https://account.battle.net/overview?ticket=fresh",
            3,
            1,
        ),
    )
    monkeypatch.setattr(
        target.v6,
        "open_verification_link",
        lambda *args, **kwargs: calls.append("open") or True,
    )

    result = target._verify_after_login(
        page,
        account,
        SimpleNamespace(
            email_login_timeout=60,
            email_mail_timeout=120,
            email_verification_timeout=20,
        ),
        not_before=datetime.now(timezone.utc),
    )

    assert result.ok is True
    assert result.status == "verified"
    assert calls == ["resend", "poll", "open"]


def test_args_limit_concurrency_to_twenty() -> None:
    args = target.parse_args(["--max-parallel", "20"])
    target._validate_args(args)

    bad = target.parse_args(["--max-parallel", "21"])
    try:
        target._validate_args(bad)
    except ValueError as exc:
        assert "1 到 20" in str(exc)
    else:
        raise AssertionError("parallelism above 20 was accepted")


def test_matrix_account_index_is_one_based_and_unique() -> None:
    records = target.parse_account_records(
        "account: one@example.com\npassword: one-pass\n"
        "API: one@example.com----mail-pass----client-id----token-one\n\n"
        "account: two@example.com\npassword: two-pass\n"
        "API: two@example.com----mail-pass----client-id----token-two\n"
    )

    assert [item.email for item in target.select_account_records(records, 1)] == [
        "one@example.com"
    ]
    assert [item.email for item in target.select_account_records(records, 2)] == [
        "two@example.com"
    ]
    assert target.select_account_records(records, 0) == records


def test_render_successful_accounts_uses_v3_format() -> None:
    account = target.parse_account_records(
        "account: mail@example.com\npassword: battle-pass\nAPI: "
        f"{API_LINE}\n"
    )[0]

    assert target.render_account_records([account]) == (
        "账号：mail@example.com\n"
        "密码：battle-pass\n"
        f"API：{API_LINE}\n\n"
    )


def test_remove_successful_accounts_keeps_failed_blocks(tmp_path: Path) -> None:
    source = tmp_path / "oath2_account.v3.txt"
    source.write_text(
        "账号：success@example.com\n"
        "密码：success-pass\n"
        "API：success@example.com----mail-pass----client-id----token-one\n\n"
        "账号：failed@example.com\n"
        "密码：failed-pass\n"
        "API：failed@example.com----mail-pass----client-id----token-two\n",
        encoding="utf-8",
    )

    result = target.remove_successful_account_records(
        source,
        ["SUCCESS@example.com"],
    )

    assert result["removed"] == 1
    remaining = source.read_text(encoding="utf-8")
    assert "success@example.com" not in remaining
    assert "账号：failed@example.com" in remaining
    assert "token-two" in remaining


def test_github_workflow_uses_direct_matrix_allocation() -> None:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "email-verify-ruyipage-v3.yml"
    ).read_text(encoding="utf-8")

    assert "oath2_account.v3.txt" in workflow
    assert "max-parallel: ${{ fromJSON(needs.prepare.outputs.max_parallel) }}" in workflow
    assert "--account-index \"$ACCOUNT_INDEX\"" in workflow
    assert "--max-parallel 1" in workflow
    assert "每个任务=1个账号" in workflow
    assert "--proxy" not in workflow
    assert "最多支持 256 个账号任务" in workflow
    assert "max-parallel: ${{ fromJSON(needs.prepare.outputs.max_parallel) }}" in workflow
    assert "if: always()" in workflow
    assert "失败任务也生成结果文件" in workflow
    assert "从账号文件删除验证成功账号" in workflow
    assert "git add oath2_account.v3.txt" in workflow
    assert "contents: write" in workflow
    assert "邮箱验证V3-全部结果" in workflow
