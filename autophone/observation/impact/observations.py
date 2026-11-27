"""Observation Assembler：把四源证据确定性合并成一次页面迁移的事实（影响图谱 §6.4 / §八）。

四源 = action mark（动作何时开始）+ API capture（期间发生了哪些请求）+ 前后 screen
checkpoint（从哪个页面到哪个页面）+ ConsumerContext（当时是什么消费状态）。

采集时序（§八）：

    before screen checkpoint + screenshot
      → API mark(action started) → execute safe action
      → wait relevant API response / quiet window
      → target screen checkpoint → two-frame visual stability
      → after screenshot → finalize observation

合并规则里有两条是**硬的**，因为它们决定归因是不是可信：

1. ``action mark 必须发生在触发网络的动作之前``：``t_req < mark.t_start`` 的请求
   在物理上不可能由这个动作触发，一律不归给它。
2. mark 序列必须单调不重叠。乱序或重叠意味着执行侧的埋点坏了，此时任何归因都是
   编的——直接 ``ObservationError`` fail-closed，不产出「看起来有结论」的观测。

Observation 是 append-only 的执行事实，只说发生了什么，**不声称字段因果**；因果住在
field-binding 的 confidence 里，要么有控制变量实验，要么有后端权威契约。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from autophone.contracts import validate_or_raise
from autophone.observation.impact import context as ctx_mod
from autophone.observation.impact.endpoints import (
    EndpointNormalizeError,
    normalize_endpoint,
    response_paths,
)

OBSERVATIONS_REL = "impact/observations.jsonl"
ACTION_MARKS_REL = "impact/action-marks.jsonl"
CONSUMER_CONTEXT_REL = "impact/consumer-context.json"
CAPTURE_REL = "planes/api/capture.jsonl"

# 动作结束后仍算「这个动作引起的」的静默窗（秒）。超出即视为后台活动，不进归因。
DEFAULT_QUIET_WINDOW_S = 3.0

ACTION_KINDS = frozenset({"cold_start", "operation_step", "refresh", "manual"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# noise catalog：心跳/埋点/日志上报这类与页面无关的请求。命中只打标不丢弃——
# 报告仍要能解释「这些请求我看见了，但没拿它们做归因」。
DEFAULT_NOISE_PATTERNS: tuple[str, ...] = (
    "/log", "/logs", "/track", "/tracking", "/collect", "/report",
    "/heartbeat", "/beacon", "/metrics", "/datatrace", "/monitor",
)


class ObservationError(ValueError):
    """action mark / capture 输入不自洽，归因不可信。"""


@dataclass(frozen=True)
class ActionMark:
    """执行侧在触发动作**之前**打下的标记。

    ``t_end`` 是目标 screen checkpoint 达成的时刻；缺省时窗口上界退化为下一个 mark 的
    起点（或 quiet window），归因会更宽——所以执行器应尽量给。
    """

    seq: int
    kind: str
    t_start: float
    t_end: float | None = None
    operation: str | None = None
    step: str | None = None
    step_index: int | None = None
    safety: str = "read_only"
    effects: tuple[str, ...] = ()
    from_screen: dict[str, Any] = field(default_factory=dict)
    to_screen: dict[str, Any] = field(default_factory=dict)
    status: str = "passed"
    blocker: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ActionMark":
        kind = str(raw.get("kind") or "operation_step")
        if kind not in ACTION_KINDS:
            raise ObservationError(f"未知 action mark kind={kind!r}（合法值 {sorted(ACTION_KINDS)}）")
        if raw.get("t_start") is None:
            raise ObservationError(f"action mark seq={raw.get('seq')} 缺 t_start，无法建立归因窗口")
        t_start = float(raw["t_start"])
        t_end = None if raw.get("t_end") is None else float(raw["t_end"])
        if t_end is not None and t_end < t_start:
            raise ObservationError(
                f"action mark seq={raw.get('seq')} 的 t_end({t_end}) 早于 t_start({t_start})"
            )
        return cls(
            seq=int(raw.get("seq") or 0),
            kind=kind,
            t_start=t_start,
            t_end=t_end,
            operation=(str(raw["operation"]) if raw.get("operation") else None),
            step=(str(raw["step"]) if raw.get("step") else None),
            step_index=(int(raw["step_index"]) if raw.get("step_index") is not None else None),
            safety=str(raw.get("safety") or "read_only"),
            effects=tuple(str(item) for item in (raw.get("effects") or [])),
            from_screen=dict(raw.get("from") or {}),
            to_screen=dict(raw.get("to") or {}),
            status=str(raw.get("status") or "passed"),
            blocker=(dict(raw["blocker"]) if isinstance(raw.get("blocker"), dict) else None),
        )


@dataclass(frozen=True)
class AssembleResult:
    observations: list[dict[str, Any]]
    unattributed: list[dict[str, Any]]
    stats: dict[str, Any]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # 坏行跳过：抓取面是旁路证据，半行不该拖垮整次归因
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_action_marks(run_dir: Path) -> list[ActionMark]:
    rows = _read_jsonl(Path(run_dir) / ACTION_MARKS_REL)
    marks = [ActionMark.from_dict(row) for row in rows]
    return _validate_sequence(marks)


def _validate_sequence(marks: Sequence[ActionMark]) -> list[ActionMark]:
    """mark 序列必须按 t_start 单调、窗口互不重叠，否则归因不可信 → fail-closed。"""
    ordered = sorted(marks, key=lambda m: (m.t_start, m.seq))
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.t_end is not None and cur.t_start < prev.t_end:
            raise ObservationError(
                f"action mark 窗口重叠：seq={prev.seq} 的 t_end({prev.t_end}) 晚于 "
                f"seq={cur.seq} 的 t_start({cur.t_start})；归因窗口无法确定"
            )
    seqs = [m.seq for m in ordered]
    if len(set(seqs)) != len(seqs):
        raise ObservationError(f"action mark seq 重复：{seqs}")
    if seqs != sorted(seqs):
        raise ObservationError(
            f"action mark 的 seq 顺序与时间顺序不一致：按时间排出来是 {seqs}；"
            "执行侧埋点有问题，拒绝在此之上做归因"
        )
    return list(ordered)


def _is_noise(rec: dict[str, Any], path_template: str, patterns: Sequence[str]) -> bool:
    if bool(rec.get("noise")):
        return True
    low = path_template.lower()
    return any(pattern in low for pattern in patterns)


def _classify_trigger(mark: ActionMark, rec: dict[str, Any], t_req: float) -> str:
    """区分 on_enter / refresh / prefetch / submit（§8 第 3 条）。

    prefetch 判据是「动作已经完成（目标页已 checkpoint）之后才发出的请求」——这类请求
    不是这次进入页面的直接产物，不能拿去做字段绑定的主证据。
    """
    if mark.t_end is not None and t_req > mark.t_end:
        return "prefetch"
    method = str(rec.get("method") or "").strip().upper()
    if mark.safety == "mutating" and method in _MUTATING_METHODS:
        return "submit"
    if mark.kind == "refresh":
        return "refresh"
    if mark.kind in ("cold_start", "operation_step"):
        return "on_enter"
    return "unknown"


def _endpoint_hit(
    rec: dict[str, Any],
    mark: ActionMark,
    t_req: float,
    *,
    aliases: dict[str, str] | None,
    noise_patterns: Sequence[str],
    with_response_paths: bool,
) -> dict[str, Any] | None:
    try:
        key = normalize_endpoint(rec.get("method"), rec.get("url"), aliases=aliases)
    except EndpointNormalizeError:
        return None
    hit: dict[str, Any] = key.as_dict()
    hit["status"] = rec.get("status") if isinstance(rec.get("status"), int) else None
    hit["ms"] = rec.get("ms") if isinstance(rec.get("ms"), (int, float)) else None
    hit["trigger"] = _classify_trigger(mark, rec, t_req)
    hit["noise"] = _is_noise(rec, key.path_template, noise_patterns)
    task_ptr = rec.get("task_ptr")
    if task_ptr:
        hit["api_ref"] = f"{CAPTURE_REL}#task_ptr={task_ptr}"
    if with_response_paths:
        body = rec.get("resp_body_text")
        if isinstance(body, str) and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if parsed is not None:
                paths = response_paths(parsed)
                if paths:
                    hit["response_paths"] = paths
    return hit


def _window_upper(
    mark: ActionMark,
    next_mark: ActionMark | None,
    quiet_window_s: float,
) -> float:
    upper = math.inf if mark.t_end is None else mark.t_end + quiet_window_s
    if next_mark is not None:
        upper = min(upper, next_mark.t_start)
    return upper


def assemble(
    *,
    run_id: str,
    app: str,
    build: str,
    marks: Iterable[ActionMark | dict[str, Any]],
    captures: Iterable[dict[str, Any]],
    consumer_context: dict[str, Any] | None = None,
    context_ref: str | None = CONSUMER_CONTEXT_REL,
    host_aliases: dict[str, str] | None = None,
    noise_patterns: Sequence[str] = DEFAULT_NOISE_PATTERNS,
    quiet_window_s: float = DEFAULT_QUIET_WINDOW_S,
    with_response_paths: bool = True,
) -> AssembleResult:
    """四源 → observation 列表。纯函数，不碰设备也不碰全局状态。"""
    prepared = _validate_sequence([
        item if isinstance(item, ActionMark) else ActionMark.from_dict(item)
        for item in marks
    ])
    records = [rec for rec in captures if isinstance(rec, dict)]
    if records and not prepared:
        raise ObservationError(
            "有 API capture 但没有任何 action mark：无法把请求归因到动作。"
            "执行器必须在触发网络的动作之前打 mark（§八 第 1 条）"
        )

    context_variant = ctx_mod.variant_from_context(consumer_context)
    buckets: dict[int, list[dict[str, Any]]] = {mark.seq: [] for mark in prepared}
    unattributed: list[dict[str, Any]] = []

    for rec in records:
        t_req = rec.get("t_req")
        if not isinstance(t_req, (int, float)):
            unattributed.append(_unattributed(rec, "missing_t_req", host_aliases))
            continue
        placed = False
        for idx, mark in enumerate(prepared):
            nxt = prepared[idx + 1] if idx + 1 < len(prepared) else None
            upper = _window_upper(mark, nxt, quiet_window_s)
            # 下界是闭的、上界是开的：mark 时刻发出的请求算这个动作的，
            # 下一个 mark 时刻发出的请求算下一个动作的。
            if mark.t_start <= float(t_req) < upper:
                hit = _endpoint_hit(
                    rec, mark, float(t_req),
                    aliases=host_aliases,
                    noise_patterns=noise_patterns,
                    with_response_paths=with_response_paths,
                )
                if hit is not None:
                    buckets[mark.seq].append(hit)
                else:
                    unattributed.append(_unattributed(rec, "unnormalizable_endpoint", host_aliases))
                placed = True
                break
        if not placed:
            reason = (
                "before_first_mark"
                if float(t_req) < prepared[0].t_start
                else "outside_action_windows"
            )
            unattributed.append(_unattributed(rec, reason, host_aliases))

    observations: list[dict[str, Any]] = []
    for mark in prepared:
        obs = _observation(
            run_id=run_id,
            app=app,
            build=build,
            mark=mark,
            hits=buckets[mark.seq],
            context_ref=context_ref,
            context_variant=context_variant,
        )
        validate_or_raise("observation.v1.json", obs)
        observations.append(obs)

    signal = sum(
        1 for obs in observations for hit in obs.get("endpoints", []) if not hit.get("noise")
    )
    total_hits = sum(len(obs.get("endpoints", [])) for obs in observations)
    stats = {
        "marks": len(prepared),
        "captures": len(records),
        "attributed": total_hits,
        "attributed_signal": signal,
        "unattributed": len(unattributed),
        "attribution_rate": (total_hits / len(records)) if records else None,
    }
    return AssembleResult(observations=observations, unattributed=unattributed, stats=stats)


def _unattributed(
    rec: dict[str, Any], reason: str, aliases: dict[str, str] | None
) -> dict[str, Any]:
    try:
        key = normalize_endpoint(rec.get("method"), rec.get("url"), aliases=aliases)
        endpoint = key.key
    except EndpointNormalizeError:
        endpoint = None
    return {
        "reason": reason,
        "endpoint_key": endpoint,
        "t_req": rec.get("t_req"),
        "task_ptr": rec.get("task_ptr"),
    }


def _screen_state(raw: dict[str, Any], fallback_variant: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"screen_id": str(raw.get("screen_id") or "unknown")}
    variant = ctx_mod.normalize_variant(raw.get("variant") or fallback_variant)
    if variant:
        state["variant"] = variant
    if isinstance(raw.get("checkpoint"), dict):
        state["checkpoint"] = dict(raw["checkpoint"])
    if raw.get("frame_ref"):
        state["frame_ref"] = str(raw["frame_ref"])
    return state


def _observation(
    *,
    run_id: str,
    app: str,
    build: str,
    mark: ActionMark,
    hits: list[dict[str, Any]],
    context_ref: str | None,
    context_variant: dict[str, Any],
) -> dict[str, Any]:
    action: dict[str, Any] = {"kind": mark.kind, "t_start": mark.t_start}
    if mark.t_end is not None:
        action["t_end"] = mark.t_end
    if mark.operation:
        action["operation"] = mark.operation
    if mark.step:
        action["step"] = mark.step
    if mark.step_index is not None:
        action["step_index"] = mark.step_index
    action["safety"] = mark.safety
    if mark.effects:
        action["effects"] = list(mark.effects)

    obs: dict[str, Any] = {
        "schema_version": "v1",
        "run_id": run_id,
        "app": app,
        "build": build,
        "seq": mark.seq,
        "action": action,
        "from": _screen_state(mark.from_screen, context_variant),
        "to": _screen_state(mark.to_screen, context_variant),
        "status": mark.status,
    }
    if context_ref:
        obs["context_ref"] = context_ref
    if hits:
        obs["endpoints"] = hits
        refs = [hit["api_ref"] for hit in hits if hit.get("api_ref")]
        if refs:
            obs["api_refs"] = refs
    frames = [
        state["frame_ref"]
        for state in (obs["from"], obs["to"])
        if state.get("frame_ref")
    ]
    if frames:
        obs["frame_refs"] = frames
    if mark.blocker:
        obs["blocker"] = mark.blocker
    return obs


def assemble_run(
    run_dir: Path,
    *,
    run_id: str | None = None,
    app: str | None = None,
    build: str | None = None,
    host_aliases: dict[str, str] | None = None,
    quiet_window_s: float = DEFAULT_QUIET_WINDOW_S,
) -> AssembleResult:
    """从一个 run 目录读四源并合并。离线可跑：不需要设备，也不需要活的抓取会话。"""
    run_dir = Path(run_dir)
    marks = load_action_marks(run_dir)
    captures = _read_jsonl(run_dir / CAPTURE_REL)
    ctx_path = run_dir / CONSUMER_CONTEXT_REL
    consumer_context = None
    if ctx_path.exists():
        try:
            consumer_context = json.loads(ctx_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ObservationError(f"consumer-context.json 解析失败：{exc}") from exc
    meta = _run_meta(run_dir)
    resolved_run_id = run_id or meta.get("run_id") or run_dir.name
    resolved_app = app or meta.get("app")
    resolved_build = build or meta.get("build")
    if not resolved_app:
        raise ObservationError(f"无法确定 app：{run_dir} 下既无 result.json 也未显式指定")
    if not resolved_build:
        raise ObservationError(f"无法确定 build：{run_dir} 下既无 result.json 也未显式指定")
    return assemble(
        run_id=str(resolved_run_id),
        app=str(resolved_app),
        build=str(resolved_build),
        marks=marks,
        captures=captures,
        consumer_context=consumer_context,
        context_ref=CONSUMER_CONTEXT_REL if ctx_path.exists() else None,
        host_aliases=host_aliases,
        quiet_window_s=quiet_window_s,
    )


def _run_meta(run_dir: Path) -> dict[str, Any]:
    for name in ("result.json", "operation-result.json"):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(doc, dict):
            return {
                "run_id": doc.get("run_id"),
                "app": doc.get("app"),
                "build": doc.get("build") or doc.get("build_label"),
            }
    return {}


def write_observations(run_dir: Path, observations: Iterable[dict[str, Any]]) -> Path:
    """append-only 落盘。已存在的行永不重写——observation 是不可变执行事实。"""
    path = Path(run_dir) / OBSERVATIONS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for obs in observations:
            fh.write(json.dumps(obs, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def load_observations(run_dir: Path) -> list[dict[str, Any]]:
    return _read_jsonl(Path(run_dir) / OBSERVATIONS_REL)


def iter_run_observations(runs_root: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    """扫一棵 runs 目录树，产出 (run_dir, observation)。索引重建用。"""
    for path in sorted(Path(runs_root).glob(f"**/{OBSERVATIONS_REL}")):
        run_dir = path.parent.parent
        for obs in _read_jsonl(path):
            yield run_dir, obs
