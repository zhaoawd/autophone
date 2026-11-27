"""Package Store：IPA 包仓库的 Build 身份、Version Slot、迁移与不可变构件存储。

由 `autophone/platform/` 归位而来（BL-012 阶段 B domain 堆）：
`packages.py` → `store.py`、`package_model.py` → `model.py`、
`package_migration.py` → `migration.py`、`artifact_store.py` 原名保留。

包级不导出符号：调用方按子模块 import，避免包级 import 拉起整棵依赖树。
"""
from __future__ import annotations
