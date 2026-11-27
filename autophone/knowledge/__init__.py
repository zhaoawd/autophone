"""App 知识：Claim 存储、Inventory、Catalog、Coverage Contract、差异与实验。

由 `autophone/platform/` 归位而来（BL-012 阶段 B domain 堆）：`knowledge_<x>.py` 去掉
`knowledge_` 前缀成为本包的 `<x>.py`。`evidence_levels.py` 是 BL-012 从
`platform/evidence_verifier.py` 析出的证据等级阶梯常量（Coverage Contract 与
cumulative verifier 共用的词汇表）；BL-014（归位表 §三 #17 / #29）把
`evidence_verifier.py` 与 `implementation_binding.py` 也归位本包。

`query.py` 随 BL-013 的 `application/run_index.py` 同批归位，读取该模块的 Run 查询投影。

包级不导出符号：调用方按子模块 import。
"""
from __future__ import annotations
