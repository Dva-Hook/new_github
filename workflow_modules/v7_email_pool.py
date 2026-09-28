# -*- coding: utf-8 -*-
"""V7 邮箱池 - 从 wow1_account.txt 读取纯邮箱列表"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WOW1_ACCOUNT_FILE = PROJECT_ROOT / "wow1_account.txt"


class EmailCredential(NamedTuple):
    """V7 邮箱凭证（仅包含邮箱，无 API 密码和 token）"""
    email: str
    api_password: str = ""
    api_token: str = ""
    raw_line: str = ""

    def to_v5(self):
        """转换为 V5 EmailCredential 格式（兼容层）"""
        return self


def select_email_for_registration(
    pool_file: Path | str | None = None,
    index: int | str | None = None,
) -> str:
    """
    从 wow1_account.txt 选择一个邮箱用于注册

    Args:
        pool_file: wow1_account.txt 路径
        index: 行号（从0开始）或 "next"

    Returns:
        str: 邮箱地址
    """
    if pool_file is None:
        pool_file = os.environ.get(
            "V7_WOW1_ACCOUNT_FILE",
            str(DEFAULT_WOW1_ACCOUNT_FILE),
        )

    pool_path = Path(pool_file)
    if not pool_path.exists():
        raise FileNotFoundError(f"V7 邮箱文件不存在: {pool_path}")

    # 读取所有有效邮箱
    with open(pool_path, "r", encoding="utf-8") as f:
        emails = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    if not emails:
        raise ValueError(f"V7 邮箱文件为空: {pool_path}")

    # 解析索引
    if index is None:
        index = os.environ.get("V7_WOW1_ACCOUNT_INDEX", "0")

    if isinstance(index, str):
        if index.lower() == "next":
            idx = 0
        else:
            idx = int(index)
    else:
        idx = int(index)

    if idx < 0 or idx >= len(emails):
        raise IndexError(
            f"V7 邮箱索引 {idx} 超出范围 (共 {len(emails)} 个邮箱)"
        )

    return emails[idx]


def load_email_pool(pool_file: Path | str | None = None) -> list[str]:
    """
    加载 wow1_account.txt 中的所有邮箱

    Args:
        pool_file: wow1_account.txt 路径

    Returns:
        list[str]: 所有有效邮箱列表
    """
    if pool_file is None:
        pool_file = os.environ.get(
            "V7_WOW1_ACCOUNT_FILE",
            str(DEFAULT_WOW1_ACCOUNT_FILE),
        )

    pool_path = Path(pool_file)
    if not pool_path.exists():
        raise FileNotFoundError(f"V7 邮箱文件不存在: {pool_path}")

    with open(pool_path, "r", encoding="utf-8") as f:
        emails = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    return emails


def validate_pool_capacity(path: Path | str, required: int) -> int:
    """
    验证邮箱池容量是否满足需求

    Args:
        path: wow1_account.txt 路径
        required: 需要的邮箱数量

    Returns:
        int: 实际可用邮箱数量

    Raises:
        ValueError: 邮箱数量不足
    """
    emails = load_email_pool(path)
    required_count = int(required)
    if len(emails) < required_count:
        raise ValueError(
            f"待注册邮箱只有 {len(emails)} 个，任务要求 {required_count} 个"
        )
    return len(emails)


def select_email_credential(
    pool_file: Path | str,
    index: int,
) -> EmailCredential:
    """
    从 wow1_account.txt 选择指定索引的邮箱（兼容 V6 接口）

    Args:
        pool_file: wow1_account.txt 路径
        index: 邮箱索引（从 1 开始，兼容 V6）

    Returns:
        EmailCredential: 邮箱凭证对象
    """
    emails = load_email_pool(pool_file)

    # V6 使用从 1 开始的索引，V7 保持兼容
    if index < 1 or index > len(emails):
        raise IndexError(
            f"V7 邮箱索引 {index} 超出范围 (共 {len(emails)} 个邮箱，索引范围 1-{len(emails)})"
        )

    email = emails[index - 1]

    return EmailCredential(
        email=email,
        api_password="",
        api_token="",
        raw_line=email,
    )


def remove_email_from_pool(email: str, pool_file: Path | str | None = None) -> None:
    """
    从 wow1_account.txt 移除已完成的邮箱

    Args:
        email: 要移除的邮箱
        pool_file: wow1_account.txt 路径
    """
    if pool_file is None:
        pool_file = os.environ.get(
            "V7_WOW1_ACCOUNT_FILE",
            str(DEFAULT_WOW1_ACCOUNT_FILE),
        )

    pool_path = Path(pool_file)
    if not pool_path.exists():
        return

    with open(pool_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    filtered = [
        line for line in lines
        if line.strip() and email not in line
    ]

    with open(pool_path, "w", encoding="utf-8") as f:
        f.writelines(filtered)
