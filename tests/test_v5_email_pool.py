from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from v5_email_pool import (
    load_email_pool,
    parse_credential_line,
    remove_consumed_emails,
    select_email_credential,
    validate_pool_capacity,
)


class EmailPoolTests(unittest.TestCase):
    def test_parse_repr_redacts_secrets(self) -> None:
        credential = parse_credential_line(
            "first@example.com|mail-secret|refresh-secret|client-secret",
            source_index=3,
        )
        rendered = repr(credential)
        self.assertIn("first@example.com", rendered)
        self.assertNotIn("mail-secret", rendered)
        self.assertNotIn("refresh-secret", rendered)
        self.assertNotIn("client-secret", rendered)
        self.assertEqual(
            credential.public_summary(),
            {"email": "first@example.com", "sourceIndex": 3},
        )

    def test_deterministic_selection_and_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Email_registing.txt"
            path.write_text(
                "first@example.com|p1|r1|c1\n"
                "second@example.com|p2|r2|c2\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_pool_capacity(path, 2), 2)
            self.assertEqual(select_email_credential(path, 1).email, "first@example.com")
            self.assertEqual(select_email_credential(path, 2).email, "second@example.com")

    def test_matrix_indices_always_receive_distinct_emails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Email_registing.txt"
            rows = [
                f"job{index}@example.com|p{index}|r{index}|c{index}"
                for index in range(1, 257)
            ]
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            assigned = {
                select_email_credential(path, index).email.casefold()
                for index in range(1, 257)
            }
            self.assertEqual(len(assigned), 256)

    def test_duplicate_email_rows_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Email_registing.txt"
            path.write_text(
                "same@example.com|p1|r1|c1\n"
                "SAME@example.com|p2|r2|c2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "重复邮箱"):
                load_email_pool(path)

    def test_remove_only_successful_email(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Email_registing.txt"
            path.write_text(
                "first@example.com|p1|r1|c1\n"
                "second@example.com|p2|r2|c2\n"
                "third@example.com|p3|r3|c3\n",
                encoding="utf-8",
            )
            result = remove_consumed_emails(path, ["SECOND@example.com"])
            self.assertEqual(result["removed"], 1)
            remaining = load_email_pool(path)
            self.assertEqual(
                [item.email for item in remaining],
                ["first@example.com", "third@example.com"],
            )

    def test_empty_success_set_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Email_registing.txt"
            original = "first@example.com|p1|r1|c1\r\n"
            path.write_bytes(original.encode("utf-8"))
            result = remove_consumed_emails(path, [])
            self.assertEqual(result["removed"], 0)
            self.assertEqual(path.read_bytes(), original.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
