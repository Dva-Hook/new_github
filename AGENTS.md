# 项目文件与修改规范

本文件适用于整个仓库。目标是避免随意新增脚本、重复实现和无分类生成物。

## 固定目录职责

- `workflow_runner.py`：现行工作流唯一 Python 入口。
- `workflow_modules/`：可复用的注册、浏览器、协议、代理、邮箱和验证码内部模块。
- `.github/workflows/`：只放仍然启用的 GitHub Actions YAML。
- `tests/`：自动化测试；测试辅助代码也必须放这里。
- `docs/`：设计、依赖和维护文档。
- `rank_v11/`、`yolo/`：模型与推理代码，不与工作流控制代码混放。
- `backup/`：已停用版本和历史工具；归档文件不得被现行工作流依赖。
- `tools/experiments/`：确有必要时才创建的临时实验目录，不得在根目录散落实验脚本。

## 新增和修改规则

1. 禁止在项目根目录随意新增 `.py` 文件。新功能优先写入现有 `workflow_modules/` 模块。
2. 需要新的 CLI 功能时，在 `workflow_runner.py` 增加命名函数和子命令，不要新建独立入口脚本。
3. 禁止复制粘贴已有注册、代理、邮箱或验证码逻辑形成第二套实现；应抽成函数复用。
4. 运行日志、截图、账号输出、临时 JSON、模型中间结果必须写入已有运行输出目录，并确保被 `.gitignore` 忽略。
5. 文档只放 `docs/`；测试只放 `tests/`；停用代码只放带日期和说明的 `backup/` 子目录。
6. 不得把账号、邮箱令牌、代理凭证、API Key 或注册结果提交到仓库。

## 移动或删除前置检查

1. 先搜索 `.github/workflows/`、Python import、文档和测试中的全部引用。
2. 对共享模块只允许保留兼容路径或同步修改全部调用方，不得凭文件名判断“没用”。
3. 大规模整理前创建仓库外完整备份，并校验文件数量、总字节数和 SHA-256。
4. 不删除模型、账号输入、缓存或运行数据，除非用户明确指定具体目标。

## 必须执行的回归检查

```powershell
python workflow_runner.py check
python -m pytest -q
git diff --check
```

同时解析 `.github/workflows/` 中所有 YAML，并确认工作流只调用 `workflow_runner.py`、`rank_v11/` 或其他明确保留的模型入口。
