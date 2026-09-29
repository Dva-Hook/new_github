from __future__ import annotations

from pathlib import Path

import register_ruyipage_v7 as v7
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "register-ruyipage-v7.yml"


def test_w1_failure_record_uses_account_password_format(tmp_path: Path) -> None:
    output = tmp_path / "w1_failed_account.txt"

    v7.write_w1_failure_account(
        "failed@example.com",
        "password123",
        output,
    )

    assert output.read_text(encoding="utf-8") == (
        "failed@example.com----password123\n"
    )


def test_w1_success_clears_stale_failure_record(tmp_path: Path) -> None:
    output = tmp_path / "w1_failed_account.txt"
    output.write_text("stale@example.com----old-password\n", encoding="utf-8")

    v7.clear_w1_failure_account(output)

    assert not output.exists()


def test_v7_workflow_uploads_w1_failure_collection() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8-sig"))
    register_steps = workflow["jobs"]["register"]["steps"]
    account_upload = next(
        step for step in register_steps if step.get("name") == "上传 V7 账号数据"
    )
    assert "w1_failed_account.txt" in account_upload["with"]["path"]

    collect_steps = workflow["jobs"]["collect"]["steps"]
    collect_step = next(
        step for step in collect_steps if step.get("name") == "汇总账号与邮箱状态"
    )
    assert "w1_failed_account.txt" in collect_step["run"]
    assert 'Path("w1_failed_accounts.txt")' in collect_step["run"]

    failure_upload = next(
        step
        for step in collect_steps
        if step.get("name") == "上传 W1 未创建成功账号"
    )
    assert failure_upload["with"]["name"] == "ALL-W1未创建成功"
    assert failure_upload["with"]["path"] == "w1_failed_accounts.txt"
