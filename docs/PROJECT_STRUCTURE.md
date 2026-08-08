# 项目结构与依赖边界

本项目采用“一个工作流入口 + 内部模块目录”的结构。这样既减少根目录散落的 Python 脚本，又不把一万多行不同职责的逻辑硬拼成一个难以维护的巨型文件。

## 活跃 GitHub Actions

`.github/workflows/` 目前保留：

- `register-ruyipage-v5.yml`：V5 注册。
- `register-ruyipage-v6.yml`：V6 注册。
- `email-verify-ruyipage-v3.yml`：邮箱验证。
- `register-funcaptcha-snapshots.yml`：HTTP 多轮 FunCaptcha 题图采集。
- `capture-images.yml`：题图采集兼容入口。
- `Clear_All_Workflow.yml`：清理 Actions 运行记录。

所有活跃 Python 工作流都调用根目录的 `workflow_runner.py`，再由函数分发到对应内部模块：

| 统一函数/命令 | 原功能 |
| --- | --- |
| `run_register_v5()` / `register-v5` | V5 注册 |
| `run_register_v6()` / `register-v6` | V6 注册 |
| `run_email_verify()` / `email-verify` | 邮箱验证 |
| `run_funcaptcha_snapshots()` / `funcaptcha-snapshots` | 多轮题图采集 |
| `run_capture_images()` / `capture-images` | 题图采集 |
| `run_proxy_pool()` / `proxy-pool` | 代理预检与分配 |
| `run_register()`、`run_register_v3()`、`run_register_v4()` | 旧入口兼容调用 |

## 内部模块

`workflow_modules/` 中的文件是库模块，不是独立工作流入口。它们按原模块名保留，是为了避免改变内部 import、缓存目录、资源目录和类型引用：

- `register.py`：基础注册与身份生成。
- `register_ruyipage_v3.py`：本地 V11/RuyiPage 兼容求解。
- `register_ruyipage_v4.py`：HTTP 持久化注册与浏览器桥接。
- `register_ruyipage_v5.py`、`register_ruyipage_v6.py`：V5/V6 注册逻辑。
- `battle_protocol_flow_v4.py`：HTTP 注册协议流。
- `v5_*`、`v6_*`：邮箱、代理、资源策略和求解器模块。
- `captcha_image_collector.py`、`proxy_traffic_meter.py`、`isolated_proxy_adapter.py`：采集、流量和代理基础设施。

统一加载器将这些模块的 `__file__` 映射到原项目根路径，因此代码中原有的 `Path(__file__).resolve().parent` 仍然得到项目根目录，不需要修改注册逻辑。

## 必须保持原路径的外部目录

- `rank_v11/`：V5/V6 本地模型服务和模型资产。
- `yolo/`：骰子模型和推理组件。
- `ruyipage_manual_register/`：手动浏览器工具；已在启动时接入统一加载器。
- `v5_desktop_ui/`：本地桌面 UI。
- `requirements-ruyipage-v4.txt`、`requirements-ruyipage-v5.txt`：活跃工作流依赖。

## 历史归档

- `backup/legacy_registration_workflows_20260808/`：五套退出 Actions 的旧工作流、入口快照和依赖快照。
- `backup/legacy_tools_20260808/`：未被现行流程引用的双浏览器抓图工具。

归档内容不能被活跃工作流直接 import。需要恢复时，应先建立分支，并按归档 README 的步骤恢复。

## 整理原则

1. 根目录禁止新增独立工作流 Python；新功能必须进入 `workflow_modules/` 并在 `workflow_runner.py` 登记函数。
2. 新增入口前先检查 `.github/workflows/`、Python import、测试和文档引用。
3. 不移动模型、账号输入、缓存或运行数据，不删除未明确指定的文件。
4. 大规模移动前先做仓库外完整备份，并校验文件数、字节数和 SHA-256。
5. 整理后执行 `python workflow_runner.py check`、YAML 解析、`python -m pytest -q` 和 `git diff --check`。
