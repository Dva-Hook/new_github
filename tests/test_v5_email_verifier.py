from __future__ import annotations

import unittest
from datetime import datetime, timezone

from v5_email_verifier import direct_battlenet_link, extract_battlenet_link, find_link


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse(self.payload)


class EmailVerifierTests(unittest.TestCase):
    def test_extracts_direct_ticket_link(self) -> None:
        link = "https://account.battle.net/overview?ticket=abc123"
        message = {
            "body": {"content": f'<html><a href="{link}">Verify</a></html>'},
            "bodyPreview": "",
        }
        self.assertEqual(extract_battlenet_link(message), link)

    def test_extracts_nested_redirect_link(self) -> None:
        nested = (
            "https://example.com/click?url="
            "https%3A%2F%2Faccount.battle.net%2Foverview%3Fticket%3Dabc123"
        )
        self.assertEqual(
            direct_battlenet_link(nested),
            "https://account.battle.net/overview?ticket=abc123",
        )

    def test_rejects_non_ticket_links(self) -> None:
        self.assertIsNone(direct_battlenet_link("https://account.battle.net/overview"))
        self.assertIsNone(direct_battlenet_link("https://example.com/overview?ticket=x"))

    def test_find_link_ignores_old_or_wrong_sender_messages(self) -> None:
        link = "https://account.battle.net/overview?ticket=new-ticket"
        payload = {
            "value": [
                {
                    "from": {"emailAddress": {"address": "someone@example.com"}},
                    "receivedDateTime": "2026-07-26T01:30:00Z",
                    "body": {"content": link},
                },
                {
                    "from": {"emailAddress": {"address": "noreply@battle.net"}},
                    "receivedDateTime": "2026-07-25T01:30:00Z",
                    "body": {"content": "https://account.battle.net/overview?ticket=old"},
                },
                {
                    "from": {"emailAddress": {"address": "noreply@battle.net"}},
                    "receivedDateTime": "2026-07-26T01:31:00Z",
                    "body": {"content": link},
                },
            ]
        }
        found, scanned, matching = find_link(
            FakeSession(payload),
            "access-token",
            not_before=datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(found, link)
        self.assertEqual(scanned, 3)
        self.assertEqual(matching, 1)


if __name__ == "__main__":
    unittest.main()
