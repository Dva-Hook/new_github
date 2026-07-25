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


if __name__ == "__main__":
    unittest.main()
