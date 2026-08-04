# -*- coding: utf-8 -*-
"""V6 entrypoint: V5 registration logic with the V6 email-pool contract."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import register_ruyipage_v5 as v5
import v6_email_pool


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ruyipage_http_v6_register" / "runs"
_V5_BUILD_PARSER = v5.build_parser
_V5_VERIFY_REGISTERED_EMAIL = v5.verify_registered_email


def _map_v6_environment() -> None:
    """Expose V6 settings to the unchanged V5 parser implementation."""
    for suffix in (
        "SOLVER",
        "BROWSER",
        "COUNTRY",
        "EMAIL_SOURCE",
        "EMAIL_POOL_FILE",
        "EMAIL_POOL_INDEX",
        "EMAIL_BROWSER_CACHE_DIR",
        "CAPMONSTER_PROXY_MODE",
        "PROXY_DIRECT_HOSTS",
        "STATIC_CACHE_DIR",
        "USER_AGENT",
    ):
        source = f"V6_{suffix}"
        target = f"V5_{suffix}"
        if source in os.environ:
            os.environ[target] = os.environ[source]


def _setup_v6_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [HTTP-V6] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    for name in ("urllib3", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_parser_v6():
    parser = _V5_BUILD_PARSER()
    parser.description = (
        "V5 persistent HTTP registration logic with V6 supplied-email input"
    )
    for action in parser._actions:
        if action.dest == "email_source":
            action.help = (
                "generated or one deterministic row from "
                "Email_registing_v6.txt"
            )
            break
    return parser


def _verify_registered_email_v6(
    credential: v6_email_pool.EmailCredential,
    account_password: str,
    **kwargs,
):
    """Feed the V6 source fields to the unchanged V5 mailbox verifier."""
    return _V5_VERIFY_REGISTERED_EMAIL(
        credential.to_v5(),
        account_password,
        **kwargs,
    )


def _install_v6_contract() -> None:
    # V5 remains byte-for-byte unchanged. Its runtime globals are replaced only
    # inside this V6 process, so registration, solver and verification logic are
    # exactly the V5 implementation while credential parsing uses V6 semantics.
    v5.DEFAULT_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT
    v5.build_parser = _build_parser_v6
    v5.EmailCredential = v6_email_pool.EmailCredential
    v5.select_email_credential = v6_email_pool.select_email_credential
    v5.verify_registered_email = _verify_registered_email_v6
    v5.setup_logging = _setup_v6_logging


def _selected_api_line() -> str:
    args = v5.build_parser().parse_args()
    if args.email_source != "pool":
        return ""
    return v6_email_pool.select_email_credential(
        args.email_pool_file, args.email_pool_index
    ).raw_line


def main() -> int:
    _map_v6_environment()
    _install_v6_contract()
    api_line = _selected_api_line()
    exit_code = v5.main()
    if exit_code == 0 and api_line:
        print(f"API：{api_line}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
