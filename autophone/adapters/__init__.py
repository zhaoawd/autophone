"""入口层适配器（目标架构 §11.1、§14）。

这里只放「协议转换」：认证、参数校验、调用一个 application 或领域 Interface、结果转换。
`http/` 是 `autophone/adapters/http/app.py` 逐组切出的 router，`cli/` 是 `autophone/cli.py`
逐族切出的子命令模块；拆解步骤见 docs/platform-refactoring-console-split.md。

本目录在 import ratchet 里按 R3 计（`tests/test_import_ratchet.py` 的 ENTRYPOINT_DIRS）：
退出 R1，但对 `autophone.platform` 的每条 import 都逐条登记在
`tests/import_ratchet_adapters_budget.txt`，只减不增。
"""
