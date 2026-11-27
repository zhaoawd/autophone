"""Impact Index：由资产 + 观测派生的可重建投影（影响图谱 §13）。

索引**不是真源**。真源是 FieldBinding（人审资产）、Observation（不可变执行事实）和
Screen Asset；索引只是把它们按「变更清单会怎么查」重排一遍，删掉整个目录再 rebuild
应当得到逐字节相同的结果。所以这里不做任何判断、不写回任何资产，只做投影和统计。

它同时是覆盖缺口的来源：观测到但没人绑定的接口、绑定指向但资产里不存在的 screen、
一个 binding 都没有的页面——这些都要显式浮出来，而不是被「看起来挺全」的统计盖掉。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from autophone.paths import STATE_ROOT
from autophone.observation.impact import bindings as bindings_mod
from autophone.observation.impact import context as ctx_mod
from autophone.observation.impact.endpoints import (
    endpoint_key_from_binding,
    normalize_response_path,
)
from autophone.observation.impact.observations import iter_run_observations
from autophone.util import now_iso, write_json_atomic

INDEX_SCHEMA_VERSION = "v1"


def index_root() -> Path:
    """派生索引根目录。

    默认落可变状态目录的 ``autophone/_data/generated/``（§13 指定路径）。包内
    ``_data/schemas`` 是只读随包资源，派生产物不进入发布目录。
    ``AUTOPHONE_IMPACT_INDEX_DIR`` 可覆写。
    """
    override = os.environ.get("AUTOPHONE_IMPACT_INDEX_DIR")
    if override:
        return Path(override)
    return STATE_ROOT / "autophone" / "_data" / "generated" / "impact-index"


def index_path(app: str, build: str, *, root: Path | None = None) -> Path:
    return (root or index_root()) / str(app) / f"{build}.json"


def build_index(
    *,
    app: str,
    build: str,
    bindings_doc: dict[str, Any],
    observations: Iterable[dict[str, Any]] = (),
    screen_ids: Iterable[str] = (),
    screen_regions: dict[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """资产 + 观测 → 索引文档（纯函数）。"""
    by_data_key: dict[str, list[str]] = defaultdict(list)
    by_endpoint: dict[str, list[str]] = defaultdict(list)
    by_response_path: dict[str, list[str]] = defaultdict(list)
    by_screen: dict[str, list[str]] = defaultdict(list)
    binding_rows: dict[str, dict[str, Any]] = {}

    all_bindings = bindings_mod.list_bindings(bindings_doc, statuses=None)
    for binding in all_bindings:
        bid = str(binding.get("id"))
        status = str(binding.get("status") or "")
        confidence = bindings_mod.effective_confidence(binding, current_build=build)
        source = dict(binding.get("source") or {})
        target = dict(binding.get("target") or {})
        ep_key = endpoint_key_from_binding(binding.get("endpoint"))
        path = normalize_response_path(binding.get("response_path"))
        binding_rows[bid] = {
            "status": status,
            "declared_confidence": str(binding.get("confidence") or ""),
            "confidence": confidence,
            "build": str(binding.get("build") or ""),
            "endpoint_key": ep_key,
            "response_path": path or None,
            "screen_id": str(target.get("screen_id") or ""),
            "variant_key": ctx_mod.variant_key(target.get("variant_selector")),
            "region": str(target.get("region") or "") or None,
            "relation": str(binding.get("relation") or ""),
            "sensitivity": str(binding.get("sensitivity") or "normal"),
            "volatile": bool(binding.get("volatile")),
        }
        if status not in ("active", "draft"):
            continue
        for key in ("data_key", "config_key"):
            value = source.get(key)
            if value:
                by_data_key[str(value)].append(bid)
        if ep_key:
            by_endpoint[ep_key].append(bid)
        if path:
            by_response_path[path].append(bid)
        if target.get("screen_id"):
            by_screen[str(target["screen_id"])].append(bid)

    observed_endpoints: dict[str, dict[str, Any]] = {}
    screen_states: dict[str, dict[str, Any]] = {}
    observation_count = 0
    for obs in observations:
        observation_count += 1
        if str(obs.get("build") or "") != str(build):
            continue
        to_state = dict(obs.get("to") or {})
        screen_id = str(to_state.get("screen_id") or "unknown")
        vkey = ctx_mod.variant_key(to_state.get("variant"))
        state = screen_states.setdefault(
            f"{screen_id}|{vkey}",
            {"screen_id": screen_id, "variant_key": vkey, "observations": 0, "endpoints": []},
        )
        state["observations"] += 1
        for hit in obs.get("endpoints") or []:
            key = str(hit.get("endpoint_key") or "")
            if not key:
                continue
            row = observed_endpoints.setdefault(
                key,
                {"screens": [], "observations": 0, "noise": 0, "triggers": {}, "response_paths": []},
            )
            row["observations"] += 1
            if hit.get("noise"):
                row["noise"] += 1
            trigger = str(hit.get("trigger") or "unknown")
            row["triggers"][trigger] = int(row["triggers"].get(trigger, 0)) + 1
            if screen_id not in row["screens"]:
                row["screens"].append(screen_id)
            for path in hit.get("response_paths") or []:
                if path not in row["response_paths"] and len(row["response_paths"]) < 200:
                    row["response_paths"].append(path)
            if key not in state["endpoints"]:
                state["endpoints"].append(key)

    known_screens = {str(s) for s in screen_ids}
    regions = {str(k): sorted({str(r) for r in v}) for k, v in (screen_regions or {}).items()}

    gaps: list[dict[str, Any]] = []
    for screen_id, ids in sorted(by_screen.items()):
        if known_screens and screen_id not in known_screens:
            gaps.append({
                "kind": "unknown_screen",
                "detail": f"binding {sorted(ids)} 指向 screen={screen_id}，但 build={build} 的 screen 资产里没有它",
                "screen_id": screen_id,
            })
    for binding_id, row in sorted(binding_rows.items()):
        region = row.get("region")
        screen_id = row.get("screen_id")
        if region and screen_id in regions and region not in regions[screen_id]:
            gaps.append({
                "kind": "unknown_region",
                "detail": f"{binding_id} 指向 {screen_id}.{region}，screen 资产未声明该区域",
                "screen_id": screen_id,
            })
        if row["confidence"] == bindings_mod.STALE and row["status"] == "active":
            gaps.append({
                "kind": "stale_binding",
                "detail": f"{binding_id} 生效置信度已降 stale（build={row['build']} vs {build}），需重新标定",
            })
    for key, row in sorted(observed_endpoints.items()):
        if row["noise"] >= row["observations"]:
            continue  # 全是噪声的接口不算覆盖缺口
        if key not in by_endpoint:
            gaps.append({
                "kind": "unbound_endpoint",
                "detail": f"观测到 {key}（{row['observations']} 次）但没有任何 FieldBinding",
                "endpoint_key": key,
            })
    for screen_id in sorted(known_screens):
        if screen_id not in by_screen:
            gaps.append({
                "kind": "unbound_screen",
                "detail": f"screen={screen_id} 有资产但没有任何 FieldBinding",
                "screen_id": screen_id,
            })

    active_ids = {bid for bid, row in binding_rows.items() if row["status"] == "active"}
    causal_ids = {
        bid for bid in active_ids
        if binding_rows[bid]["confidence"] == bindings_mod.CAUSAL
    }
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "app": str(app),
        "build": str(build),
        "generated_at": now_iso(),
        "bindings": binding_rows,
        "by_data_key": {k: sorted(v) for k, v in sorted(by_data_key.items())},
        "by_endpoint": {k: sorted(v) for k, v in sorted(by_endpoint.items())},
        "by_response_path": {k: sorted(v) for k, v in sorted(by_response_path.items())},
        "by_screen": {k: sorted(v) for k, v in sorted(by_screen.items())},
        "observed_endpoints": dict(sorted(observed_endpoints.items())),
        "screen_states": dict(sorted(screen_states.items())),
        "counts": {
            "bindings": len(binding_rows),
            "active_bindings": len(active_ids),
            "causal_bindings": len(causal_ids),
            "observations": observation_count,
            "observed_endpoints": len(observed_endpoints),
            "screen_states": len(screen_states),
            "screens_with_assets": len(known_screens),
            "gaps": len(gaps),
        },
        "gaps": gaps,
    }


def rebuild(
    *,
    app: str,
    build: str,
    bindings_doc: dict[str, Any],
    runs_root: Path | None = None,
    screen_ids: Iterable[str] = (),
    screen_regions: dict[str, Iterable[str]] | None = None,
    out_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """扫 runs 树重建索引并落盘。删掉整个目录再跑一次应当等价。"""
    observations: list[dict[str, Any]] = []
    if runs_root is not None and Path(runs_root).exists():
        for _run_dir, obs in iter_run_observations(Path(runs_root)):
            if str(obs.get("app") or "") == str(app):
                observations.append(obs)
    doc = build_index(
        app=app,
        build=build,
        bindings_doc=bindings_doc,
        observations=observations,
        screen_ids=screen_ids,
        screen_regions=screen_regions,
    )
    path = index_path(app, build, root=out_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, doc)
    return path, doc


def load_index(app: str, build: str, *, root: Path | None = None) -> dict[str, Any] | None:
    path = index_path(app, build, root=root)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None
