# -*- coding: utf-8 -*-
"""项目唯一工作流入口。

所有 GitHub Actions 通过本文件中的不同函数调用对应功能。具体实现统一
收纳在 ``workflow_modules/``，加载时仍把 ``__file__`` 映射为原项目根目录，
从而保持旧代码的资源目录、缓存目录和输出目录计算方式不变。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
MODULE_ROOT = PROJECT_ROOT / "workflow_modules"

MODULE_NAMES = (
    "battle_protocol_flow_v4",
    "captcha_image_collector",
    "email_verify_ruyipage_v3",
    "funcaptcha_http_snapshots",
    "isolated_proxy_adapter",
    "proxy_traffic_meter",
    "register",
    "register_capture_images",
    "register_ruyipage_v3",
    "register_ruyipage_v4",
    "register_ruyipage_v5",
    "register_ruyipage_v6",
    "v4_browser_resource_optimizer",
    "v5_cloak_adapter",
    "v5_email_pool",
    "v5_email_verifier",
    "v5_proxy_pool",
    "v5_resource_policy",
    "v5_yescaptcha_solver",
    "v6_email_pool",
    "v6_email_verifier",
)


class _WorkflowModuleLoader(importlib.abc.Loader):
    """从归类目录加载模块，同时保留原根目录虚拟文件路径。"""

    def __init__(self, fullname: str, source_path: Path) -> None:
        self.fullname = fullname
        self.source_path = source_path
        self.virtual_path = PROJECT_ROOT / f"{fullname}.py"

    def create_module(self, spec):  # noqa: ANN001
        return None

    def exec_module(self, module: ModuleType) -> None:
        source = self.source_path.read_text(encoding="utf-8-sig")
        module.__file__ = str(self.virtual_path)
        module.__loader__ = self
        module.__package__ = ""
        code = compile(source, str(self.virtual_path), "exec")
        exec(code, module.__dict__)


class _WorkflowModuleFinder(importlib.abc.MetaPathFinder):
    """只接管 ``MODULE_NAMES`` 中的项目内部顶层模块。"""

    marker = "new-github-workflow-module-finder-v1"

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        if fullname not in MODULE_NAMES:
            return None
        source_path = MODULE_ROOT / f"{fullname}.py"
        if not source_path.is_file():
            raise ModuleNotFoundError(f"统一模块缺失：{source_path}")
        loader = _WorkflowModuleLoader(fullname, source_path)
        return importlib.util.spec_from_loader(
            fullname,
            loader,
            origin=str(loader.virtual_path),
        )


def install_module_loader() -> None:
    """安装一次内部模块加载器；测试和内联 Python 片段也可调用。"""

    if any(getattr(item, "marker", "") == _WorkflowModuleFinder.marker for item in sys.meta_path):
        return
    sys.meta_path.insert(0, _WorkflowModuleFinder())
    importlib.invalidate_caches()


def load_module(module_name: str) -> ModuleType:
    """加载一个已归类的内部模块。"""

    if module_name not in MODULE_NAMES:
        raise ValueError(f"未知内部模块：{module_name}")
    install_module_loader()
    return importlib.import_module(module_name)


def _exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(not value)
    if isinstance(value, int):
        return value
    return 0


def run_module_cli(module_name: str, argv: Sequence[str] | None = None) -> int:
    """按旧脚本的 ``main()`` 约定执行模块，同时原样传递 CLI 参数。"""

    module = load_module(module_name)
    entry = getattr(module, "main", None)
    if not callable(entry):
        raise RuntimeError(f"内部模块没有 main()：{module_name}")

    old_argv = sys.argv[:]
    sys.argv = [str(PROJECT_ROOT / f"{module_name}.py"), *(list(argv or ()))]
    try:
        try:
            return _exit_code(entry())
        except SystemExit as exc:
            return _exit_code(exc.code)
    finally:
        sys.argv = old_argv


def run_capture_images(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register_capture_images", argv)


def run_register(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register", argv)


def run_register_v3(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register_ruyipage_v3", argv)


def run_register_v4(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register_ruyipage_v4", argv)


def run_email_verify(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("email_verify_ruyipage_v3", argv)


def run_funcaptcha_snapshots(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("funcaptcha_http_snapshots", argv)


def run_proxy_pool(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("v5_proxy_pool", argv)


def run_register_v5(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register_ruyipage_v5", argv)


def run_register_v6(argv: Sequence[str] | None = None) -> int:
    return run_module_cli("register_ruyipage_v6", argv)


def check_modules(module_names: Iterable[str] | None = None) -> int:
    """不触发网络或浏览器，只编译检查统一模块。"""

    selected = tuple(module_names or MODULE_NAMES)
    unknown = sorted(set(selected) - set(MODULE_NAMES))
    if unknown:
        raise ValueError(f"未知内部模块：{', '.join(unknown)}")
    for module_name in selected:
        source_path = MODULE_ROOT / f"{module_name}.py"
        source = source_path.read_text(encoding="utf-8-sig")
        compile(source, str(PROJECT_ROOT / f"{module_name}.py"), "exec")
    print(f"统一模块检查通过：{len(selected)} 个")
    return 0


COMMANDS: dict[str, Callable[[Sequence[str] | None], int]] = {
    "capture-images": run_capture_images,
    "email-verify": run_email_verify,
    "funcaptcha-snapshots": run_funcaptcha_snapshots,
    "proxy-pool": run_proxy_pool,
    "register": run_register,
    "register-v3": run_register_v3,
    "register-v4": run_register_v4,
    "register-v5": run_register_v5,
    "register-v6": run_register_v6,
}


def _print_help() -> None:
    print("用法：python workflow_runner.py <功能> [原功能参数...]")
    print("功能：")
    for command in COMMANDS:
        print(f"  {command}")
    print("  check [模块名 ...]")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        _print_help()
        return 0
    command, *remaining = args
    if command == "check":
        return check_modules(remaining or None)
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"未知功能：{command}", file=sys.stderr)
        _print_help()
        return 2
    return handler(remaining)


install_module_loader()


if __name__ == "__main__":
    raise SystemExit(main())
