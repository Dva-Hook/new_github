# -*- coding: utf-8 -*-
"""V7 entrypoint: V6 registration + W1 step (login with RuyiPage and create WoW Trial)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import register_ruyipage_v6 as v6
import v6_email_pool
import v7_w1_step


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ruyipage_http_v7_register" / "runs"
_V6_BUILD_PARSER = v6._build_parser_v6
_V6_MAIN = v6.main

LOG = logging.getLogger(__name__)


def _map_v7_environment() -> None:
    """Expose V7 settings to V6."""
    for suffix in (
        "SOLVER",
        "BROWSER",
        "COUNTRY",
        "EMAIL_SOURCE",
        "WOW1_ACCOUNT_FILE",
        "WOW1_ACCOUNT_INDEX",
        "EMAIL_BROWSER_CACHE_DIR",
        "VERIFY_EMAIL",
        "CAPMONSTER_PROXY_MODE",
        "CAPMONSTER_USER_AGENT_URL",
        "PROXY_DIRECT_HOSTS",
        "STATIC_CACHE_DIR",
        "USER_AGENT",
        "W1_HEADLESS",
        "W1_USER_DIR",
    ):
        source = f"V7_{suffix}"
        target_v6 = f"V6_{suffix}"
        target_v5 = f"V5_{suffix}"
        if source in os.environ:
            os.environ[target_v6] = os.environ[source]
            os.environ[target_v5] = os.environ[source]

    # V7 特殊映射：wow1_account.txt → email_pool_file
    if "V7_WOW1_ACCOUNT_FILE" in os.environ:
        os.environ["V6_EMAIL_POOL_FILE"] = os.environ["V7_WOW1_ACCOUNT_FILE"]
        os.environ["V5_EMAIL_POOL_FILE"] = os.environ["V7_WOW1_ACCOUNT_FILE"]

    if "V7_WOW1_ACCOUNT_INDEX" in os.environ:
        os.environ["V6_EMAIL_POOL_INDEX"] = os.environ["V7_WOW1_ACCOUNT_INDEX"]
        os.environ["V5_EMAIL_POOL_INDEX"] = os.environ["V7_WOW1_ACCOUNT_INDEX"]

    # API keys
    if "V7_TWOCAPTCHA_API_KEY" in os.environ:
        os.environ["V6_TWOCAPTCHA_API_KEY"] = os.environ["V7_TWOCAPTCHA_API_KEY"]
        os.environ["TWOCAPTCHA_API_KEY"] = os.environ["V7_TWOCAPTCHA_API_KEY"]
    if "V7_SOLVECAPTCHA_API_KEY" in os.environ:
        os.environ["V6_SOLVECAPTCHA_API_KEY"] = os.environ["V7_SOLVECAPTCHA_API_KEY"]
        os.environ["SOLVECAPTCHA_API_KEY"] = os.environ["V7_SOLVECAPTCHA_API_KEY"]
    if "V7_EZCAPTCHA_API_KEY" in os.environ:
        os.environ["V6_EZCAPTCHA_API_KEY"] = os.environ["V7_EZCAPTCHA_API_KEY"]
        os.environ["EZCAPTCHA_API_KEY"] = os.environ["V7_EZCAPTCHA_API_KEY"]


def _setup_v7_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [HTTP-V7] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    for name in ("urllib3", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_parser_v7():
    parser = _V6_BUILD_PARSER()
    parser.description = "V6 完整注册逻辑 + V7 W1 步骤（RuyiPage 登录并创建 WoW Trial）"

    # 修改邮箱来源说明
    for action in parser._actions:
        if action.dest == "email_source":
            action.help = (
                "generated or one deterministic row from "
                "wow1_account.txt"
            )
            break

    # 添加 W1 步骤相关参数
    parser.add_argument(
        "--w1-headless",
        action="store_true",
        default=os.environ.get("V7_W1_HEADLESS", "").strip().lower() == "true",
        help="W1 步骤使用无头模式（默认：否）",
    )

    parser.add_argument(
        "--w1-user-dir",
        type=Path,
        default=None,
        help="W1 步骤 RuyiPage 用户数据目录（默认：临时目录）",
    )

    parser.add_argument(
        "--w1-proxy",
        default="",
        help="W1 步骤使用的代理地址（默认：继承注册代理）",
    )

    return parser


def _execute_w1_after_registration(
    email: str,
    password: str,
    proxy: str = "",
    headless: bool = False,
    user_dir: Path | None = None,
) -> bool:
    """
    V7 注册成功后执行 W1 步骤

    Args:
        email: 注册成功的邮箱
        password: 账号密码
        proxy: 代理地址
        headless: 是否无头模式
        user_dir: 用户数据目录

    Returns:
        bool: W1 步骤是否成功
    """
    try:
        LOG.info("=" * 80)
        LOG.info("V7 W1 步骤：注册成功，开始创建 WoW Trial 账号")
        LOG.info("=" * 80)

        w1_success = v7_w1_step.execute_w1_step_with_login(
            email=email,
            password=password,
            proxy=proxy,
            headless=headless,
            user_dir=user_dir,
        )

        if w1_success:
            LOG.info("=" * 80)
            LOG.info("V7 ✅ W1 步骤成功：WoW Trial 账号已创建")
            LOG.info("=" * 80)
        else:
            LOG.warning("=" * 80)
            LOG.warning("V7 ⚠️ W1 步骤失败：WoW Trial 账号创建失败，但注册流程已完成")
            LOG.warning("=" * 80)

        return w1_success

    except Exception as e:
        LOG.exception(f"V7 W1 步骤执行异常: {e}")
        LOG.warning("V7 W1 步骤失败，但注册流程已完成")
        return False


def _install_v7_contract() -> None:
    """Install V7 runtime behavior on top of V6."""
    # V7 使用独立的输出目录
    v6.DEFAULT_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT

    # V7 使用独立的日志格式
    v6._setup_v6_logging = _setup_v7_logging

    # V7 使用 wow1_account.txt 作为邮箱源


def main() -> int:
    """V7 主入口：V6 注册 + W1 步骤"""
    _map_v7_environment()
    _install_v7_contract()

    # 解析参数
    parser = _build_parser_v7()
    args = parser.parse_args()

    # 执行 V6 注册
    LOG.info("=" * 80)
    LOG.info("V7 开始执行：V6 注册逻辑")
    LOG.info("=" * 80)

    v6_exit_code = _V6_MAIN()

    if v6_exit_code != 0:
        LOG.error(f"V7 注册失败，退出码: {v6_exit_code}")
        return v6_exit_code

    LOG.info("=" * 80)
    LOG.info("V7 ✅ 注册成功")
    LOG.info("=" * 80)

    # 读取注册结果
    try:
        # 查找最新的注册结果
        runs_dir = DEFAULT_OUTPUT_ROOT
        if not runs_dir.exists():
            LOG.error("V7 未找到注册结果目录")
            return 1

        run_dirs = sorted(runs_dir.glob("run_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not run_dirs:
            LOG.error("V7 未找到任何注册运行记录")
            return 1

        latest_run = run_dirs[0]
        account_file = latest_run / "account_generated.json"

        if not account_file.exists():
            LOG.error(f"V7 未找到账号生成文件: {account_file}")
            return 1

        account_data = json.loads(account_file.read_text(encoding="utf-8"))
        email = account_data.get("email")
        password = account_data.get("password")

        if not email or not password:
            LOG.error("V7 账号数据不完整")
            return 1

        LOG.info(f"V7 读取到注册账号: {email}")

        # 执行 W1 步骤
        w1_proxy = args.w1_proxy or args.proxy
        w1_success = _execute_w1_after_registration(
            email=email,
            password=password,
            proxy=w1_proxy,
            headless=args.w1_headless,
            user_dir=args.w1_user_dir,
        )

        # 保存 W1 结果
        w1_result_file = latest_run / "w1_result.json"
        w1_result = {
            "ok": w1_success,
            "email": email,
            "note": "WoW Trial 账号创建成功" if w1_success else "WoW Trial 账号创建失败",
        }
        w1_result_file.write_text(
            json.dumps(w1_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if w1_success:
            LOG.info("=" * 80)
            LOG.info("V7 🎉 完整流程成功：注册 + WoW Trial 创建")
            LOG.info("=" * 80)
            return 0
        else:
            LOG.warning("=" * 80)
            LOG.warning("V7 ⚠️ 注册成功但 W1 步骤失败")
            LOG.warning("=" * 80)
            # 注册成功，W1 失败不影响退出码
            return 0

    except Exception as e:
        LOG.exception(f"V7 W1 步骤调度失败: {e}")
        LOG.warning("V7 注册成功但 W1 步骤未执行")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
