# 工作流内部模块

这里保存现行 GitHub Actions 的内部实现模块。项目根目录只保留统一入口：

`workflow_runner.py`

不要直接新增新的根目录 Python 脚本。新增工作流能力时：

1. 优先扩展现有内部模块；确需新模块时放入本目录。
2. 在 `workflow_runner.py` 中登记模块并增加一个清晰的调用函数。
3. GitHub Actions 只能调用 `workflow_runner.py`，不要重新直接调用内部模块文件。
4. 修改后运行 `python workflow_runner.py check` 和 `python -m pytest -q`。

统一加载器会把模块的 `__file__` 映射到项目根目录原路径，因此原有资源、缓存和输出目录逻辑保持不变。
