"""Discrimination twins：normalized discriminator signature + 交叉验证（ADR-0002 的纯原语）。

把「不同操作、断言标记无公共子串」的对偶用例（R47 login/下班 ⇆ R49 login_negative/账号密码登录）
按**归一化判据签名**配成 twin，并用已录的 negative-control 证据交叉验证它们互为对偶。词汇见
`apps/<app>/discrimination/dimensions.yaml`：一个 *dimension* = 一对对偶 *pole*，每 pole 绑 marker
集 + operation-witness stage。

本模块是平台层纯逻辑（无 IO 副作用、无 VM、无 argparse）：实验 CLI
`tools/phase2/experiments/twins.py` 与派生 Exception 面（`exceptions.py`）都复用它。**配对 + 交叉
验证是纯派生**——不持久化图、不自动起草、不自动 quarantine（ADR-0002 仍把那些 deferred）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from autophone.execution.engine.recipe import load_recipe


# ── 词汇：dimension = 一对对偶 pole，每 pole 绑 markers + witness stages ──────────────
@dataclass(frozen=True)
class Pole:
    name: str
    markers: tuple[str, ...]
    stages: tuple[str, ...]


@dataclass(frozen=True)
class Dimension:
    name: str
    poles: tuple[Pole, Pole]   # 恰两极（对偶）

    def opposite(self, pole_name: str) -> str:
        a, b = self.poles
        return b.name if pole_name == a.name else a.name


@dataclass(frozen=True)
class Vocabulary:
    dimensions: tuple[Dimension, ...]

    def classify_marker(self, marker: str) -> tuple[str, str] | None:
        """marker → (dimension, pole)；不在词汇里 → None。"""
        for d in self.dimensions:
            for p in d.poles:
                if marker in p.markers:
                    return (d.name, p.name)
        return None

    def classify_stage(self, stage: str | None) -> tuple[str, str] | None:
        """operation witness stage → (dimension, pole)；未知 → None。"""
        if not stage:
            return None
        for d in self.dimensions:
            for p in d.poles:
                if stage in p.stages:
                    return (d.name, p.name)
        return None


def load_vocabulary(path: Path) -> Vocabulary:
    """读词汇并跑静态不变量闸（fail-closed）：每 dimension 恰 2 pole；同 dimension 内 markers/stages
    两极不相交（一个标记/态不能同时指两极，否则归一化无区分力，twin 配对会失真）。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    dims: list[Dimension] = []
    for d in doc.get("dimensions") or []:
        poles_raw = d.get("poles") or []
        if len(poles_raw) != 2:
            raise ValueError(f"dimension {d.get('name')!r} 必须恰 2 个对偶 pole，得到 {len(poles_raw)}")
        poles = tuple(
            Pole(name=p["name"], markers=tuple(p.get("markers") or ()), stages=tuple(p.get("stages") or ()))
            for p in poles_raw
        )
        m0, m1 = set(poles[0].markers), set(poles[1].markers)
        if m0 & m1:
            raise ValueError(f"dimension {d['name']!r} 两极 markers 相交 {sorted(m0 & m1)}——无区分力，fail-closed")
        s0, s1 = set(poles[0].stages), set(poles[1].stages)
        if s0 & s1:
            raise ValueError(f"dimension {d['name']!r} 两极 stages 相交 {sorted(s0 & s1)}——fail-closed")
        dims.append(Dimension(name=d["name"], poles=poles))  # type: ignore[arg-type]
    if not dims:
        raise ValueError(f"{path} 未声明任何 dimension")
    return Vocabulary(dimensions=tuple(dims))


# ── 归一化判据签名 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Signature:
    case_id: str
    dimension: str
    pole: str
    markers: tuple[str, ...]

    @property
    def key(self) -> str:
        """归一化签名键：判据指向的 (dimension, pole)。twin 配对只看这个，不看 recipe 形状。"""
        return f"{self.dimension}@{self.pole}"

    def as_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "dimension": self.dimension, "pole": self.pole,
                "markers": list(self.markers), "key": self.key}


def recipe_discriminator_markers(steps: list[dict[str, Any]]) -> list[str]:
    """从 recipe 抠出判据标记：所有 assert_present 步的 queries（用例「expect」落到判定面的那些 OCR 标记）。"""
    markers: list[str] = []
    for s in steps:
        if isinstance(s, dict) and s.get("action") == "assert_present":
            for q in s.get("queries") or []:
                if q not in markers:
                    markers.append(str(q))
    return markers


def discriminator_signature(case_id: str, markers: list[str], vocab: Vocabulary) -> Signature:
    """把一条用例的判据标记归一化成签名。fail-closed：标记必须全部落到**同一** (dimension, pole)——
    有标记不在词汇里、或分裂到不同极 → 抛（签名不该跨极含混，否则 twin 配对失真）。"""
    if not markers:
        raise ValueError(f"{case_id}: 无判据标记（无 assert_present），无法归一化签名")
    classified = [(m, vocab.classify_marker(m)) for m in markers]
    unknown = [m for m, c in classified if c is None]
    if unknown:
        raise ValueError(f"{case_id}: 判据标记 {unknown} 不在区分力词汇里——补 dimensions.yaml 或修 recipe，fail-closed")
    poles = {c for _, c in classified}
    if len(poles) != 1:
        raise ValueError(f"{case_id}: 判据标记分裂到多个极 {sorted(poles)}——签名含混，fail-closed")
    (dim, pole), = poles
    return Signature(case_id=case_id, dimension=dim, pole=pole, markers=tuple(markers))


def build_signatures(case_ids: list[str], recipe_dir: Path, vocab: Vocabulary) -> list[Signature]:
    """按显式 case 列表算签名（实验用，fail-closed：每条都必须能归一化）。"""
    return [
        discriminator_signature(cid, recipe_discriminator_markers(load_recipe(recipe_dir / f"{cid.lower()}.yaml")), vocab)
        for cid in case_ids
    ]


def signatures_for_active_cases(
    cases: list[dict[str, Any]], recipe_dir: Path, vocab: Vocabulary,
) -> list[Signature]:
    """库级**fail-soft** 算签名：遍历用例，能归一化的收，归一化不了的（无 recipe / 无 assert /
    标记不在任何 dimension）**跳过**——它们只是不在任何已知判据轴上，不是错误（与实验的 fail-closed
    相反：派生面要扫全库，绝大多数用例本就不在 auth_session 这类轴上）。"""
    out: list[Signature] = []
    for case in cases:
        cid = str(case.get("id") or "")
        recipe_path = recipe_dir / f"{cid.lower()}.yaml"
        if not cid or not recipe_path.exists():
            continue
        markers = recipe_discriminator_markers(load_recipe(recipe_path))
        if not markers or any(vocab.classify_marker(m) is None for m in markers):
            continue  # 不在任何已知 dimension → 非 twin 参与者，跳过
        try:
            out.append(discriminator_signature(cid, markers, vocab))
        except ValueError:
            continue  # 跨极含混等 → 跳过（库级不 fail-closed）
    return out


# ── twin 配对（按签名，不按形状） ────────────────────────────────────────────────
@dataclass(frozen=True)
class TwinPair:
    a: Signature
    b: Signature

    def as_dict(self) -> dict[str, Any]:
        return {"a": self.a.as_dict(), "b": self.b.as_dict(), "dimension": self.a.dimension,
                "poles": [self.a.pole, self.b.pole]}


def find_twins(signatures: list[Signature]) -> list[TwinPair]:
    """同 dimension、对偶 pole 的任意两条 = 一对 discrimination twin。稳定去重（a.case_id < b.case_id）。"""
    pairs: list[TwinPair] = []
    for i, sa in enumerate(signatures):
        for sb in signatures[i + 1:]:
            if sa.dimension == sb.dimension and sa.pole != sb.pole:
                a, b = sorted((sa, sb), key=lambda s: s.case_id)
                pairs.append(TwinPair(a=a, b=b))
    return pairs


# ── 交叉验证：用 negctl 已录证据让一对 twin 互相证明 ─────────────────────────────
@dataclass
class CaseEvidence:
    """从 negctl artifact 聚合出一条用例的判据证据（可能多 probe：ablate + flip）。"""
    case_id: str
    target_green: bool = False
    has_teeth: bool = False
    flip_crossings: list[tuple[str | None, tuple[str, str] | None]] = field(default_factory=list)
    # 每条 flip probe：(witness_stage, 归一化到的 (dimension,pole) | None)


def evidence_from_probes(probes: list[dict[str, Any]], vocab: Vocabulary) -> dict[str, CaseEvidence]:
    """把 negctl probes 按 case 聚合：是否拿到绿基线、是否任一扰动下翻红（有牙）、flip 落到的归一化极。"""
    out: dict[str, CaseEvidence] = {}
    for pr in probes:
        cid = str(pr.get("case_id") or "")
        ev = out.setdefault(cid, CaseEvidence(case_id=cid))
        if pr.get("target_green") is True:
            ev.target_green = True
        if (pr.get("target_green") is True and pr.get("discriminator_flipped") is True
                and pr.get("teeth_at") in {"recipe", "operation"}):
            ev.has_teeth = True
        if (pr.get("perturbation") or {}).get("kind") == "flip":
            stage = pr.get("witness_stage")
            ev.flip_crossings.append((stage, vocab.classify_stage(stage)))
    return out


def cross_validate(pair: TwinPair, evidence: dict[str, CaseEvidence], vocab: Vocabulary) -> dict[str, Any]:
    """用 negctl 证据交叉验证一对 twin（ADR-0002）。判定：

      inconclusive — 任一 twin 没绿基线或没牙（拿不到可交叉的判据）。
      disagree     — 有 flip 越界证据但落到**错的极**（不是对方那一极 / 跨了维度 / 未知 stage）：
                     声明的对偶链路被实测推翻 → **硬阻塞这对 pair**（ADR-0002）。
      agree        — 两 twin 各有绿基线 + 牙，且至少一条 flip 越界落到**对方那一极**（对偶被独立印证）。
      agree_no_crossing — 两 twin 有绿 + 牙但没有 flip 越界证据（仅 ablate 取牙），对偶未被独立印证，弱一档。
    """
    ea, eb = evidence.get(pair.a.case_id), evidence.get(pair.b.case_id)
    reasons: list[str] = []
    if ea is None or eb is None or not ea.target_green or not eb.target_green:
        return {"state": "inconclusive", "reason": "某一 twin 缺绿基线（negctl 未覆盖或 target 未跑绿）",
                **pair.as_dict()}
    if not ea.has_teeth or not eb.has_teeth:
        return {"state": "inconclusive", "reason": "某一 twin 的判据在 negctl 下未证有牙", **pair.as_dict()}

    crossed = False
    for sig, partner in ((pair.a, pair.b), (pair.b, pair.a)):
        ev = evidence[sig.case_id]
        for stage, classified in ev.flip_crossings:
            if not stage:
                continue  # 该 flip probe 没录到 witness stage（旧 harness 缺口）= 无越界证据，不是矛盾
            if classified is None:
                return {"state": "disagree",
                        "reason": f"{sig.case_id} 的 flip 落到未知 stage={stage!r}（不在词汇任何极）——对偶链路无法印证",
                        **pair.as_dict()}
            cdim, cpole = classified
            if cdim != sig.dimension:
                return {"state": "disagree",
                        "reason": f"{sig.case_id} 的 flip 越到别的维度 {cdim}@{cpole}（应留在 {sig.dimension}）",
                        **pair.as_dict()}
            if cpole == sig.pole:
                return {"state": "disagree",
                        "reason": f"{sig.case_id} 的 flip 没越界、仍落在自身极 {sig.pole}——它与 {partner.case_id} 不是对偶",
                        **pair.as_dict()}
            # cpole == 对方那一极（== partner.pole，因恰两极）
            crossed = True
            reasons.append(f"{sig.case_id} flip→stage={stage!r} 归一化落到 {cdim}@{cpole}（={partner.case_id} 的极）")
    state = "agree" if crossed else "agree_no_crossing"
    if not crossed:
        reasons.append("两 twin 均经 ablate 取牙、无 flip 越界证据——对偶成立但未被输入翻转独立印证")
    return {"state": state, "reason": "；".join(reasons), **pair.as_dict()}
