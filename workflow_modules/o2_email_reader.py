# -*- coding: utf-8 -*-
"""O2 mailbox-reader compatibility client.

The normal verification path reads Microsoft Graph directly.  This module is
only used after that path times out or cannot read mail.  It deliberately
normalizes the external reader response to the small Graph-shaped message
contract consumed by the existing Battle.net link/code extractors.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

import requests


DEFAULT_BASE_URL = os.environ.get(
    "BATTLE_NET_O2_MAIL_BASE_URL",
    "https://app.wyx66.com",
).strip()
PERMISSION_PATH = "/detect-permission"
REFRESH_PATH = "/api/emails/refresh"
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "BattleNetO2MailboxFallback/1.0"


class O2MailboxError(RuntimeError):
    """The O2 mailbox service returned an unusable result."""


class O2TransportError(O2MailboxError):
    """The O2 mailbox service could not be reached or decoded."""


class O2PermissionError(O2MailboxError):
    """The credential was not accepted as an O2 mailbox credential."""


@dataclass(frozen=True)
class O2Permission:
    token_type: str
    scope: str
    use_local_ip: bool


def _normalized_base_url(value: str) -> str:
    base = str(value or DEFAULT_BASE_URL).strip()
    if not base:
        base = DEFAULT_BASE_URL
    return base.rstrip("/") + "/"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _normalize_message(row: Mapping[str, Any]) -> dict[str, Any]:
    sender = _mapping_value(row, "from")
    sender_address = _text(
        _mapping_value(row, "from_address", "fromAddress", "sender_address")
    )
    sender_name = _text(
        _mapping_value(row, "from_name", "fromName", "sender_name")
    )
    if isinstance(sender, Mapping):
        address_node = sender.get("emailAddress")
        if isinstance(address_node, Mapping):
            sender_address = sender_address or _text(address_node.get("address"))
            sender_name = sender_name or _text(address_node.get("name"))
        else:
            sender_address = sender_address or _text(sender.get("address"))
            sender_name = sender_name or _text(sender.get("name"))

    raw_body = _mapping_value(row, "body")
    body_content = ""
    body_type = "html"
    if isinstance(raw_body, Mapping):
        body_content = _text(
            _mapping_value(raw_body, "content", "text", "value")
        )
        body_type = _text(raw_body.get("contentType")) or "html"
    else:
        body_content = _text(raw_body)

    return {
        "id": _text(_mapping_value(row, "id", "message_id", "messageId")),
        "subject": _text(_mapping_value(row, "subject")),
        "from": {
            "emailAddress": {
                "address": sender_address,
                "name": sender_name,
            }
        },
        "receivedDateTime": _text(
            _mapping_value(row, "received_time", "receivedDateTime", "received_at")
        ),
        "bodyPreview": _text(
            _mapping_value(row, "body_preview", "bodyPreview", "preview")
        ),
        "body": {
            "content": body_content,
            "contentType": body_type,
        },
    }


class O2MailboxClient:
    """Small session-scoped client for the O2 fallback endpoints."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.session = session or requests.Session()
        self.base_url = _normalized_base_url(base_url or DEFAULT_BASE_URL)
        self.timeout = max(1.0, float(timeout))
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.permission: Optional[O2Permission] = None
        self._credential_key: Optional[tuple[str, str]] = None

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "Accept": accept,
            "Content-Type": "application/json",
            "Origin": self.base_url.rstrip("/"),
            "Referer": self.base_url,
            "User-Agent": self.user_agent,
        }

    def _post_json(
        self, path: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        url = urljoin(self.base_url, path.lstrip("/"))
        try:
            response = self.session.post(
                url,
                json=dict(payload),
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise O2TransportError(
                f"O2 邮箱服务请求失败：{type(exc).__name__}"
            ) from exc
        try:
            decoded = response.json()
        except ValueError as exc:
            raise O2TransportError(
                f"O2 邮箱服务返回了无效 JSON：HTTP {response.status_code}"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise O2TransportError(
                f"O2 邮箱服务返回格式错误：HTTP {response.status_code}"
            )
        if not 200 <= int(response.status_code) < 300:
            raise O2MailboxError(
                f"O2 邮箱服务请求失败：HTTP {response.status_code}"
            )
        return dict(decoded)

    def detect_permission(
        self, *, client_id: str, refresh_token: str
    ) -> O2Permission:
        payload = self._post_json(
            PERMISSION_PATH,
            {
                "client_id": str(client_id),
                "refresh_token": str(refresh_token),
            },
        )
        if payload.get("success") is not True:
            raise O2PermissionError("O2 邮箱服务未接受该凭证")
        token_type = _text(payload.get("token_type")).casefold()
        if token_type != "o2":
            raise O2PermissionError(
                f"O2 邮箱服务返回了不支持的 token_type：{token_type or 'empty'}"
            )
        permission = O2Permission(
            token_type="o2",
            scope=_text(payload.get("scope")),
            use_local_ip=bool(payload.get("use_local_ip")),
        )
        self.permission = permission
        self._credential_key = (str(client_id), str(refresh_token))
        return permission

    def _ensure_permission(self, *, client_id: str, refresh_token: str) -> O2Permission:
        key = (str(client_id), str(refresh_token))
        if self.permission is None or self._credential_key != key:
            return self.detect_permission(
                client_id=client_id,
                refresh_token=refresh_token,
            )
        return self.permission

    def refresh_messages(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        folder: str = "inbox",
    ) -> list[dict[str, Any]]:
        permission = self._ensure_permission(
            client_id=client_id,
            refresh_token=refresh_token,
        )
        payload = self._post_json(
            REFRESH_PATH,
            {
                "email_address": str(email),
                "client_id": str(client_id),
                "refresh_token": str(refresh_token),
                "folder": str(folder or "inbox"),
                "token_type": permission.token_type,
            },
        )
        if payload.get("success") is False:
            raise O2MailboxError("O2 邮箱服务读取邮件失败")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise O2MailboxError("O2 邮箱服务返回的邮件列表格式错误")
        return [
            _normalize_message(row)
            for row in rows
            if isinstance(row, Mapping)
        ]


__all__ = [
    "DEFAULT_BASE_URL",
    "O2MailboxClient",
    "O2MailboxError",
    "O2Permission",
    "O2PermissionError",
    "O2TransportError",
]
