from __future__ import annotations

from datetime import datetime, timezone

import pytest

import o2_email_reader as target


class _Response:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.reason = "OK" if status_code < 400 else "Bad Request"

    def json(self):
        return self.payload


def test_o2_client_detects_permission_before_refreshing_messages() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class Session:
        def post(self, url, *, json, headers, timeout):
            calls.append((url, dict(json)))
            if url.endswith("/detect-permission"):
                return _Response(
                    {
                        "success": True,
                        "token_type": "o2",
                        "use_local_ip": True,
                        "scope": "https://graph.microsoft.com/User.Read",
                    }
                )
            return _Response(
                {
                    "success": True,
                    "data": [
                        {
                            "id": "message-1",
                            "subject": "Your security code",
                            "from_address": "noreply@battle.net",
                            "from_name": "Battle.net",
                            "received_time": "2026-09-11T07:27:12Z",
                            "body_preview": "Your security code: KQDLX7",
                            "body": "<p>Your security code: KQDLX7</p>",
                            "is_read": False,
                        }
                    ],
                }
            )

    client = target.O2MailboxClient(
        Session(), base_url="https://mail.example.test"
    )
    messages = client.refresh_messages(
        email="mail@example.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )

    assert [url for url, _ in calls] == [
        "https://mail.example.test/detect-permission",
        "https://mail.example.test/api/emails/refresh",
    ]
    assert calls[1][1] == {
        "email_address": "mail@example.com",
        "client_id": "client-id",
        "refresh_token": "refresh-token",
        "folder": "inbox",
        "token_type": "o2",
    }
    assert messages[0]["from"]["emailAddress"]["address"] == "noreply@battle.net"
    assert messages[0]["receivedDateTime"] == "2026-09-11T07:27:12Z"
    assert messages[0]["body"]["content"].startswith("<p>")


def test_o2_client_rejects_non_o2_permission_without_echoing_secret() -> None:
    class Session:
        def post(self, url, **kwargs):
            return _Response(
                {
                    "success": True,
                    "token_type": "graph",
                    "scope": "User.Read",
                }
            )

    with pytest.raises(target.O2MailboxError) as error:
        target.O2MailboxClient(
            Session(), base_url="https://mail.example.test"
        ).refresh_messages(
            email="mail@example.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

    message = str(error.value)
    assert "o2" in message.lower()
    assert "refresh-token" not in message


def test_o2_message_received_time_is_preserved_for_datetime_filters() -> None:
    class Session:
        def post(self, url, **kwargs):
            if url.endswith("/detect-permission"):
                return _Response({"success": True, "token_type": "o2"})
            return _Response(
                {
                    "success": True,
                    "data": [
                        {
                            "from_address": "noreply@battle.net",
                            "received_time": "2026-09-11T07:27:12+00:00",
                            "body_preview": "x",
                            "body": "x",
                        }
                    ],
                }
            )

    messages = target.O2MailboxClient(
        Session(), base_url="https://mail.example.test"
    ).refresh_messages(
        email="mail@example.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )

    parsed = datetime.fromisoformat(
        messages[0]["receivedDateTime"].replace("Z", "+00:00")
    )
    assert parsed.tzinfo == timezone.utc
