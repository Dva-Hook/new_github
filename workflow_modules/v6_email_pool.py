# -*- coding: utf-8 -*-
"""V6 supplied-email pool parsing and deterministic job allocation.

Input format (one record per line):
    email----mailbox_password----client_id----refresh_token
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from v5_email_pool import EmailCredential as V5EmailCredential


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DELIMITER = "----"


@dataclass(frozen=True, repr=False)
class EmailCredential:
    email: str
    mailbox_password: str
    client_id: str
    refresh_token: str
    raw_line: str
    source_index: int

    def __repr__(self) -> str:
        return (
            "EmailCredential("
            f"email={self.email!r}, source_index={self.source_index})"
        )

    def public_summary(self) -> dict[str, object]:
        return {
            "email": self.email,
            "sourceIndex": self.source_index,
        }

    def to_v5(self) -> V5EmailCredential:
        """Convert field order to the credential shape used by the V5 verifier."""
        return V5EmailCredential(
            email=self.email,
            mailbox_password=self.mailbox_password,
            refresh_token=self.refresh_token,
            client_id=self.client_id,
            source_index=self.source_index,
        )


def parse_credential_line(raw: str, *, source_index: int) -> EmailCredential:
    # Keep the exact non-newline source record for the final API output. Only
    # surrounding whitespace is ignored as it is not part of a credential.
    line = str(raw or "").strip()
    parts = line.split(DELIMITER, 3)
    if len(parts) != 4 or not all(part.strip() for part in parts):
        raise ValueError(f"邮箱凭证第 {source_index} 行格式错误")
    email, mailbox_password, client_id, refresh_token = (
        part.strip() for part in parts
    )
    if not EMAIL_RE.fullmatch(email):
        raise ValueError(f"邮箱凭证第 {source_index} 行邮箱格式错误")
    return EmailCredential(
        email=email,
        mailbox_password=mailbox_password,
        client_id=client_id,
        refresh_token=refresh_token,
        raw_line=line,
        source_index=int(source_index),
    )


def load_email_pool(path: Path | str) -> list[EmailCredential]:
    pool_path = Path(path).expanduser().resolve()
    if not pool_path.is_file():
        raise FileNotFoundError(f"待注册邮箱文件不存在: {pool_path}")
    credentials: list[EmailCredential] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        pool_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        credential = parse_credential_line(raw_line, source_index=line_number)
        normalized = credential.email.casefold()
        if normalized in seen:
            raise ValueError(f"待注册邮箱文件存在重复邮箱: {credential.email}")
        seen.add(normalized)
        credentials.append(credential)
    if not credentials:
        raise ValueError(f"待注册邮箱文件为空: {pool_path}")
    return credentials


def select_email_credential(
    path: Path | str, job_index: int
) -> EmailCredential:
    index = int(job_index)
    if index < 1:
        raise ValueError(f"邮箱池 job index 必须从 1 开始: {index}")
    credentials = load_email_pool(path)
    if index > len(credentials):
        raise IndexError(
            f"待注册邮箱只有 {len(credentials)} 行，无法分配第 {index} 个 job"
        )
    return credentials[index - 1]


def validate_pool_capacity(path: Path | str, required: int) -> int:
    credentials = load_email_pool(path)
    required_count = int(required)
    if len(credentials) < required_count:
        raise ValueError(
            f"待注册邮箱只有 {len(credentials)} 个，任务要求 {required_count} 个"
        )
    return len(credentials)


def remove_consumed_emails(
    path: Path | str, emails: Iterable[str]
) -> dict[str, object]:
    pool_path = Path(path).expanduser().resolve()
    if not pool_path.is_file():
        raise FileNotFoundError(f"待注册邮箱文件不存在: {pool_path}")
    consumed = {
        str(email or "").strip().casefold()
        for email in emails
        if str(email or "").strip()
    }
    original_lines = pool_path.read_text(encoding="utf-8-sig").splitlines()
    original_nonempty = sum(1 for line in original_lines if line.strip())
    if not consumed:
        return {
            "requested": 0,
            "removed": 0,
            "removedEmails": [],
            "remaining": original_nonempty,
        }
    kept: list[str] = []
    removed: list[str] = []
    for line_number, raw_line in enumerate(original_lines, start=1):
        if not raw_line.strip():
            kept.append(raw_line)
            continue
        credential = parse_credential_line(raw_line, source_index=line_number)
        if credential.email.casefold() in consumed:
            removed.append(credential.email)
        else:
            # Preserve the original unconsumed source record byte-for-byte apart
            # from newline normalization, including its token.
            kept.append(raw_line)
    if not removed:
        return {
            "requested": len(consumed),
            "removed": 0,
            "removedEmails": [],
            "remaining": original_nonempty,
        }
    rendered = "\n".join(kept)
    if kept:
        rendered += "\n"
    temporary = pool_path.with_name(pool_path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8", newline="\n")
    os.replace(temporary, pool_path)
    return {
        "requested": len(consumed),
        "removed": len(removed),
        "removedEmails": removed,
        "remaining": sum(1 for line in kept if line.strip()),
    }


def credential_map(path: Path | str) -> dict[str, EmailCredential]:
    """Return case-insensitive email lookup used by account export."""
    return {item.email.casefold(): item for item in load_email_pool(path)}


def is_email_verification_pending(result: object) -> bool:
    """Return whether a successful pool registration has a failed mail check."""
    if not isinstance(result, Mapping):
        return False
    if result.get("ok") is not True or result.get("emailSource") != "pool":
        return False
    verification = result.get("emailVerification")
    if not isinstance(verification, Mapping) or verification.get("ok") is not False:
        return False
    status = str(verification.get("status") or "").strip().lower()
    return status not in {"", "not_requested", "skipped"}


__all__ = [
    "DELIMITER",
    "EmailCredential",
    "credential_map",
    "is_email_verification_pending",
    "load_email_pool",
    "parse_credential_line",
    "remove_consumed_emails",
    "select_email_credential",
    "validate_pool_capacity",
]
