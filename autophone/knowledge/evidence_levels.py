"""证据等级阶梯（由弱到强），Coverage Contract 与 cumulative verifier 共用的词汇表。

原定义在 `autophone/platform/evidence_verifier.py`。BL-012 把 `knowledge_coverage.py`
归位 `autophone/knowledge/coverage.py` 时，这份常量随消费方先搬到目标包；BL-014 把
`evidence_verifier.py` 本身也归位 `autophone/knowledge/evidence_verifier.py`，两者
现在同包。取值与顺序未改。
"""
from __future__ import annotations

LEVELS = (
    "static_hint",
    "observed",
    "repeated",
    "controlled_contrast",
    "independent_readback",
)
