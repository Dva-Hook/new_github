from __future__ import annotations

from pathlib import Path

import workflow_runner


ROOT = Path(__file__).resolve().parents[1]


def test_unified_runner_exposes_every_active_command() -> None:
    assert set(workflow_runner.COMMANDS) == {
        "capture-images",
        "email-verify",
        "funcaptcha-snapshots",
        "proxy-pool",
        "register",
        "register-v3",
        "register-v4",
        "register-v5",
        "register-v6",
    }


def test_internal_modules_compile_without_import_side_effects() -> None:
    assert workflow_runner.check_modules() == 0


def test_internal_module_keeps_original_virtual_project_path() -> None:
    module = workflow_runner.load_module("v6_email_pool")
    assert Path(module.__file__).resolve() == ROOT / "v6_email_pool.py"
    assert not (ROOT / "v6_email_pool.py").exists()
    assert (ROOT / "workflow_modules" / "v6_email_pool.py").is_file()


def test_active_workflows_use_only_the_unified_project_entrypoint() -> None:
    workflows = ROOT / ".github" / "workflows"
    forbidden = {
        "register_capture_images.py",
        "email_verify_ruyipage_v3.py",
        "funcaptcha_http_snapshots.py",
        "v5_proxy_pool.py",
        "register_ruyipage_v5.py",
        "register_ruyipage_v6.py",
    }
    for path in workflows.glob("*.yml"):
        text = path.read_text(encoding="utf-8-sig")
        assert not (forbidden & set(text.replace("\\", " ").split())), path.name

    combined = "\n".join(
        path.read_text(encoding="utf-8-sig") for path in workflows.glob("*.yml")
    )
    assert "workflow_runner.py capture-images" in combined
    assert "workflow_runner.py email-verify" in combined
    assert "workflow_runner.py funcaptcha-snapshots" in combined
    assert "workflow_runner.py proxy-pool" in combined
    assert "workflow_runner.py register-v5" in combined
    assert "workflow_runner.py register-v6" in combined
