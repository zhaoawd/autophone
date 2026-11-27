"""录制与回放：run 期录屏、浏览器录制面与批次回放存储。

由 `autophone/platform/` 归位而来（BL-012 阶段 B domain 堆）：`record_web.py` → `web.py`，
`recording.py`、`replay_batch.py` 原名保留。

同簇另三个于 BL-014 执行段第二轮（2026-09-10）随行：`record_lift.py` → `lift.py`，
`recordings.py`、`replay_timing.py` 原名保留。它们在 BL-012 时留守，原因是
`record_lift.py` 模块级 import `platform.ai`、函数体内 import `platform.ai_draft`；
两者已按 BL-014 表 §4.1 裁定归位 `autophone/application/`，该原因不再成立。

包级不导出符号：调用方按子模块 import。
"""
from __future__ import annotations
