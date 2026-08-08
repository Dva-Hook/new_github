# -*- coding: utf-8 -*-
r"""Adapter for ``D:\Project\isolated-proxy-browser``.

The source project remains the implementation source of truth. This adapter
loads its latency-picker module, starts one isolated Mihomo core, lets the user
choose a node, and exposes the resulting local mixed proxy to both browsers in
the FunCaptcha experiment.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Optional


DEFAULT_PROJECT = Path(r"D:\Project\isolated-proxy-browser")
DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"


class IsolatedProxyAdapterError(RuntimeError):
    pass


def load_latency_picker_module(project_dir: Path) -> ModuleType:
    project_dir = project_dir.expanduser().resolve()
    script = project_dir / "scripts" / "cloak_latency_proxy_browser.py"
    if not script.is_file():
        raise IsolatedProxyAdapterError(f"找不到 isolated-proxy-browser 脚本：{script}")

    module_name = f"_isolated_proxy_picker_{abs(hash(str(script)))}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise IsolatedProxyAdapterError(f"无法加载模块：{script}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules while the
    # module is executing, so register it before exec_module().
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    required = (
        "load_yaml",
        "extract_nodes",
        "build_core_config",
        "dump_yaml",
        "resolve_core",
        "launch_core",
        "wait_port",
        "test_all_delays",
        "print_delay_table",
        "choose_node",
        "set_group_selection",
        "free_port",
        "terminate_process",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise IsolatedProxyAdapterError(
            f"isolated-proxy-browser 接口不完整，缺少：{', '.join(missing)}"
        )
    return module


class IsolatedProxyRoute:
    """Own one Mihomo core and one selected route for both browser processes."""

    def __init__(
        self,
        *,
        project_dir: Path = DEFAULT_PROJECT,
        config_path: Optional[Path] = None,
        core_path: Optional[Path] = None,
        test_url: str = DEFAULT_TEST_URL,
        timeout_ms: int = 6000,
        workers: int = 8,
        node_index: Optional[int] = None,
        require_explicit_choice: bool = False,
        keep_temp: bool = False,
        evidence_dir: Optional[Path] = None,
    ) -> None:
        self.project_dir = project_dir.expanduser().resolve()
        self.config_path = (
            config_path.expanduser().resolve()
            if config_path
            else self.project_dir / "config" / "proxy-config.yaml"
        )
        self.core_path = (
            core_path.expanduser().resolve()
            if core_path
            else self.project_dir / "bin" / "mihomo.exe"
        )
        self.test_url = test_url
        self.timeout_ms = max(500, int(timeout_ms))
        self.workers = max(1, int(workers))
        self.node_index = node_index
        self.require_explicit_choice = bool(require_explicit_choice)
        self.keep_temp = keep_temp
        self.evidence_dir = evidence_dir.expanduser().resolve() if evidence_dir else None

        self.module: Optional[ModuleType] = None
        self.process = None
        self.temp_root: Optional[Path] = None
        self.log_path: Optional[Path] = None
        self.proxy_url: Optional[str] = None
        self.route_info: dict[str, Any] = {}

    def _choose_by_index(self, nodes: list[Any], index: int) -> Any:
        by_id = {int(n.original_index): n for n in nodes}
        if index not in by_id:
            raise IsolatedProxyAdapterError(
                f"节点编号不存在：{index}；有效范围包含 {sorted(by_id)[:3]}...{sorted(by_id)[-3:]}"
            )
        return by_id[index]

    def _choose_by_prompt_required(self, nodes: list[Any]) -> Any:
        """Require explicit numeric node choice instead of Enter=fastest."""

        by_id = {int(n.original_index): n for n in nodes}
        ranked = sorted(
            nodes,
            key=lambda n: (
                0 if getattr(n, "ok", False) else 1,
                getattr(n, "delay_ms", None) if getattr(n, "delay_ms", None) is not None else 10**9,
                int(n.original_index),
            ),
        )
        fastest = ranked[0] if ranked else nodes[0]
        print(
            f"\n请手动输入本次使用的节点编号；直接回车不会自动选择。\n"
            f"当前测速最快候选：#{fastest.original_index} {fastest.original_name} "
            f"({fastest.delay_ms if getattr(fastest, 'ok', False) else '测速失败'}ms)"
        )
        while True:
            raw = input("节点编号：").strip()
            if not raw:
                print("请明确输入节点编号，例如 55。")
                continue
            try:
                idx = int(raw)
            except ValueError:
                print("请输入数字编号。")
                continue
            if idx not in by_id:
                print(f"编号不存在：{idx}")
                continue
            chosen = by_id[idx]
            if not getattr(chosen, "ok", False):
                confirm = input(f"该节点测速失败，仍然使用 #{idx}？输入 y 确认：").strip().lower()
                if confirm != "y":
                    continue
            return chosen
    def start(self) -> dict[str, Any]:
        if self.process is not None:
            return dict(self.route_info)

        module = load_latency_picker_module(self.project_dir)
        self.module = module
        try:
            original = module.load_yaml(self.config_path)
            nodes = module.extract_nodes(original)
            mixed_port = int(module.free_port())
            controller_port = int(module.free_port())
            while controller_port == mixed_port:
                controller_port = int(module.free_port())

            secret = secrets.token_urlsafe(18)
            controller = f"http://127.0.0.1:{controller_port}"
            proxy_url = f"http://127.0.0.1:{mixed_port}"
            temp_root = Path(tempfile.mkdtemp(prefix="funcaptcha-isolated-route-"))
            self.temp_root = temp_root
            core_home = temp_root / "core-home"
            core_home.mkdir(parents=True, exist_ok=True)
            cfg_path = temp_root / "config.yaml"
            log_path = temp_root / "core.log"
            self.log_path = log_path

            core_cfg = module.build_core_config(
                original,
                nodes,
                mixed_port,
                controller_port,
                secret,
            )
            module.dump_yaml(cfg_path, core_cfg)
            core = module.resolve_core(str(self.core_path))

            print("\n=== isolated-proxy-browser 路线模式 ===")
            print(f"配置文件：{self.config_path}")
            print(f"Mihomo core：{core}")
            print(f"节点数量：{len(nodes)}")
            print(f"共享本地代理：{proxy_url}")
            print("该代理只传给两个 CloakBrowser，不修改 Windows 系统代理。")

            self.process = module.launch_core(core, core_home, cfg_path, log_path)
            module.wait_port("127.0.0.1", mixed_port, self.process, log_path, timeout=25.0)
            module.wait_port("127.0.0.1", controller_port, self.process, log_path, timeout=15.0)
            print("Mihomo core 已启动，开始节点测速。")

            if self.node_index is None:
                module.test_all_delays(
                    controller,
                    secret,
                    nodes,
                    self.test_url,
                    self.timeout_ms,
                    self.workers,
                )
                module.print_delay_table(nodes)
                if self.require_explicit_choice:
                    chosen = self._choose_by_prompt_required(nodes)
                else:
                    chosen = module.choose_node(nodes)
            else:
                chosen = self._choose_by_index(nodes, int(self.node_index))
                # Keep command-line runs deterministic and quick, but still
                # test the selected route once when the source module supports it.
                if hasattr(module, "test_one_delay"):
                    module.test_one_delay(
                        controller,
                        secret,
                        chosen,
                        self.test_url,
                        self.timeout_ms,
                    )

            module.set_group_selection(controller, secret, chosen)
            self.proxy_url = proxy_url
            self.route_info = {
                "mode": 2,
                "modeName": "isolated-proxy-browser",
                "proxyURL": proxy_url,
                "projectDir": str(self.project_dir),
                "configPath": str(self.config_path),
                "corePath": str(Path(core).resolve()),
                "testURL": self.test_url,
                "mixedPort": mixed_port,
                "controllerPort": controller_port,
                "selectedNode": {
                    "index": int(chosen.original_index),
                    "name": str(chosen.original_name),
                    "type": str(chosen.proxy.get("type", "")),
                    "server": str(chosen.proxy.get("server", "")),
                    "port": chosen.proxy.get("port"),
                    "delayMs": chosen.delay_ms,
                    "delayError": chosen.error,
                },
                "tempRoot": str(temp_root) if self.keep_temp else None,
            }
            print(
                f"\n已选择节点 #{chosen.original_index}: {chosen.original_name} "
                f"({chosen.delay_ms if chosen.ok else '测速失败'}ms)"
            )
            print(f"原注册浏览器和手动求解浏览器将共同使用：{proxy_url}\n")
            return dict(self.route_info)
        except Exception as exc:
            self.stop()
            if isinstance(exc, IsolatedProxyAdapterError):
                raise
            raise IsolatedProxyAdapterError(f"隔离代理路线启动失败：{type(exc).__name__}: {exc}") from exc

    def stop(self) -> None:
        if self.module is not None and self.process is not None:
            try:
                self.module.terminate_process(self.process)
            except Exception:
                pass
        self.process = None

        if self.log_path and self.log_path.exists() and self.evidence_dir:
            try:
                self.evidence_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.log_path, self.evidence_dir / "mihomo_core.log")
            except Exception:
                pass

        if self.temp_root and self.temp_root.exists():
            if self.keep_temp:
                print(f"已保留隔离代理临时目录：{self.temp_root}")
            else:
                try:
                    shutil.rmtree(self.temp_root, ignore_errors=True)
                except Exception:
                    pass
        self.temp_root = None

    def __enter__(self) -> "IsolatedProxyRoute":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


