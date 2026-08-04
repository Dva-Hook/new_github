from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "register-ruyipage-v6.yml"


def test_v6_workflow_uses_v6_pool_runner_and_output_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'EMAIL_POOL_FILE: "Email_registing_v6.txt"' in text
    assert "from v6_email_pool import validate_pool_capacity" in text
    assert "register_ruyipage_v6.py" in text
    assert "V6_EMAIL_POOL_FILE: Email_registing_v6.txt" in text
    assert "from v6_email_pool import remove_consumed_emails" in text
    assert 'Path("Email_registing_v6.txt")' in text
    assert "git add Email_registing_v6.txt" in text
    assert 'f"API：{api_line}"' in text
    assert 'lines.append(f"说明：{note}")' not in text
    assert "V5_EMAIL_POOL_FILE: Email_registing.txt" not in text


def test_v6_workflow_retains_unique_matrix_allocation_and_serial_pool_runs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "list(range(1, count + 1))" in text
    assert "V6_EMAIL_POOL_INDEX: ${{ matrix.index }}" in text
    assert "cancel-in-progress: false" in text
    assert "format('V6-待注册邮箱-{0}-{1}'" in text


def test_v6_workflow_supports_optional_email_verification() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "verify_email:" in text
    assert 'default: "是"' in text
    assert 'verify_email_map = {"是": "yes", "否": "no"}' in text
    assert "V6_VERIFY_EMAIL: ${{ needs.prepare.outputs.verify_email }}" in text
    assert '--verify-email "$V6_VERIFY_EMAIL"' in text
    assert "needs.prepare.outputs.verify_email == 'yes'" in text


def test_v6_workflow_quarantines_login_form_without_retrying() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'if [ "$last_rc" -eq 43 ]; then' in text
    assert "already_registered_email.txt" in text
    assert "already_registered_emails.txt" in text
    assert "pool_removal_emails.txt" in text
    assert 'success_path = Path("pool_removal_emails.txt")' in text
