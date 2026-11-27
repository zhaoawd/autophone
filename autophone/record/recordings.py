"""Host-side recording registry for the record/replay dashboard.

Recordings live under ``runs/rec``.  The stable identifier is the repo-relative
directory path such as ``runs/rec/food_ordering/20260625-120000-enter_home``.  That
same value is written to ``operation.provenance.from`` so trace -> draft
association is exact and auditable.

归位（BL-014 执行段第二轮，2026-09-10）：BL-012 时因函数体内四处 import
`platform.record_lift` 而连带留守；`record_lift` 已归位 `autophone/record/lift.py`，
本模块随之由 `platform/recordings.py` 搬到本路径。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from autophone.paths import APP_ASSET_ROOT, CODE_ROOT, STATE_ROOT  # noqa: F401 -- CODE_ROOT 保留给既有调用方
from autophone.execution import operation_run as op_run
from autophone.assets.apps import load_app_profile
from autophone.execution.app_recipes import import_app_recipes
from autophone.device.lock import ScreenLock, default_screen_lock_root
from autophone.util import now_iso
from autophone.execution.engine.cases_loader import load_cases
from autophone.execution.engine import recipe as recipe_mod

ROOT = APP_ASSET_ROOT
RECORDINGS_ROOT = STATE_ROOT / "runs" / "rec"
META_NAME = "recording.meta.json"
DATA_TRACE_NAME = "data-trace.json"
OPERATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 录制起点候选：只有「回到干净首页」这类复位 operation 才配当录制起点声明
# （select_store_takeout 这种导航算子不是起点）。这里是候选 + 偏好序 + 展示文案的
# 唯一真源；某 app 实际能选哪些，由它注册了候选里的哪几个决定（见 start_options）。
# 越靠前越优先（前端默认取首项），所以 relaunch_home（冷启更确定）排在 reset 之前。
START_OPERATION_CANDIDATES: list[tuple[str, str]] = [
    ("relaunch_home", "relaunch_home（杀进程冷启回首页）"),
    ("reset_to_home", "reset_to_home（界面回退）"),
]


def start_options(app: str, *, root: Path = ROOT) -> list[dict[str, str]]:
    """该 app 可用的录制起点下拉项：候选里它真正注册了执行器的那些 + 「无起点声明」。

    起点声明会作为前置 operation 步插进录制蒸馏的 recipe，所以只能列该 app 真注册了
    的复位 operation——否则回放期 validate_recipe 会因引用未注册 operation fail-closed。
    """
    options: list[dict[str, str]] = []
    # import_app_recipes False = 该 app 没有 recipes 包；只在成功捕获该 App/root
    # 的注册表后读取候选，否则按零注册处理。
    try:
        registered = (
            set(recipe_mod.operations_for_app(app, root=root))
            if import_app_recipes(app, root=root)
            else set()
        )
    except Exception:
        registered = set()
    for value, label in START_OPERATION_CANDIDATES:
        if value in registered:
            options.append({"value": value, "label": label})
    options.append({"value": "", "label": "无起点声明"})
    return options


def normalize_operation_name(value: str | None, *, fallback: str = "recorded_operation") -> str:
    raw = str(value or "").strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", raw)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = fallback
    if not re.match(r"^[a-z]", name):
        name = f"op_{name}"
    return name[:64].rstrip("_") or fallback


class RecordingError(RuntimeError):
    """Recording registry or lifecycle error."""


class DeviceBusy(RecordingError):
    """开录时设备锁已被别人按着（run / 浏览器录制 / 远程操作 / 另一个录制窗口）。

    是 RecordingError 的子类，所以老的 422 兜底仍然接得住；API 层单独接它给 409。
    """


Launcher = Callable[..., Any]
Terminator = Callable[[int], None]


def _repo_rel(path: Path, *, root: Path = ROOT) -> str:
    return path.resolve().relative_to(_state_root(root).resolve()).as_posix()


def _state_root(root: Path) -> Path:
    return STATE_ROOT if root == APP_ASSET_ROOT else root


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return slug[:48] or "recording"


def _pid_alive(pid: Any) -> bool:
    try:
        ipid = int(pid)
    except (TypeError, ValueError):
        return False
    if ipid <= 0:
        return False
    if _pid_zombie(ipid):
        return False
    try:
        os.kill(ipid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_zombie(pid: int) -> bool:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


def _validate_starts_with(app: str, starts_with: str | None, *, root: Path = ROOT) -> None:
    if not starts_with:
        return
    if not import_app_recipes(app, root=root):
        raise RecordingError(
            f"app {app!r} has no recipe package; cannot use starts_with={starts_with!r}"
        )
    operations = recipe_mod.operations_for_app(app, root=root)
    if starts_with not in operations:
        raise RecordingError(
            f"unknown starts_with {starts_with!r} for app {app!r}; "
            f"registered={sorted(operations)}"
        )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _recording_action_points(
    directory: Path,
    trace: dict[str, Any],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build stable absolute action points without exposing recorded input text."""

    started_at = _parse_iso(meta.get("started_at"))
    session = _read_json(directory / "session.json") or {}
    committed = session.get("committed") if isinstance(session.get("committed"), dict) else {}
    acknowledgements: dict[int, dict[str, Any]] = {}
    for ack in committed.values():
        if not isinstance(ack, dict):
            continue
        index = ack.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            acknowledgements[index] = ack
    points: list[dict[str, Any]] = []
    for zero_index, event in enumerate(trace.get("events") or []):
        if not isinstance(event, dict):
            continue
        ack = acknowledgements.get(zero_index) or {}
        action_at = _parse_iso(ack.get("action_at"))
        if action_at is None and started_at is not None:
            try:
                action_at = started_at + timedelta(seconds=max(0.0, float(event.get("t") or 0)))
            except (TypeError, ValueError):
                action_at = started_at
        committed_at = _parse_iso(ack.get("committed_at")) or action_at
        points.append({
            "index": zero_index + 1,
            "kind": str(event.get("kind") or ack.get("kind") or "event"),
            "action_at": action_at.isoformat() if action_at is not None else None,
            "committed_at": committed_at.isoformat() if committed_at is not None else None,
        })
    return points


def recording_data_trace(
    app: str,
    recording_id: str,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Return the persisted, shareable recording readback view."""

    directory = resolve_recording_id(recording_id, root=root)
    if not directory.exists():
        raise FileNotFoundError(recording_id)
    meta = _read_json(directory / META_NAME) or {}
    trace = _read_json(directory / "trace.json") or _read_json(directory / "trace.partial.json") or {}
    row_app = str(trace.get("app") or meta.get("app") or "")
    if row_app and row_app != app:
        raise FileNotFoundError(recording_id)
    stored = _read_json(directory / DATA_TRACE_NAME)
    if stored is not None:
        return {**stored, "available": True}
    return {
        "schema_version": "recording-data-trace.v1",
        "available": False,
        "status": "not_queried",
        "recording_id": recording_id,
        "action_count": len(trace.get("events") or []),
        "event_count": 0,
        "actions": [],
        "unattributed_events": [],
    }


def query_recording_data_trace(
    app: str,
    recording_id: str,
    identity: dict[str, Any],
    *,
    root: Path = ROOT,
    profile_raw: dict[str, Any] | None = None,
    query_func: Callable[..., dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _local_now,
) -> dict[str, Any]:
    """Query dataTrace for a recording and persist action-level correlations.

    The raw phone / DeviceID is passed only to ``query_func``. The persisted
    artifact contains the already-redacted dataTrace events and identity field,
    never the identity value.
    """

    if not isinstance(identity, dict) or set(identity) != {"field", "value"}:
        raise RecordingError("identity must contain exactly field and value")
    identity_field = str(identity.get("field") or "").strip()
    identity_value = identity.get("value")
    if identity_field not in {"phone_no", "device_id"}:
        raise RecordingError("recording dataTrace identity must be phone_no or device_id")
    if not isinstance(identity_value, str) or not identity_value.strip():
        raise RecordingError("recording dataTrace identity value is required")

    directory = resolve_recording_id(recording_id, root=root)
    if not directory.exists():
        raise FileNotFoundError(recording_id)
    meta = _read_json(directory / META_NAME) or {}
    trace = _read_json(directory / "trace.json") or _read_json(directory / "trace.partial.json")
    if not isinstance(trace, dict):
        raise RecordingError("recording trace is not available")
    row_app = str(trace.get("app") or meta.get("app") or "")
    if row_app and row_app != app:
        raise FileNotFoundError(recording_id)
    points = _recording_action_points(directory, trace, meta)
    timed_points = [
        (point, _parse_iso(point.get("action_at")))
        for point in points
        if _parse_iso(point.get("action_at")) is not None
    ]
    if not timed_points:
        raise RecordingError("recording has no timestamped actions")

    profile = profile_raw or load_app_profile(app, root=root).raw
    telemetry_cap = ((profile.get("capabilities") or {}).get("telemetry") or {})
    try:
        padding_s = max(0.0, float(telemetry_cap.get("query_padding_s", 10)))
    except (TypeError, ValueError):
        padding_s = 10.0
    try:
        clock_skew_s = max(0.0, float(telemetry_cap.get("attribution_clock_skew_s", 2)))
    except (TypeError, ValueError):
        clock_skew_s = 2.0
    try:
        query_delay_s = max(0.0, float(telemetry_cap.get("record_replay_delay_s", 10)))
    except (TypeError, ValueError):
        query_delay_s = 10.0
    action_times = [item[1] for item in timed_points if item[1] is not None]
    completed_times = [
        _parse_iso(point.get("committed_at")) or point_at
        for point, point_at in timed_points
    ]
    # 前端有可见倒计时，服务端再守一次 ready_at，避免刷新、脚本直调或手动
    # “重新查询”绕过 dataTrace 上报链的 10 秒可见性窗口。
    remaining_s = (max(completed_times) + timedelta(seconds=query_delay_s) - now()).total_seconds()
    if remaining_s > 0:
        sleep(remaining_s)
    started_at = min(action_times) - timedelta(seconds=padding_s)
    ended_at = max(action_times) + timedelta(seconds=padding_s)
    if query_func is None:
        from autophone.observation.telemetry import query_telemetry
        query_func = query_telemetry
    result = query_func(
        app=app,
        profile_raw=profile,
        request={
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "identity": {"field": identity_field, "value": identity_value.strip()},
            "filters": [],
            "limit": 10_000,
        },
    )

    by_index = {
        point["index"]: {**point, "status": "unmatched", "event_count": 0, "events": []}
        for point in points
    }
    unattributed: list[dict[str, Any]] = []
    for raw_event in result.get("events") or []:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        occurred_at = _parse_iso(event.get("occurred_at"))
        candidates: list[tuple[dict[str, Any], datetime]] = []
        if occurred_at is not None:
            # 正常归到“最近一次已经发生的动作”。只有事件时钟略早于第一条动作时，
            # 才使用 clock_skew_s 向前容忍；不能让未来动作抢走前一动作的事件。
            candidates = [
                (point, point_at)
                for point, point_at in timed_points
                if point_at is not None and point_at <= occurred_at
            ]
            if not candidates:
                candidates = [
                    (point, point_at)
                    for point, point_at in timed_points
                    if (
                        point_at is not None
                        and occurred_at < point_at <= occurred_at + timedelta(seconds=clock_skew_s)
                    )
                ]
        if not candidates:
            unattributed.append(event)
            continue
        point, point_at = (
            max(candidates, key=lambda item: item[1])
            if any(point_at <= occurred_at for _point, point_at in candidates)
            else min(candidates, key=lambda item: item[1])
        )
        delta_ms = round((occurred_at - point_at).total_seconds() * 1000, 1)
        event["recording_action_index"] = point["index"]
        event["recording_action_kind"] = point["kind"]
        event["recording_attribution"] = {
            "mode": "time_inferred",
            "delta_ms": delta_ms,
        }
        target = by_index[point["index"]]
        target["status"] = "matched"
        target["event_count"] += 1
        target["events"].append(event)

    for action in by_index.values():
        action["events"].sort(key=lambda item: str(item.get("occurred_at") or ""))
    actions = [by_index[index] for index in sorted(by_index)]
    event_count = sum(action["event_count"] for action in actions) + len(unattributed)
    payload = {
        "schema_version": "recording-data-trace.v1",
        "available": True,
        "status": "matched" if any(action["event_count"] for action in actions) else "empty",
        "recording_id": recording_id,
        "app": app,
        "query_delay_s": query_delay_s,
        "queried_at": now_iso(),
        "window": result.get("window"),
        "identity": {
            "field": identity_field,
            "masked": (((result.get("attributes") or {}).get("identity") or {}).get("masked")),
        },
        "action_count": len(actions),
        "event_count": event_count,
        "possibly_truncated": bool((result.get("summary") or {}).get("possibly_truncated")),
        "actions": actions,
        "unattributed_events": unattributed,
    }
    _write_json(directory / DATA_TRACE_NAME, payload)
    return payload


def _script_from_freeform(text: str) -> dict[str, Any] | None:
    lines = [re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
             for line in str(text or "").splitlines()]
    steps = [{"id": f"s{i}", "text": line, "expect": ""} for i, line in enumerate(lines, start=1) if line]
    if not steps:
        return None
    return {"source": "freeform", "case_id": None, "case_revision": None, "steps": steps}


def build_script_snapshot(
    app: str,
    script: dict[str, Any] | None,
    *,
    root: Path = ROOT,
) -> dict[str, Any] | None:
    """Normalize a recording script binding into a durable trace/header snapshot."""
    if not isinstance(script, dict):
        return None
    case_id = str(script.get("case_id") or "").strip()
    freeform = str(script.get("freeform") or "").strip()
    if case_id:
        from autophone.record.case_recipe import snapshot
        profile = load_app_profile(app, root=root)
        case = next((c for c in load_cases(profile.cases_path()) if str(c.get("id")) == case_id), None)
        if case is None:
            raise RecordingError(f"case not found: {case_id}")
        raw_steps = case.get("steps") if isinstance(case.get("steps"), list) else []
        raw_expects = case.get("expects") if isinstance(case.get("expects"), list) else []
        steps: list[dict[str, str]] = []
        for i, text in enumerate(raw_steps, start=1):
            t = str(text or "").strip()
            if not t:
                continue
            expect = str(raw_expects[i - 1] or "").strip() if i - 1 < len(raw_expects) else ""
            steps.append({"id": f"s{i}", "text": t, "expect": expect})
        if not steps:
            desc = str(case.get("desc") or case.get("test_point") or case.get("item") or "").strip()
            if desc:
                steps.append({"id": "s1", "text": desc, "expect": ""})
        if not steps:
            raise RecordingError(f"case {case_id} has no steps to snapshot")
        return {
            "source": "case",
            "case_id": case_id,
            "case_revision": case.get("revision"),
            "title": str(case.get("desc") or case.get("test_point") or case.get("item") or ""),
            "steps": steps,
            "case_snapshot": snapshot(app, case),
        }
    if freeform:
        return _script_from_freeform(freeform)
    return None


def _script_texts(steps: Any) -> list[str]:
    if not isinstance(steps, list):
        return []
    return [str(s.get("text") or "").strip() for s in steps if isinstance(s, dict) and str(s.get("text") or "").strip()]


def _case_script_with_review(base: dict[str, Any], reviewed: dict[str, Any] | None) -> dict[str, Any]:
    """把审查页的说明文案并进一个按用例绑定的剧本快照，不丢绑定与 expects。

    - 文案为空：不视为解除绑定，原快照原样保留（解除绑定须显式操作，当前未提供入口）。
    - 文案与原用例步骤一致：回到原用例快照（去掉此前的修改标记）。
    - 文案有改动：``source``/``case_id``/``case_revision``/``title`` 保持；``steps`` 换成审查后的
      文案（对齐与回放说明以它为准）；原用例步骤整份保留在 ``case_steps``；``edited: true``。
      与原用例某步文字完全相同的行按原顺序带回该步的 ``expect`` 并记 ``case_step``，其余行
      ``expect`` 为空——不按位置猜，避免插行后把 expects 错配到别的步。
    """
    original = base.get("case_steps") if isinstance(base.get("case_steps"), list) else base.get("steps")
    original = [s for s in (original or []) if isinstance(s, dict)]
    binding = {k: base[k] for k in ("source", "case_id", "case_revision", "title") if k in base}
    pristine = {**binding, "steps": original}
    if reviewed is None:
        return base
    reviewed_texts = _script_texts(reviewed.get("steps"))
    if reviewed_texts == _script_texts(original):
        return pristine
    if reviewed_texts == _script_texts(base.get("steps")):
        return base
    steps: list[dict[str, Any]] = []
    cursor = 0
    for row in reviewed.get("steps") or []:
        text = str(row.get("text") or "").strip()
        item: dict[str, Any] = {"id": str(row.get("id") or f"s{len(steps) + 1}"), "text": text, "expect": ""}
        hit = next(
            (k for k in range(cursor, len(original)) if str(original[k].get("text") or "").strip() == text),
            None,
        )
        if hit is not None:
            item["expect"] = str(original[hit].get("expect") or "")
            item["case_step"] = str(original[hit].get("id") or "")
            cursor = hit + 1
        steps.append(item)
    return {**binding, "steps": steps, "edited": True, "case_steps": original}


def resolve_review_script(
    app: str,
    directory: Path,
    script: dict[str, Any] | None,
    *,
    root: Path = ROOT,
) -> dict[str, Any] | None:
    """审查阶段（lift-preview / land）请求里的剧本 → 应生效的剧本快照。

    录制发起时的剧本来源是事实：已按用例绑定的录制，审查请求只带自由文案（或带同一
    case_id）时，服务端保留用例绑定，只把文案改动并进快照；不信任前端是否回传了
    case_id。请求带另一个 case_id 视为显式改绑，按用例库重新快照。未绑定用例的录制
    维持原行为：自由文案即快照，空文案即无剧本。``script`` 为 None 时沿用已存快照。
    """
    trace = _trace_with_script(directory) or {}
    existing = trace.get("script") if isinstance(trace.get("script"), dict) else None
    if existing is None:
        meta_script = (_read_json(directory / META_NAME) or {}).get("script")
        existing = meta_script if isinstance(meta_script, dict) else None
    if script is None:
        return existing
    case_id = str(script.get("case_id") or "").strip()
    freeform = str(script.get("freeform") or "").strip()
    reviewed = _script_from_freeform(freeform) if freeform else None
    bound = existing if (existing or {}).get("source") == "case" and existing.get("case_id") else None
    if bound is not None and (not case_id or case_id == str(bound.get("case_id"))):
        return _case_script_with_review(bound, reviewed)
    if case_id:
        base = build_script_snapshot(app, {"case_id": case_id}, root=root)
        return _case_script_with_review(base, reviewed) if base is not None else None
    return reviewed


#: Web 审查路径做页分类时认的 screen 状态，与 CLI ``--screen-statuses`` 默认值、回放期
#: checkpoint 的默认状态集一致：只有 active 签名参与匹配并写进 ``screen_expectations``。
SCREEN_MATCH_STATUSES: tuple[str, ...] = ("active",)


def resolve_screen_context(
    app: str,
    directory: Path,
    *,
    root: Path = ROOT,
) -> tuple[Path | None, dict[str, Any]]:
    """为 Web 提升/落库找该录制可用的屏骨架资产，返回 ``(资产路径或 None, 说明)``。

    路径规则沿用 ``AppProfile.screens_path(build)``（``apps/<app>/screens/<build>.yaml``）。
    build 取录制自身的 ``build``（trace 优先，其次 meta），且必须等于 App profile 当前的
    ``build_label``：回放期 checkpoint 按当前 build 的资产判定，两者不一致时带上旧 build
    的 screen_id 会让回放误判，故不带。屏骨架是可选先验：任何一种情况都不让提升失败，
    说明里的 ``status`` 写明带或不带的原因——

    - ``used``：资产有效且至少有一个 active screen，路径交给 ``lift_trace``；
    - ``build_unpinned``：录制或 profile 没有 build 标签；
    - ``build_mismatch``：录制 build 不是当前 build；
    - ``absent``：当前 build 没有资产文件；
    - ``invalid``：资产文件加载或校验失败；
    - ``no_active``：资产有效但没有 active screen（draft 不参与匹配）；
    - ``unavailable``：无法读取 App profile。
    """
    meta = _read_json(directory / META_NAME) or {}
    trace = _read_json(directory / "trace.json") or {}
    build = str(trace.get("build") or meta.get("build") or "").strip()
    context: dict[str, Any] = {
        "status": "unavailable",
        "build": build or None,
        "statuses": list(SCREEN_MATCH_STATUSES),
    }
    try:
        profile = load_app_profile(app, root=root)
        current = str(getattr(profile, "build_label", "") or "").strip()
        context["profile_build"] = current or None
        if not build or not current:
            context.update(status="build_unpinned", reason="录制或 App profile 没有 build 标签")
            return None, context
        if build != current:
            context.update(status="build_mismatch", reason=f"录制 build {build} 不是当前 build {current}")
            return None, context
        path = Path(profile.screens_path(build))
    except Exception as exc:  # noqa: BLE001 - 屏骨架是可选先验，profile 读不了只说明原因
        context["reason"] = f"{type(exc).__name__}: {exc}"[:240]
        return None, context
    context["path"] = _display_path(path, root=root)
    if not path.exists():
        context.update(status="absent", reason="当前 build 没有屏骨架资产")
        return None, context
    from autophone.assets import screens as screen_assets

    try:
        doc = screen_assets.load_screen_asset(path)
    except Exception as exc:  # noqa: BLE001 - YAML/schema/IO 失败都按资产无效处理
        context.update(status="invalid", reason=f"{type(exc).__name__}: {exc}"[:240])
        return None, context
    counts: dict[str, int] = {}
    for screen in doc.get("screens") or []:
        status = str(screen.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    context["screen_counts"] = counts
    if not any(counts.get(status) for status in SCREEN_MATCH_STATUSES):
        context.update(status="no_active", reason="资产里没有 active screen，draft 签名不参与匹配")
        return None, context
    context["status"] = "used"
    return path, context


def _screen_context_summary(
    context: dict[str, Any],
    lifted: dict[str, Any] | None = None,
    *,
    expectations: dict[int, str] | None = None,
    starts_with: str | None = None,
) -> dict[str, Any]:
    """在资产说明上补本次实际分类结果的计数，供审查页如实展示（不展示就等于假装带了）。"""
    out = dict(context)
    if context.get("status") != "used":
        return out
    lifted = lifted or {}
    out["matched_events"] = len(lifted.get("screen_matches") or [])
    out["conflicts"] = list(lifted.get("screen_conflicts") or [])
    if expectations is None:
        expectations = lifted.get("screen_expectations") or {}
        starts_with = lifted.get("screen_starts_with")
    out["checkpoints"] = len(expectations) + (1 if starts_with else 0)
    return out


def _display_path(path: Path, *, root: Path) -> str:
    for base in (Path(root), APP_ASSET_ROOT, _state_root(root)):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def _before_screens(matches: list[dict[str, Any]] | None) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in matches or []:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and row.get("screen_id"):
            out[index] = str(row["screen_id"])
    return out


def step_screen_sidecar(
    before_screens: dict[int, str],
    step_events: list[int | None] | None,
    step_count: int,
    *,
    start_prefixed: bool,
) -> tuple[str | None, dict[int, str]]:
    """按「事件执行前在哪一页」推出落库用的 ``screen_starts_with`` 与按 **step 下标**的
    ``screen_expectations``。

    ``lift_trace`` 的 ``screen_expectations`` 按事件下标记，但落库后的 steps 可能合并、删减，
    还会在最前面补一条起始复位算子；回放按 step 下标查（``operation_run``：第 i 步执行后读
    键 i-1）。这里用 ``step_events``（每个 step 对应的最后一个录制事件）换算：step p 对应事件
    e，且事件 e 与 e+1 执行前的页都识别出来并且不同，则 step p 执行后应在事件 e+1 的页。

    补了起始复位时，录制的第一帧就是复位之后的页，所以它成为复位那一步之后的 checkpoint，
    不写 ``screen_starts_with``（复位前的屏不确定）。映射缺失或长度不符时不产出任何 checkpoint。
    """
    if not before_screens or step_events is None or len(step_events) != step_count:
        return None, {}
    expectations: dict[int, str] = {}
    first = before_screens.get(0)
    starts_with: str | None = None
    if start_prefixed:
        if first and step_count and step_events[0] is None:
            expectations[0] = first
    else:
        starts_with = first
    for position, event in enumerate(step_events):
        if event is None:
            continue
        current = before_screens.get(event)
        nxt = before_screens.get(event + 1)
        if current and nxt and current != nxt:
            expectations[position] = nxt
    return starts_with, expectations


def _trace_with_script(directory: Path) -> dict[str, Any] | None:
    trace = _read_json(directory / "trace.json")
    if trace is None:
        return None
    if not isinstance(trace.get("script"), dict):
        meta = _read_json(directory / META_NAME) or {}
        script = meta.get("script")
        if isinstance(script, dict):
            trace = dict(trace)
            trace["script"] = script
    return trace


def _persist_script_snapshot(
    directory: Path,
    script: dict[str, Any] | None,
) -> None:
    """Persist the reviewed post-recording script to both durable headers."""
    meta = _read_json(directory / META_NAME) or {}
    trace = _read_json(directory / "trace.json")
    if script is None:
        meta.pop("script", None)
        if trace is not None:
            trace.pop("script", None)
    else:
        meta["script"] = script
        if trace is not None:
            trace["script"] = script
    _write_json(directory / META_NAME, meta)
    if trace is not None:
        _write_json(directory / "trace.json", trace)


def resolve_recording_id(recording_id: str, *, root: Path = ROOT) -> Path:
    rid = str(recording_id or "").strip()
    if not rid:
        raise RecordingError("recording_id is required")
    if "\x00" in rid:
        raise RecordingError("recording_id contains NUL")
    state_root = _state_root(root)
    p = (state_root / rid).resolve()
    rec_root = (state_root / "runs" / "rec").resolve()
    try:
        p.relative_to(rec_root)
    except ValueError as exc:
        raise RecordingError("recording_id must resolve under runs/rec") from exc
    return p


def _trace_info(directory: Path) -> tuple[dict[str, Any] | None, int, str | None]:
    trace_path = directory / "trace.json"
    if not trace_path.exists():
        return None, 0, None
    try:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, 0, f"{type(exc).__name__}: {exc}"
    events = trace.get("events")
    return trace, len(events) if isinstance(events, list) else 0, None


def _event_summaries(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Compact, replay-faithful summary of captured trace events for the list view.

    Keep only what the card needs to render「已采集步骤」(kind + preview/text/手势
    端点 + 截图名 + 时戳)。Index is positional so the front-end can align each
    captured step with the lifted operation step (1:1, lifter is index-preserving)."""
    events = (trace or {}).get("events")
    if not isinstance(events, list):
        return []
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        summary: dict[str, Any] = {"index": i, "kind": kind, "t": ev.get("t")}
        if ev.get("label"):
            summary["label"] = ev["label"]
        for shot in ("before_shot", "after_shot"):
            if ev.get(shot):
                summary[shot] = ev[shot]
        if kind == "tap":
            summary["preview"] = ev.get("preview")
        elif kind == "swipe":
            summary["from_preview"] = ev.get("from_preview")
            summary["to_preview"] = ev.get("to_preview")
            summary["ms"] = ev.get("ms")
        elif kind == "type":
            summary["text"] = ev.get("text")
        elif kind == "key":
            summary["name"] = ev.get("name")
        out.append(summary)
    return out


def _tail_log(directory: Path, limit: int = 2400) -> str | None:
    path = directory / "recorder.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text[-limit:]


def _operation_rows(app: str, *, root: Path = ROOT) -> list[dict[str, Any]]:
    profile = load_app_profile(app, root=root)
    path = profile.operations_path()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
    rows = doc.get("operations") or []
    return [
        {
            **op,
            "has_executor": op_run.has_builtin_executor(str(op.get("name") or ""), app=app)
            or op_run.is_typed_ir_steps(op.get("steps")),
        }
        for op in rows
    ]


def _draft_for(app: str, recording_id: str, *, root: Path = ROOT) -> dict[str, Any] | None:
    for op in _operation_rows(app, root=root):
        if (op.get("provenance") or {}).get("from") == recording_id:
            return {
                "id": op.get("id"),
                "name": op.get("name"),
                "summary": op.get("summary"),
                "status": op.get("status"),
                "revision": op.get("revision"),
                "has_executor": op.get("has_executor"),
                "step_count": len(op.get("steps") or []),
            }
    return None


def _operation_run_records_for_draft(
    app: str,
    draft_id: str | None,
    *,
    root: Path = ROOT,
    limit: int = 20,
) -> list[dict[str, Any]]:
    did = str(draft_id or "").strip()
    if not did:
        return []
    records: list[dict[str, Any]] = []
    for op_path in (_state_root(root) / "runs" / "platform").glob("*/*/operation-result.json"):
        operation = _read_json(op_path) or {}
        if str(operation.get("draft_id") or "") != did:
            continue
        run_dir = op_path.parent
        platform = _read_json(run_dir / "platform-run-result.json") or {}
        if platform.get("type") not in (None, "operation"):
            continue
        if platform.get("app") not in (None, app):
            continue
        try:
            replay_speed = float(operation.get("replay_speed") or 1.0)
        except (TypeError, ValueError):
            replay_speed = 1.0
        telemetry = _read_json(run_dir / "planes" / "telemetry" / "events.json") or {}
        records.append(
            {
                "run_id": platform.get("run_id") or run_dir.name,
                "status": operation.get("status") or platform.get("status"),
                "review_status": operation.get("review_status"),
                "ended_at": (platform.get("timestamps") or {}).get("collect_ended_at"),
                "replay_speed": replay_speed,
                "data_trace": {
                    "available": bool(telemetry),
                    "status": telemetry.get("status") or (
                        "waiting" if not (run_dir / "platform-run-result.json").exists() else "not_queried"
                    ),
                    "event_count": int(telemetry.get("event_count") or 0),
                },
            }
        )
    records.sort(key=lambda item: str(item.get("ended_at") or item.get("run_id") or ""), reverse=True)
    return records[:limit]


def _iter_recording_dirs(app: str | None, *, root: Path = ROOT) -> list[Path]:
    rec_root = _state_root(root) / "runs" / "rec"
    if not rec_root.exists():
        return []
    dirs: set[Path] = set()
    for trace in rec_root.glob("**/trace.json"):
        dirs.add(trace.parent)
    for meta in rec_root.glob(f"**/{META_NAME}"):
        dirs.add(meta.parent)
    return sorted(dirs, key=lambda p: str(p.relative_to(rec_root)))


def list_recordings(app: str, *, root: Path = ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for directory in _iter_recording_dirs(app, root=root):
        recording_id = _repo_rel(directory, root=root)
        meta = _read_json(directory / META_NAME) or {}
        _raw_trace, event_count, trace_error = _trace_info(directory)
        trace = _trace_with_script(directory)
        session = _read_json(directory / "session.json") if meta.get("surface") in {"web", "native"} else None
        if trace is None and session is not None:
            partial = _read_json(directory / "trace.partial.json")
            if partial is not None:
                trace = partial
                events = partial.get("events")
                event_count = len(events) if isinstance(events, list) else 0
        row_app = str((trace or {}).get("app") or meta.get("app") or "")
        if row_app and row_app != app:
            continue
        if not row_app:
            # Historical row with missing app metadata: keep it visible for the
            # selected app only when an operation provenance already links to it.
            linked = _draft_for(app, recording_id, root=root)
            if linked is None:
                continue
        else:
            linked = _draft_for(app, recording_id, root=root)
        build = str((trace or {}).get("build") or meta.get("build") or "")
        started_at = str(meta.get("started_at") or "")
        starts_with = ((trace or {}).get("start") or {}).get("starts_with") or meta.get("starts_with")
        shots = [p.name for p in sorted(directory.glob("shot_*.jpg")) if p.is_file()]
        thumbnail = shots[0] if shots else None
        pid = meta.get("pid")
        alive = _pid_alive(pid)
        web_state = str((session or {}).get("state") or "")
        if linked is not None:
            status = "landed"
        elif session is not None and web_state in {"starting", "live", "finishing", "cancelling"}:
            # cancelling = 取消已受理，等在飞的 starts_with 复位落地后由发起方删目录。
            status = "recording"
        elif session is not None and web_state in {"faulted", "interrupted"}:
            status = web_state
        elif session is not None and web_state == "failed":
            status = "failed"
        elif trace_error or ((directory / "trace.json").exists() and event_count == 0):
            status = "empty"
        elif (directory / "trace.json").exists():
            status = "captured"
        elif meta and alive:
            status = "recording"
        elif meta:
            status = "failed"
        else:
            status = "empty"
        row = {
            "recording_id": recording_id,
            "name": directory.name,
            "display_name": str(meta.get("display_name") or ""),
            "dir": str(directory),
            "app": row_app or app,
            "build": build,
            "started_at": started_at,
            "device": meta.get("device"),
            "event_count": event_count,
            "events": _event_summaries(trace),
            "script": trace.get("script") if isinstance((trace or {}).get("script"), dict) else meta.get("script"),
            "semantic_capture": (
                trace.get("semantic_capture")
                if isinstance((trace or {}).get("semantic_capture"), dict)
                else None
            ),
            "starts_with": starts_with,
            "shots": shots,
            "thumbnail": thumbnail,
            "status": status,
            "surface": meta.get("surface") or "desktop",
            "session_state": web_state or None,
            "last_error": (session or {}).get("last_error"),
            "landable": status == "captured",
            "draft": linked,
            "data_trace": _read_json(directory / DATA_TRACE_NAME) or {
                "schema_version": "recording-data-trace.v1",
                "available": False,
                "status": "not_queried",
                "action_count": event_count,
                "event_count": 0,
                "actions": [],
                "unattributed_events": [],
            },
            "run_records": _operation_run_records_for_draft(
                app, str(linked.get("id") or "") if linked else None, root=root
            ),
        }
        if trace_error:
            row["error"] = trace_error
        if status == "failed":
            row["log_tail"] = _tail_log(directory)
        out.append(row)
    return out


def start_recording(
    app: str,
    *,
    build: str,
    starts_with: str | None,
    device: str,
    script: dict[str, Any] | None = None,
    root: Path = ROOT,
    launcher: Launcher = subprocess.Popen,
    surface: str = "desktop",
    web_manager: Any | None = None,
    lock: Any | None = None,
) -> dict[str, Any]:
    _validate_starts_with(app, starts_with, root=root)
    script_snapshot = build_script_snapshot(app, script, root=root)
    if surface in {"web", "native"}:
        if web_manager is None:
            raise RecordingError("web recording manager is required")
        return web_manager.start(
            app,
            build=build,
            starts_with=starts_with,
            device=device,
            script=script_snapshot,
            **({"surface": "native"} if surface == "native" else {}),
        )
    if surface != "desktop":
        raise RecordingError(f"unknown recording surface: {surface}")
    # 先拿锁再建目录：拿不到就什么都没发生（没有半个录制目录要收尾，也没有已经起来
    # 的窗口要杀）。锁随 fd 进子进程，父子之间没有「已 spawn 但还没拿到锁」的窗口。
    state_root = _state_root(root)
    screen_lock = lock if lock is not None else ScreenLock(
        root=(
            default_screen_lock_root()
            if root == APP_ASSET_ROOT
            else state_root / "runs" / ".locks"
        ),
    )
    lease = screen_lock.try_lease(
        device=device, owner_kind="desktop_recording", owner_id=f"{app}:{build or 'recording'}",
    )
    if lease is None:
        owner = screen_lock.live_status(device=device)
        raise DeviceBusy(
            f"设备 {device} 正被 {owner.get('owner_kind') or 'workflow'} "
            f"{owner.get('owner_id') or owner.get('run_id') or '?'} 占用，不能开始录制")
    ts = now_iso().replace(":", "").replace("-", "").replace("+", "-").split(".")[0]
    name = f"{ts}-{_safe_slug(build or 'recording')}"
    directory = state_root / "runs" / "rec" / app / name
    directory.mkdir(parents=True, exist_ok=False)
    meta = {
        "app": app,
        "build": build,
        "starts_with": starts_with,
        "started_at": now_iso(),
        "pid": None,
        "device": device,
        "surface": "desktop",
    }
    if script_snapshot:
        meta["script"] = script_snapshot
    _write_json(directory / META_NAME, meta)
    log = (directory / "recorder.log").open("ab")
    cmd = [
        sys.executable,
        "-m",
        "autophone.observation.record_capture",
        "--out",
        str(directory),
        "--app",
        app,
        "--build",
        build,
        "--registry-root",
        str(Path(root).resolve()),
    ]
    if starts_with:
        cmd += ["--starts-with", starts_with]
    recording_id = _repo_rel(directory, root=root)
    fd = lease.fileno()
    cmd += ["--lease-fd", str(fd), "--lease-device", device, "--lease-id", recording_id,
            "--lease-root", str(screen_lock.root)]
    try:
        proc = launcher(cmd, cwd=str(root), stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, pass_fds=(fd,))
    except BaseException:
        lease.release()
        raise
    finally:
        log.close()
    # spawn 之后才交接：子进程已经继承了同一个打开文件描述，父进程关掉自己那份，
    # 锁留在录制窗口手里，随它退出（哪怕是被杀）由 OS 释放。
    lease.detach()
    pid = int(getattr(proc, "pid", 0) or 0)
    meta["pid"] = pid
    _write_json(directory / META_NAME, meta)
    return {"recording_id": recording_id, "dir": str(directory), "pid": pid}


def _terminate_process_group(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        raise
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return


def cancel_recording(
    app: str,
    recording_id: str,
    *,
    root: Path = ROOT,
    terminate: Terminator = _terminate_process_group,
) -> dict[str, Any]:
    directory = resolve_recording_id(recording_id, root=root)
    if not directory.exists():
        raise FileNotFoundError(recording_id)
    meta = _read_json(directory / META_NAME) or {}
    if meta.get("surface") in {"web", "native"}:
        raise RecordingError("Web 录制必须通过 session/cancel 并携带 controller token")
    trace, _, _ = _trace_info(directory)
    row_app = str((trace or {}).get("app") or meta.get("app") or "")
    if row_app and row_app != app:
        raise FileNotFoundError(recording_id)
    if trace is not None:
        raise RecordingError("录制已收口，请使用删除录制")
    try:
        pid = int(meta.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if _pid_alive(pid):
        try:
            terminate(pid)
        except OSError as exc:
            if _pid_alive(pid):
                raise RecordingError(f"取消录制失败：{exc}") from exc
    shutil.rmtree(directory)
    return {"cancelled": recording_id, "device": meta.get("device"), "pid": pid}


def delete_recording(app: str, recording_id: str, *, root: Path = ROOT) -> dict[str, Any]:
    directory = resolve_recording_id(recording_id, root=root)
    rows = list_recordings(app, root=root)
    row = next((r for r in rows if r["recording_id"] == recording_id), None)
    if row is None:
        raise FileNotFoundError(recording_id)
    if row.get("status") == "recording":
        raise RecordingError("录制仍在进行中，请先关闭录制窗口再删除")
    if row.get("surface") in {"web", "native"} and row.get("session_state") in {
        "starting", "live", "finishing", "cancelling", "faulted", "interrupted",
    }:
        raise RecordingError("Web 录制会话尚未收口，请通过 session/finish 或 session/cancel 处理")
    draft = row.get("draft")
    # 只有 active/archived 等已转正的操作才阻止删除录制；draft 状态允许删除（试验阶段可随时清理）
    if draft and draft.get("status") not in ("draft", "deprecated"):
        raise RecordingError(f"录制已关联 {draft.get('status')} 操作 {draft.get('id')}，请先废弃该操作再删除录制")
    detached = None
    if draft and draft.get("id"):
        # 先标记再删目录：证据链断了这件事必须写在操作自己身上，否则留下一条指向空路径的
        # draft——回放、重提升、逐步复核全断，面板上却看不出任何异常。
        detached = _detach_draft(app, str(draft.get("id")), recording_id, root=root)
    shutil.rmtree(directory)
    return {"deleted": recording_id, "device": row.get("device"), "detached_operation": detached}


def _detach_draft(
    app: str, op_id: str, recording_id: str, *, root: Path = ROOT,
) -> dict[str, Any] | None:
    from autophone.assets import operations_store

    path = load_app_profile(app, root=root).operations_path()
    if not path.exists():
        return None
    try:
        item = operations_store.detach_operation_source(
            path, op_id, reason=f"来源录制 {recording_id} 已删除，证据链不可达",
        )
    except KeyError:
        return None
    return {"id": op_id, "status": item.get("status"), "detached": True}


def update_recording(app: str, recording_id: str, changes: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    directory = resolve_recording_id(recording_id, root=root)
    if not directory.exists():
        raise FileNotFoundError(recording_id)
    allowed = {"display_name"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise RecordingError(f"unknown field(s): {unknown}")
    if not changes:
        raise RecordingError("no fields to update")
    meta = _read_json(directory / META_NAME) or {}
    trace, _, _ = _trace_info(directory)
    row_app = str((trace or {}).get("app") or meta.get("app") or "")
    if row_app and row_app != app:
        raise FileNotFoundError(recording_id)
    if "display_name" in changes:
        value = str(changes.get("display_name") or "").strip()
        if len(value) > 120:
            raise RecordingError("display_name too long")
        if value:
            meta["display_name"] = value
        else:
            meta.pop("display_name", None)
    meta.setdefault("app", app)
    _write_json(directory / META_NAME, meta)
    return {"recording_id": recording_id, "display_name": meta.get("display_name") or ""}


def lift_preview_thunk(
    app: str,
    recording_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    script: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    root: Path = ROOT,
):
    """提升预览（不落库）：跑 lift_trace_file，回 {steps, decisions, summary}。

    落库前逐步审查走这条——AI 只在这一步在环（创建期），人审完拿编辑后的 steps 再走 land。
    decisions 已带每个 tap 的落点 preview + before_shot，前端据此画红圈、给「保留坐标 / 改文字 /
    文字+region」三选一。结果会缓存到 lift_preview_cache.json，下次打开可直接使用。"""
    directory = resolve_recording_id(recording_id, root=root)

    def _run() -> dict[str, Any]:
        import json
        from autophone.record.lift import LIFT_PREVIEW_VERSION, lift_trace

        cache_file = directory / "lift_preview_cache.json"
        trace = _trace_with_script(directory)
        if trace is None:
            raise RecordingError("trace.json missing or invalid")
        if script is not None:
            script_snapshot = resolve_review_script(app, directory, script, root=root)
            trace = dict(trace)
            if script_snapshot is None:
                trace.pop("script", None)
            else:
                trace["script"] = script_snapshot
        screen_path, screen_context = resolve_screen_context(app, directory, root=root)
        screen_kwargs: dict[str, Any] = {}
        if screen_path is not None:
            screen_kwargs = {
                "screen_asset_path": screen_path,
                "screen_statuses": SCREEN_MATCH_STATUSES,
                "screen_conflicts": "record",
            }
        result = lift_trace(
            trace,
            directory,
            provider=provider,
            model=model,
            progress=progress,
            root=root,
            **screen_kwargs,
        )
        result = {**result, "screen_context": _screen_context_summary(screen_context, result)}
        if result.get("script_alignment"):
            _write_json(directory / "alignment.json", result["script_alignment"])

        # 缓存结果到录制目录（包含 provider/model 信息）
        cache_data = {
            "lift_version": LIFT_PREVIEW_VERSION,
            "result": result,
            "provider": provider,
            "model": model,
            "script": trace.get("script") if isinstance(trace.get("script"), dict) else None,
            "timestamp": __import__("time").time(),
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # ai_jobs._normalize 以 contract["ok"] 判 job 成败——lift 输出本身没有 ok 键，
        # 不显式置 True 会被当成 ok=false 标 failed、前端进不了审查（lift 抛错走 except→failed）。
        return {"ok": True, **result}

    return _run


def lift_preview_step_thunk(
    app: str,
    recording_id: str,
    *,
    index: int,
    provider: str | None = None,
    model: str | None = None,
    root: Path = ROOT,
):
    """Retry one recorded event through the same lifter used by full preview."""
    directory = resolve_recording_id(recording_id, root=root)

    def _run() -> dict[str, Any]:
        from autophone.record.lift import lift_trace_event

        trace = _read_json(directory / "trace.json")
        if trace is None:
            raise RecordingError("trace.json missing or invalid")
        result = lift_trace_event(
            trace,
            directory,
            index,
            provider=provider,
            model=model,
            root=root,
        )
        return {"ok": True, **result}

    return _run


def land_recorded_steps(app: str, recording_id: str, *, name: str, root: Path = ROOT):
    from autophone.record.lift import LiftError, recorded_steps

    directory = resolve_recording_id(recording_id, root=root)
    trace = _trace_with_script(directory)
    if trace is None or trace.get("app") != app:
        raise RecordingError("recording trace does not belong to this App")
    try:
        steps = recorded_steps(trace)
    except (LiftError, TypeError, ValueError) as exc:
        raise RecordingError(str(exc)) from exc
    return land_recording_thunk(
        app, recording_id, op_name=name, summary=name,
        steps=steps, step_events=list(range(len(steps))), root=root,
    )()


def land_recording_thunk(
    app: str,
    recording_id: str,
    *,
    op_name: str,
    summary: str,
    provider: str | None = None,
    model: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    step_events: list[int | None] | None = None,
    script_alignment: dict[str, Any] | None = None,
    script: dict[str, Any] | None = None,
    root: Path = ROOT,
):
    """落库为 operation 草稿。

    `steps` 给定（落库前审查通过的编辑结果）→ **直接落库，不再跑 AI 提升**；这些 steps 仍过
    land_lifted_operation 的 validate_recipe + 整库 operation.v1 兜底（fail-closed）。`steps` 省略
    → 老路径：先 lift_trace_file 再落库（一次性提升+落库，无人工审查）。

    录制已关联 draft：就地更新（revision++），复用 id/provenance；active/archived 拒绝（须先废弃）。
    无关联：append 新 draft。"""
    directory = resolve_recording_id(recording_id, root=root)
    linked = _draft_for(app, recording_id, root=root)
    op_name = normalize_operation_name(op_name)
    script_snapshot = resolve_review_script(app, directory, script, root=root) if script is not None else None

    def _steps_with_start(
        steps: list[dict[str, Any]], mapping: list[int | None] | None,
    ) -> tuple[list[dict[str, Any]], list[int | None] | None]:
        """在 steps 前补上录制声明的起始复位算子（回放要从同一个起点出发）。

        映射同步补一个 None：这一步不是录出来的，没有对应的人手停顿。少了这一步，steps 会比
        trace 事件多一个，回放的 timing 对齐就永远失配（原速回放静默退化成均匀节奏）。
        """
        trace = _read_json(directory / "trace.json") or {}
        starts_with = ((trace.get("start") or {}).get("starts_with") or "").strip()
        if not starts_with:
            return steps, mapping
        start_step = {"operation": starts_with}
        if steps and steps[0] == start_step:
            return steps, mapping
        return [start_step, *steps], ([None, *mapping] if mapping is not None else None)

    def _run() -> dict[str, Any]:
        from autophone.record import lift as record_lift
        from autophone.record.lift import (
            AlignmentError,
            land_lifted_operation,
            lift_trace,
            update_lifted_operation,
            validate_script_alignment,
        )

        final_steps = steps
        final_step_events = step_events
        alignment = script_alignment
        effective_script = script_snapshot
        screen_path, screen_context = resolve_screen_context(app, directory, root=root)
        screen_matches: dict[str, Any] = {}
        if final_steps is None:
            trace = _trace_with_script(directory)
            if trace is None:
                raise RecordingError("trace.json missing or invalid")
            if script is not None:
                trace = dict(trace)
                if script_snapshot is None:
                    trace.pop("script", None)
                else:
                    trace["script"] = script_snapshot
            screen_kwargs: dict[str, Any] = {}
            if screen_path is not None:
                screen_kwargs = {
                    "screen_asset_path": screen_path,
                    "screen_statuses": SCREEN_MATCH_STATUSES,
                    "screen_conflicts": "record",
                }
            lifted = lift_trace(
                trace,
                directory,
                provider=provider,
                model=model,
                root=root,
                **screen_kwargs,
            )
            final_steps = lifted["steps"]
            final_step_events = lifted.get("step_events")
            screen_matches = {
                "matches": lifted.get("screen_matches") or [],
                "conflicts": lifted.get("screen_conflicts") or [],
            }
            alignment = lifted.get("script_alignment")
            effective_script = trace.get("script") if isinstance(trace.get("script"), dict) else None
        else:
            if effective_script is None and script is None:
                trace = _trace_with_script(directory) or {}
                effective_script = trace.get("script") if isinstance(trace.get("script"), dict) else None
            if screen_path is not None:
                # 审查后的 steps 不再跑 lift；页分类由宿主按当前资产重做（OCR，不调 AI），
                # 不采信浏览器回传的 screen 信息。
                raw_trace = _read_json(directory / "trace.json") or {}
                try:
                    screen_matches = record_lift.classify_trace_screens(
                        raw_trace,
                        directory,
                        screen_asset_path=screen_path,
                        screen_statuses=SCREEN_MATCH_STATUSES,
                    )
                except Exception as exc:  # noqa: BLE001 - 屏骨架是可选先验，失败照常落库
                    screen_context = {
                        **screen_context,
                        "status": "invalid",
                        "reason": f"{type(exc).__name__}: {exc}"[:240],
                    }
        if alignment and effective_script:
            review = alignment.get("review") if isinstance(alignment.get("review"), dict) else None
            try:
                alignment = validate_script_alignment(
                    alignment,
                    script=effective_script,
                    decision_count=len(final_steps),
                )
            except AlignmentError as exc:
                raise RecordingError(f"说明与动作关联无效：{exc}") from exc
            if review and review.get("status") == "confirmed":
                alignment["review"] = {
                    "status": "confirmed",
                    "source": str(review.get("source") or "web")[:32],
                    "confirmed_at": now_iso(),
                }
        final_steps, final_step_events = _steps_with_start(final_steps, final_step_events)
        screen_starts_with, screen_expectations, screen_context = _land_screen_sidecar(
            directory, final_steps, final_step_events, screen_context, screen_matches,
        )

        # 录制已关联 draft → 就地更新；否则 append 新 draft
        if linked is not None:
            draft_id = str(linked.get("id") or "")
            if not draft_id:
                raise RecordingError("录制已关联 draft 但 id 缺失，数据损坏")
            status = str(linked.get("status") or "").strip()
            if status in ("active", "archived"):
                raise RecordingError(f"操作已转正（status={status}），请先废弃再重新提升")
            result = update_lifted_operation(
                app,
                draft_id,
                op_name,
                summary,
                final_steps,
                screen_starts_with=screen_starts_with,
                screen_expectations=screen_expectations,
                script_alignment=alignment,
                from_ref=recording_id,
                step_events=final_step_events,
                provider=provider,
                model=model,
                root=root,
            )
        else:
            result = land_lifted_operation(
                app,
                op_name,
                summary,
                final_steps,
                screen_starts_with=screen_starts_with,
                screen_expectations=screen_expectations,
                script_alignment=alignment,
                from_ref=recording_id,
                step_events=final_step_events,
                provider=provider,
                model=model,
                root=root,
            )
        if script is not None:
            _persist_script_snapshot(directory, script_snapshot)
        if alignment:
            _write_json(directory / "alignment.json", alignment)
        if isinstance(result, dict):
            result = {**result, "screen_context": screen_context}
        return result

    return _run


def _land_screen_sidecar(
    directory: Path,
    steps: list[dict[str, Any]],
    step_events: list[int | None] | None,
    context: dict[str, Any],
    classified: dict[str, Any],
) -> tuple[str | None, dict[int, str] | None, dict[str, Any]]:
    """落库前把页分类结果换算成按 step 下标的 sidecar，并把实际结果写回说明。"""
    from autophone.record.lift import normalized_step_events

    if context.get("status") != "used":
        return None, None, context
    trace = _read_json(directory / "trace.json") or {}
    event_count = len(trace.get("events") or [])
    mapping = normalized_step_events(step_events, len(steps))
    known = [e for e in (mapping or []) if e is not None]
    if mapping is None or any(e >= event_count for e in known) or known != sorted(set(known)):
        # 映射对不上就不写 checkpoint：错位的页期望会让回放在正确页面上判失败。
        summary = _screen_context_summary(context, {"screen_matches": classified.get("matches")},
                                          expectations={}, starts_with=None)
        summary["conflicts"] = list(classified.get("conflicts") or [])
        summary["reason"] = "step 与录制事件的映射缺失或不单调，未生成页级 checkpoint"
        return None, None, summary
    starts_with_op = str(((trace.get("start") or {}).get("starts_with") or "")).strip()
    start_prefixed = bool(starts_with_op) and bool(steps) and steps[0] == {"operation": starts_with_op}
    starts_with, expectations = step_screen_sidecar(
        _before_screens(classified.get("matches")), mapping, len(steps), start_prefixed=start_prefixed,
    )
    summary = _screen_context_summary(
        context,
        {"screen_matches": classified.get("matches"), "screen_conflicts": classified.get("conflicts")},
        expectations=expectations,
        starts_with=starts_with,
    )
    return starts_with, expectations or None, summary


def get_lift_preview_cache(recording_id: str, *, root: Path = ROOT) -> dict[str, Any] | None:
    """获取录制的提升预览缓存。无缓存 → None，有缓存 → {result, provider, model, timestamp}。"""
    try:
        from autophone.record.lift import LIFT_PREVIEW_VERSION

        directory = resolve_recording_id(recording_id, root=root)
        cache_file = directory / "lift_preview_cache.json"
        if not cache_file.exists():
            return None
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        result = cache.get("result") if isinstance(cache, dict) else None
        if not isinstance(result, dict) or result.get("lift_version") != LIFT_PREVIEW_VERSION:
            return None
        return cache
    except Exception:
        return None


def shot_path(recording_id: str, file: str, *, root: Path = ROOT) -> Path:
    directory = resolve_recording_id(recording_id, root=root)
    if file == "shot.jpg":
        first = next((p for p in sorted(directory.glob("shot_*.jpg")) if p.is_file()), None)
        if first is None:
            raise FileNotFoundError(directory / file)
        return first
    if not re.fullmatch(r"shot_[A-Za-z0-9._-]*\.jpg", file or ""):
        raise RecordingError("only shot_*.jpg is allowed")
    target = (directory / file).resolve()
    try:
        target.relative_to(directory.resolve())
    except ValueError as exc:
        raise RecordingError("shot path escapes recording directory") from exc
    if not target.exists():
        raise FileNotFoundError(target)
    return target


def _run_selftest() -> int:
    import tempfile

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rec = root / "runs" / "rec" / "food_ordering" / "cap"
        rec.mkdir(parents=True)
        (rec / "trace.json").write_text(json.dumps({"app": "food_ordering", "build": "b", "events": [{"kind": "tap"}]}), encoding="utf-8")
        empty = root / "runs" / "rec" / "food_ordering" / "empty"
        empty.mkdir(parents=True)
        (empty / "trace.json").write_text(json.dumps({"app": "food_ordering", "events": []}), encoding="utf-8")
        ops = root / "apps" / "food_ordering" / "operations" / "operations.yaml"
        ops.parent.mkdir(parents=True)
        ops.write_text(
            yaml.safe_dump({
                "operations": [{
                    "id": "OP1", "name": "cap", "status": "draft", "revision": 1,
                    "steps": [{"action": "tap_xy", "px": 1, "py": 2}],
                    "provenance": {"source": "conversation", "from": "runs/rec/food_ordering/cap"},
                }]
            }, allow_unicode=True),
            encoding="utf-8",
        )
        prof = root / "apps" / "food_ordering" / "profile.yaml"
        prof.write_text(
            "schema_version: v1\n"
            "app: food_ordering\n"
            "bundle_id: x\n"
            "display_name: x\n"
            "login:\n  method: manual\n"
            "reset: ui_home\n"
            "operations_dir: operations\n",
            encoding="utf-8",
        )
        rows = list_recordings("food_ordering", root=root)
        by = {r["name"]: r for r in rows}
        check(by["cap"]["status"] == "landed" and by["cap"]["draft"]["id"] == "OP1", "exact provenance links landed draft")
        check(by["empty"]["status"] == "empty" and by["empty"]["landable"] is False, "empty trace is not landable")
        try:
            resolve_recording_id("../escape", root=root)
            check(False, "path traversal rejected")
        except RecordingError:
            check(True, "path traversal rejected")
    print(f"\nselftest: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run_selftest())
