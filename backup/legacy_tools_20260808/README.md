# 历史工具归档

`funcaptcha_two_browser_snapshots.py` 未被现行 `.github/workflows/` 或活跃模块引用，因此从根目录移到这里。

它仍保留原始源码，但依赖旧的顶层模块导入方式。若要重新启用，应先恢复到独立分支并适配 `workflow_runner.py` 的统一加载器，不能直接加入现行工作流。
