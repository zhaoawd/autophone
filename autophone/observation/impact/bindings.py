"""FieldBinding 登记表与置信度闸（影响图谱 §6.5 / §6.6）。

一条 binding 说的是：「后端这个 key，经这个接口的这个响应字段，作用于这个页面状态的
这个区域」。它是**人审资产**，不是运行期自己长出来的东西——所以新增恒 draft，转 active
要人审，置信度提升有硬闸：

    observed   —— 页面出现期间采到该接口。只能列候选。
    correlated —— 多次 run 中稳定共现。可 advisory 执行，报告里写「相关」。
    causal     —— 控制变量实验（negative control）或后端权威契约证明。可自动选中。
    stale      —— 新 build / 连续 no-match 使证据失效。禁止自动判断，回到待标定。

``correlated → causal`` 必须有一个控制变量实验或后端权威契约，没有就抬不上去。这条闸
是整张图谱可信度的支点：把相关性当因果，报告出来的「受影响页面」就是编的。

Prod 只负责观测正式配置的实际效果；需要主动改配置的 A/B 在 UAT、灰度或后端沙箱做。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from autophone.assets import store as asset_store
from autophone.assets.policy import assert_assets_writable
from autophone.contracts import validate_or_raise
from autophone.observation.impact import context as ctx_mod
from autophone.observation.impact.endpoints import (
    endpoint_key_from_binding,
    normalize_response_path,
    response_path_matches,
)

BINDINGS_REL = "impact/data-bindings.yaml"

OBSERVED = "observed"
CORRELATED = "correlated"
CAUSAL = "causal"
STALE = "stale"
CONFIDENCE_ORDER = (OBSERVED, CORRELATED, CAUSAL)

# correlated 至少要在这么多个不同 run 里稳定共现；一次同现只是 observed。
MIN_CORRELATED_RUNS = 2
# 连续这么多次 run 没再观测到 → 判 stale，回到待标定。
STALE_NO_MATCH_STREAK = 2

BINDINGS_SPEC = asset_store.AssetSpec(
    list_key="bindings",
    schema_version=1,
    editable_scalars=frozenset({"relation", "response_path", "sensitivity", "volatile", "build"}),
    id_prefix="FB",
)


class BindingError(ValueError):
    """binding 资产不合法，或置信度提升不满足证据要求。"""


@dataclass(frozen=True)
class BindingMatch:
    binding: dict[str, Any]
    match_level: str  # jsonpath | endpoint | data_key
    confidence: str  # 生效置信度（已考虑 build / no-match staleness）


def bindings_path(profile: Any) -> Path:
    """``apps/<app>/impact/data-bindings.yaml``。"""
    return Path(profile.profile_dir) / BINDINGS_REL


def empty_registry(app: str) -> dict[str, Any]:
    return {
        "meta": {
            "schema_version": 1,
            "app": str(app),
            "title": f"{app} 字段→页面区域绑定表",
            "total": 0,
            "next_seq": 1,
        },
        "bindings": [],
    }


def load(path: Path) -> dict[str, Any]:
    """读 + 校验。文件不存在 → 空登记表（零 binding 是合法状态，不是错误）。"""
    path = Path(path)
    if not path.exists():
        app = path.parent.parent.name
        return empty_registry(app)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise BindingError(f"binding 登记表格式非法：{path}")
    validate_or_raise("field-binding.v1.json", doc)
    return doc


def ensure_file(path: Path, app: str) -> Path:
    path = Path(path)
    if path.exists():
        return path
    assert_assets_writable("create field-binding asset", path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(empty_registry(app), allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


def list_bindings(
    doc: dict[str, Any],
    *,
    statuses: Sequence[str] | None = ("active",),
) -> list[dict[str, Any]]:
    items = [b for b in (doc.get("bindings") or []) if isinstance(b, dict)]
    if statuses is None:
        return items
    wanted = set(statuses)
    return [b for b in items if str(b.get("status") or "") in wanted]


def by_id(doc: dict[str, Any], binding_id: str) -> dict[str, Any]:
    for item in doc.get("bindings") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(binding_id):
            return item
    raise KeyError(binding_id)


# --------------------------------------------------------------------------
# 置信度
# --------------------------------------------------------------------------

def effective_confidence(binding: dict[str, Any], *, current_build: str | None = None) -> str:
    """声明置信度 + staleness → 生效置信度。

    build 换了或连续 no-match 达阈值，不管当初标到多高，一律降 stale：新 build 上
    没重新验证过的因果不是因果。
    """
    declared = str(binding.get("confidence") or OBSERVED)
    if declared == STALE:
        return STALE
    build = str(binding.get("build") or "")
    if current_build and build and build != str(current_build):
        return STALE
    evidence = dict(binding.get("evidence") or {})
    streak = int(evidence.get("no_match_streak") or 0)
    if streak >= STALE_NO_MATCH_STREAK:
        return STALE
    return declared


def check_promotion(binding: dict[str, Any], target: str) -> None:
    """置信度提升的证据闸。不满足直接抛——不存在「先标上、证据后补」。"""
    if target == STALE:
        return
    if target not in CONFIDENCE_ORDER:
        raise BindingError(f"未知置信度 {target!r}（合法值 {list(CONFIDENCE_ORDER)} 或 stale）")
    evidence = dict(binding.get("evidence") or {})
    positives = list(evidence.get("positive_runs") or [])
    if target == OBSERVED and not positives:
        raise BindingError(
            f"{binding.get('id')} → observed 需要至少 1 条 positive run（页面出现期间采到该接口）"
        )
    if target == CORRELATED and len(set(positives)) < MIN_CORRELATED_RUNS:
        raise BindingError(
            f"{binding.get('id')} → correlated 需要至少 {MIN_CORRELATED_RUNS} 个不同 run 的稳定共现，"
            f"当前 {len(set(positives))} 个"
        )
    if target == CAUSAL:
        negatives = list(evidence.get("negative_control_runs") or [])
        authority = str(evidence.get("authority") or "none")
        if not negatives and authority != "backend_contract":
            raise BindingError(
                f"{binding.get('id')} → causal 必须有控制变量实验（negative_control_runs）或后端权威契约"
                "（evidence.authority=backend_contract）。多次共现只能到 correlated——"
                "把相关性当因果会直接错报受影响页面（§17 首条风险）"
            )


def evidence_after_run(
    binding: dict[str, Any], run_id: str, *, matched: bool
) -> dict[str, Any]:
    """一次 run 之后的证据更新（纯函数）。命中清零 streak，未命中累加。"""
    evidence = dict(binding.get("evidence") or {})
    positives = list(evidence.get("positive_runs") or [])
    if matched:
        if run_id not in positives:
            positives.append(str(run_id))
        evidence["positive_runs"] = positives
        evidence["observed_count"] = int(evidence.get("observed_count") or 0) + 1
        evidence["no_match_streak"] = 0
    else:
        evidence["no_match_streak"] = int(evidence.get("no_match_streak") or 0) + 1
    return evidence


# --------------------------------------------------------------------------
# 反查
# --------------------------------------------------------------------------

def _binding_keys(binding: dict[str, Any]) -> set[str]:
    source = dict(binding.get("source") or {})
    return {
        str(value)
        for key, value in source.items()
        if key in ("data_key", "config_key") and value
    }


def resolve(
    doc: dict[str, Any],
    *,
    data_keys: Iterable[str] = (),
    endpoint_keys: Iterable[str] = (),
    response_paths: Iterable[str] = (),
    current_build: str | None = None,
    statuses: Sequence[str] | None = ("active",),
) -> list[BindingMatch]:
    """变更输入 → 命中的 binding，按 §10 的定位质量分级。

    ``jsonpath`` 是最精确的一档（key + endpoint + JSONPath 全对上）；只有 endpoint 的
    退到 ``endpoint``（该接口关联的全部页面状态）；只有 key 的退到 ``data_key``。
    一条 binding 只报最高的那一档。
    """
    wanted_keys = {str(k) for k in data_keys if k}
    wanted_endpoints = {str(k) for k in endpoint_keys if k}
    wanted_paths = [normalize_response_path(p) for p in response_paths if p]

    matches: list[BindingMatch] = []
    for binding in list_bindings(doc, statuses=statuses):
        keys = _binding_keys(binding)
        key_hit = bool(wanted_keys & keys) if wanted_keys else False
        ep_key = endpoint_key_from_binding(binding.get("endpoint"))
        ep_hit = bool(ep_key and ep_key in wanted_endpoints)
        declared_path = binding.get("response_path")
        path_hit = bool(
            declared_path
            and any(response_path_matches(p, str(declared_path)) for p in wanted_paths)
        )
        if not (key_hit or ep_hit or path_hit):
            continue
        if path_hit and (ep_hit or not wanted_endpoints):
            level = "jsonpath"
        elif ep_hit:
            level = "endpoint"
        else:
            level = "data_key"
        matches.append(
            BindingMatch(
                binding=binding,
                match_level=level,
                confidence=effective_confidence(binding, current_build=current_build),
            )
        )
    order = {"jsonpath": 0, "endpoint": 1, "data_key": 2}
    matches.sort(key=lambda m: (order[m.match_level], str(m.binding.get("id"))))
    return matches


def targets_for(match: BindingMatch) -> dict[str, Any]:
    """binding → plan target 里的页面部分。"""
    target = dict(match.binding.get("target") or {})
    return {
        "screen_id": str(target.get("screen_id") or "unknown"),
        "variant_selector": ctx_mod.normalize_variant(target.get("variant_selector")),
        "regions": [str(target["region"])] if target.get("region") else [],
    }


# --------------------------------------------------------------------------
# 写入（人审面）
# --------------------------------------------------------------------------

def append_binding(path: Path, fields: dict[str, Any], *, status: str = "draft") -> dict[str, Any]:
    """新增一条 binding。默认 draft——发现不等于成立。"""
    path = Path(path)
    app = str(load(path).get("meta", {}).get("app") or path.parent.parent.name)
    ensure_file(path, app)
    payload = dict(fields)
    payload.setdefault("confidence", OBSERVED)
    item = asset_store.append_item(BINDINGS_SPEC, path, payload, status=status)
    validate_or_raise("field-binding.v1.json", load(path))
    return item


def set_confidence(
    path: Path,
    binding_id: str,
    target: str,
    *,
    evidence: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """人审改置信度。先合并证据，再过闸——闸不过不落盘。"""
    path = Path(path)
    doc = load(path)
    binding = dict(by_id(doc, binding_id))
    merged = dict(binding.get("evidence") or {})
    for key, value in (evidence or {}).items():
        if key in ("positive_runs", "negative_control_runs"):
            existing = list(merged.get(key) or [])
            for item in value or []:
                if item not in existing:
                    existing.append(str(item))
            merged[key] = existing
        else:
            merged[key] = value
    if note:
        merged["note"] = note
    binding["evidence"] = merged
    check_promotion(binding, target)
    changes: dict[str, Any] = {"evidence": merged, "confidence": target}
    item = asset_store.rewrite_item_fields(
        BINDINGS_SPEC, path, binding_id, changes, bump_revision=True
    )
    validate_or_raise("field-binding.v1.json", load(path))
    return item


def record_run_evidence(
    path: Path, binding_id: str, run_id: str, *, matched: bool
) -> dict[str, Any]:
    """把一次 run 的命中/未命中写回证据链；连续 no-match 达阈值时自动降 stale。"""
    path = Path(path)
    doc = load(path)
    binding = dict(by_id(doc, binding_id))
    updated = evidence_after_run(binding, run_id, matched=matched)
    changes: dict[str, Any] = {"evidence": updated}
    if int(updated.get("no_match_streak") or 0) >= STALE_NO_MATCH_STREAK:
        changes["confidence"] = STALE
    item = asset_store.rewrite_item_fields(
        BINDINGS_SPEC, path, binding_id, changes, bump_revision=False
    )
    validate_or_raise("field-binding.v1.json", load(path))
    return item


def set_status(path: Path, binding_id: str, status: str) -> dict[str, Any]:
    item = asset_store.update_item(BINDINGS_SPEC, Path(path), binding_id, {"status": status})
    validate_or_raise("field-binding.v1.json", load(Path(path)))
    return item
