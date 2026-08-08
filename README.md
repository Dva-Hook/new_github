# Battle.net FunCaptcha Register Toolkit

当前项目以 HTTP 持久化注册、RuyiPage 浏览器求解、本地 V11/第三方求解器、邮箱验证和题图采集为主要功能。

## 统一入口

项目根目录只保留一个工作流 Python 入口：

`workflow_runner.py`

不同功能通过子命令调用，原有参数会原样传递：

```powershell
python workflow_runner.py register-v5 --help
python workflow_runner.py register-v6 --help
python workflow_runner.py email-verify --help
python workflow_runner.py funcaptcha-snapshots --help
python workflow_runner.py capture-images --help
python workflow_runner.py proxy-pool --help
python workflow_runner.py check
```

内部实现统一放在 `workflow_modules/`，不再把可复用逻辑散落在根目录。`workflow_runner.py` 的加载器保留原模块名和虚拟根路径，所以 V5/V6、邮箱验证、题图采集和缓存目录逻辑不变。

## 主要工作流

- `.github/workflows/register-ruyipage-v5.yml`：V5 注册。
- `.github/workflows/register-ruyipage-v6.yml`：V6 注册。
- `.github/workflows/email-verify-ruyipage-v3.yml`：邮箱验证。
- `.github/workflows/register-funcaptcha-snapshots.yml`：HTTP 多轮 FunCaptcha 题图采集。
- `.github/workflows/capture-images.yml`：题图采集兼容入口。
- `rank_v11/`：本地 V11 模型服务与推理组件。
- `v5_desktop_ui/`：V5 本地桌面批量运行界面。

## 旧版归档

CloakBrowser、RuyiPage、RuyiPage V2、V3、V4 五套旧 GitHub Actions 位于：

`backup/legacy_registration_workflows_20260808/`

未被现行工作流引用的双浏览器抓图工具位于：

`backup/legacy_tools_20260808/`

归档目录包含恢复说明和哈希清单。不要把账号、邮箱令牌、代理凭证、API Key、运行截图或日志提交到公开仓库。
