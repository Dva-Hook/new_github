# 旧版注册工作流归档（2026-08-08）

这里保存从 `.github/workflows/` 移出的五套旧版注册工作流及对应入口脚本快照。归档 YAML 不会被 GitHub Actions 自动加载。

## 已归档工作流

| 原工作流 | 入口脚本 | 处理方式 |
| --- | --- | --- |
| `register.yml` | `register.py` | 工作流移入归档；脚本因仍被抓图和 V3/V4 复用，保留操作前快照，当前实现已归类到 `workflow_modules/` |
| `register-ruyipage.yml` | `register_ruyipage.py` | 工作流与专用脚本均移入归档 |
| `register-ruyipage-v2.yml` | `register_ruyipage_v2.py` | 工作流与专用脚本均移入归档 |
| `register-ruyipage-v3.yml` | `register_ruyipage_v3.py` | 工作流移入归档；脚本仍被 V4 导入，保留操作前快照，当前实现已归类到 `workflow_modules/` |
| `register-ruyipage-v4.yml` | `register_ruyipage_v4.py` | 工作流移入归档；脚本仍被 V5、邮箱验证及 HTTP 抓图导入，保留操作前快照，当前实现已归类到 `workflow_modules/` |

## 目录说明

- `workflows/`：原始工作流 YAML。
- `scripts/`：对应入口脚本。这里的共享脚本是归档时快照；当前正式实现位于项目根目录的 `workflow_modules/`，通过 `workflow_runner.py` 调用。
- `requirements/`：相关依赖文件。仍由活跃流程使用的文件在根目录保留，并在这里复制快照。
- `MANIFEST.json`：每个归档文件的来源、移动/复制状态和 SHA-256。

`rank_v11/`、`yolo/`、`ruyipage_manual_register/` 等共享依赖仍保留在项目正式位置；工作流 Python 实现统一位于 `workflow_modules/`，避免影响 V5、V6、邮箱验证和题图采集。

## 恢复方法

1. 将所需 YAML 从 `workflows/` 复制回 `.github/workflows/`。
2. 对旧 RuyiPage 和 RuyiPage V2，将对应脚本从 `scripts/` 复制回项目根目录；现行版本不要覆盖 `workflow_modules/`。
3. 将该工作流引用但根目录已经不存在的依赖文件从 `requirements/` 复制回根目录。
4. 现行版本默认使用 `workflow_runner.py` 和 `workflow_modules/`。若必须完全复现归档时版本，应先建立新分支，再按 `scripts/` 快照恢复，避免影响 V5/V6。
5. 恢复后运行 YAML 解析、Python 编译和测试，再提交变更。

归档中的工作流继续保留原来的根目录相对路径，因此不能直接在此目录中作为 GitHub Actions 运行。
