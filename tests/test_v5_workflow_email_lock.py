from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "register-ruyipage-v5.yml"
)


class V5WorkflowEmailLockTests(unittest.TestCase):
    def test_pool_runs_are_serialized_per_repository_branch(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("concurrency:", text)
        self.assertIn("github.repository", text)
        self.assertIn("github.ref_name", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_each_matrix_job_uses_its_own_one_based_pool_index(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("V5_EMAIL_POOL_INDEX: ${{ matrix.index }}", text)
        self.assertIn('--email-pool-index "$V5_EMAIL_POOL_INDEX"', text)

    def test_pool_jobs_restore_and_save_sanitized_browser_cache(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("恢复邮箱验证浏览器静态缓存", text)
        self.assertIn("保存邮箱验证浏览器静态缓存", text)
        self.assertIn(".cache/v5_email_browser_profile", text)
        self.assertIn("V5_EMAIL_BROWSER_CACHE_DIR:", text)
        self.assertIn(
            '--email-browser-cache-dir "$V5_EMAIL_BROWSER_CACHE_DIR"', text
        )
        self.assertIn("保存前清理邮箱验证浏览器身份状态", text)
        self.assertIn("sanitize_cached_profile(cache_dir)", text)
        self.assertIn(
            "steps.sanitize-email-browser-cache.outcome == 'success'", text
        )
        self.assertIn("needs.prepare.outputs.email_source == 'pool'", text)


if __name__ == "__main__":
    unittest.main()
