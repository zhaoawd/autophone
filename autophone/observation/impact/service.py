"""影响图谱的应用层门面：把 App Profile、run 目录和各层模块接起来。

CLI 与 HTTP API 都只调这一层，不直接拼路径——资产在哪、run 在哪、索引在哪，只有这里
知道一处。下层（endpoints / observations / bindings / index / planner / result）保持纯
函数与纯 IO，可以脱离 App Profile 单测。

计划、归因、索引和报告保持离线可跑。M2 执行入口只做编排：按 plan 调用注入的既有
``RunService.submit(type=operation)`` adapter；本模块不实现屏幕锁、设备准备或动作派发，
避免出现第二套设备执行器。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.execution import effects as effects_mod
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.execution.app_recipes import import_app_recipes
from autophone.observation.artifacts import (
    Artifact,
    append_artifacts,
    default_phase2_artifacts,
    write_artifact_manifest,
)
from autophone.contracts import validate_or_raise
from autophone.observation.impact import comparison
from autophone.observation.impact import bindings as bindings_mod
from autophone.observation.impact import context as ctx_mod
from autophone.observation.impact import execution as execution_mod
from autophone.observation.impact import index as index_mod
from autophone.observation.impact import manifest as manifest_mod
from autophone.observation.impact import observations as obs_mod
from autophone.observation.impact import planner as planner_mod
from autophone.observation.impact import policy as policy_mod
from autophone.observation.impact import report as report_mod
from autophone.observation.impact import result as result_mod
from autophone.observation.impact import runtime_evidence as runtime_evidence_mod
from autophone.assets.operations import load_operations
from autophone.util import write_json_atomic

PLAN_REL = "impact/plan.json"
EXECUTION_REL = "impact/execution.json"
CHANGE_MANIFEST_REL = "impact/change-manifest.json"
CONSUMER_CONTEXT_REL = obs_mod.CONSUMER_CONTEXT_REL


class ImpactError(ValueError):
    pass


def _register_impact_evidence(run_dir: Path, *, run_id: str | None = None) -> None:
    """把当前已存在的脱敏 impact 迁移事实登记进 Run Artifact Manifest。"""
    run_dir = Path(run_dir)
    artifacts = [
        item
        for item in default_phase2_artifacts(run_dir)
        if item.path in {
            obs_mod.OBSERVATIONS_REL,
            obs_mod.ACTION_MARKS_REL,
            obs_mod.CONSUMER_CONTEXT_REL,
        }
    ]
    if not artifacts:
        return
    manifest_path = run_dir / "artifact-manifest.json"
    if manifest_path.is_file():
        append_artifacts(run_dir, artifacts)
    else:
        write_artifact_manifest(run_dir, str(run_id or run_dir.name), artifacts)


def _profile(app: str | AppProfile, *, root: Path = APP_ASSET_ROOT) -> AppProfile:
    if isinstance(app, AppProfile):
        return app
    return load_app_profile(str(app), root=root)


def resolve_build(profile: AppProfile, build: str | None = None) -> str:
    resolved = build or profile.build_label
    if not resolved:
        raise ImpactError(
            f"app={profile.app} 没有 build_label，也没显式指定 --build："
            "影响图谱的资产和证据都是 build 维度的，缺 build 无法判 staleness"
        )
    return str(resolved)


def load_bindings(profile: AppProfile) -> dict[str, Any]:
    return bindings_mod.load(bindings_mod.bindings_path(profile))


def screen_inventory(profile: AppProfile, build: str) -> tuple[list[str], dict[str, list[str]]]:
    """screen 资产里的 screen id 与各自声明的 region 名。

    资产还没建（M1 之前）→ 返回空，表示「无法判断」；planner 与 index 于是不会因为
    「screen 不在资产里」去拦目标——拿缺失的资产当否定证据是错的。
    """
    try:
        path = profile.screens_path(build)
    except ValueError:
        return [], {}
    if not path.exists():
        return [], {}
    from autophone.assets.screens import load_screen_asset

    doc = load_screen_asset(path)
    ids: list[str] = []
    regions: dict[str, list[str]] = {}
    for screen in doc.get("screens") or []:
        if not isinstance(screen, dict) or not screen.get("id"):
            continue
        screen_id = str(screen["id"])
        ids.append(screen_id)
        regions[screen_id] = sorted(str(name) for name in (screen.get("regions") or {}))
    return ids, regions


def executor_effects(profile: AppProfile) -> dict[str, effects_mod.EffectDecision]:
    """加载并钉住当前 App 的 Python executor effect 注册表。"""
    root = getattr(profile, "root", APP_ASSET_ROOT)
    import_app_recipes(profile.app, root=root)
    return effects_mod.executor_effects_for_app(profile.app, root=root)


def operation_graph(profile: AppProfile) -> planner_mod.OperationGraph:
    operations = load_operations(profile)
    return planner_mod.OperationGraph.from_operations(
        operations,
        executor_effects=executor_effects(profile),
    )


# --------------------------------------------------------------------------
# observe
# --------------------------------------------------------------------------

def observe(
    run_dir: Path,
    *,
    app: str | None = None,
    build: str | None = None,
    run_id: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """一个 run 目录的四源证据 → observations.jsonl。"""
    run_dir = Path(run_dir)
    resolved_app = app
    resolved_build = build
    if resolved_app and not resolved_build:
        resolved_build = resolve_build(_profile(resolved_app))
    outcome = obs_mod.assemble_run(
        run_dir, run_id=run_id, app=resolved_app, build=resolved_build
    )
    payload: dict[str, Any] = {
        "run_dir": str(run_dir),
        "observations": len(outcome.observations),
        "stats": outcome.stats,
        "unattributed": outcome.unattributed,
    }
    if write:
        path = obs_mod.write_observations(run_dir, outcome.observations)
        payload["path"] = str(path)
        _register_impact_evidence(run_dir, run_id=run_id)
    return payload


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------

def rebuild_index(
    app: str | AppProfile,
    *,
    build: str | None = None,
    runs_root: Path | None = None,
    out_root: Path | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    profile = _profile(app, root=root)
    resolved_build = resolve_build(profile, build)
    screen_ids, regions = screen_inventory(profile, resolved_build)
    path, doc = index_mod.rebuild(
        app=profile.app,
        build=resolved_build,
        bindings_doc=load_bindings(profile),
        runs_root=(
            runs_root
            if runs_root is not None
            else profile.host_state_root / "runs"
        ),
        screen_ids=screen_ids,
        screen_regions=regions,
        out_root=(
            out_root
            if out_root is not None
            else profile.host_state_root
            / "autophone"
            / "_data"
            / "generated"
            / "impact-index"
        ),
    )
    return {"path": str(path), "index": doc}


def read_index(
    app: str | AppProfile,
    *,
    build: str | None = None,
    out_root: Path | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any] | None:
    profile = _profile(app, root=root)
    resolved_out_root = (
        out_root
        if out_root is not None
        else profile.host_state_root
        / "autophone"
        / "_data"
        / "generated"
        / "impact-index"
    )
    return index_mod.load_index(
        profile.app, resolve_build(profile, build), root=resolved_out_root
    )


# --------------------------------------------------------------------------
# 人审工作台
# --------------------------------------------------------------------------

def _review_region(name: str, value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, dict) else {"rect": value}
    return {
        "id": str(name),
        "display_name": str(raw.get("display_name") or name),
        "description": str(raw.get("description") or ""),
        "rect": list(raw.get("rect") or []),
        "volatile": bool(raw.get("volatile")),
        "sensitive": bool(raw.get("sensitive")),
    }


def _review_run(row: Mapping[str, Any], *, screen_id: str) -> dict[str, Any]:
    """把运行目录压成浏览器可消费的证据卡；不暴露宿主绝对路径。"""
    run_id = str(row.get("run_id") or "")
    run_dir = Path(str(row.get("run_dir") or ""))
    shots: list[str] = []

    def add_shot(value: Any) -> None:
        ref = Path(str(value or ""))
        if (
            not value
            or ref.is_absolute()
            or ".." in ref.parts
            or ref.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")
            or not (run_dir / ref).is_file()
        ):
            return
        normalized = ref.as_posix()
        if normalized not in shots:
            shots.append(normalized)

    # Operation 的 checkpoint 截图使用通用文件名，目标 Screen 记录在结果元数据里。
    # 优先按 matched screen 取证，避免把首页或中间步骤截图展示成目标页证据。
    operation_result = run_dir / "operation-result.json"
    if operation_result.is_file():
        try:
            operation_doc = json.loads(operation_result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            operation_doc = {}
        if not isinstance(operation_doc, dict):
            operation_doc = {}
        for step in operation_doc.get("steps") or []:
            if not isinstance(step, dict):
                continue
            checkpoint = step.get("screen_checkpoint") or {}
            if not isinstance(checkpoint, dict):
                continue
            if (
                checkpoint.get("status") == "passed"
                and str(checkpoint.get("matched") or "") == screen_id
                # 只出遥测的 draft checkpoint（BL-060）不作目标页证据。
                and checkpoint.get("enforcement") != "telemetry_only"
            ):
                add_shot(checkpoint.get("shot"))

    # 兼容早期以 screen_id 命名的页面证据。
    shot_dir = run_dir / "shots"
    if run_id and shot_dir.is_dir():
        for path in sorted(shot_dir.iterdir()):
            if (
                path.is_file()
                and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                and path.stem.startswith(screen_id)
            ):
                add_shot(Path("shots") / path.name)
    return {
        "run_id": run_id,
        "status": str(row.get("status") or "unknown"),
        "review_status": str(row.get("review_status") or "unreviewed"),
        "ended_at": row.get("ended_at"),
        "replay_speed": float(row.get("replay_speed") or 1.0),
        "screenshots": shots[:6],
    }


def screen_review(
    app: str | AppProfile,
    screen_id: str,
    *,
    build: str | None = None,
    operation_decider: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """聚合一个页面模块的人审视图。

    这是 YAML/运行目录与看板之间唯一的 seam：调用方只需要 app + screen_id，得到
    页面区域、FieldBinding、入口 Operation、证据和可转正状态；浏览器不拼资产路径，
    也不需要理解 YAML 结构。``operation_decider`` 由平台层注入，以复用现有转正门。
    """
    from autophone.assets.screens import load_screen_asset, screen_by_id

    profile = _profile(app, root=root)
    resolved_build = resolve_build(profile, build)
    screen_path = profile.screens_path(resolved_build)
    if not screen_path.exists():
        raise ImpactError(f"app={profile.app} build={resolved_build} 还没有 Screen 资产")
    screen_doc = load_screen_asset(screen_path)
    try:
        screen = screen_by_id(screen_doc, str(screen_id))
    except KeyError as exc:
        raise ImpactError(
            f"app={profile.app} build={resolved_build} 没有 screen={screen_id}"
        ) from exc

    screen_rows = [
        {
            "id": str(row.get("id")),
            "display_name": str(row.get("display_name") or row.get("id")),
            "status": str(row.get("status") or "draft"),
        }
        for row in (screen_doc.get("screens") or [])
        if isinstance(row, dict) and row.get("id")
    ]
    regions = [_review_region(name, value) for name, value in (screen.get("regions") or {}).items()]
    region_by_id = {row["id"]: row for row in regions}

    bindings = []
    for item in bindings_mod.list_bindings(load_bindings(profile), statuses=None):
        target = dict(item.get("target") or {})
        if str(target.get("screen_id") or "") != str(screen_id):
            continue
        if str(item.get("build") or "") != resolved_build:
            continue
        region_id = str(target.get("region") or "")
        evidence = dict(item.get("evidence") or {})
        declared = str(item.get("confidence") or bindings_mod.OBSERVED)
        effective = bindings_mod.effective_confidence(item, current_build=resolved_build)
        status = str(item.get("status") or "draft")
        if region_id:
            region = region_by_id.get(region_id) or {
                "id": region_id,
                "display_name": region_id,
            }
        else:
            screen_name = str(screen.get("display_name") or screen_id)
            region = {
                "id": "__screen__",
                "display_name": f"{screen_name}页面整体",
                "description": "当前证据只支持页面级关系，尚未细分到具体页面区域",
            }
        bindings.append({
            "id": str(item.get("id")),
            "status": status,
            "revision": item.get("revision"),
            "confidence": declared,
            "effective_confidence": effective,
            "reviewed": status == "active" and effective in (
                bindings_mod.CORRELATED, bindings_mod.CAUSAL
            ),
            "source": dict(item.get("source") or {}),
            "endpoint": dict(item.get("endpoint") or {}),
            "response_path": item.get("response_path"),
            "relation": item.get("relation"),
            "target": target,
            "region": region,
            "sensitivity": str(item.get("sensitivity") or "normal"),
            "volatile": bool(item.get("volatile")),
            "evidence": evidence,
        })
    bindings.sort(key=lambda row: (str(row["region"].get("display_name")), row["id"]))

    operations: list[dict[str, Any]] = []
    for item in load_operations(profile).values():
        navigation = dict(item.get("navigation") or {})
        # 页面模块只审“进入并停在该页”的边；首尾同态巡检不是入口 Operation。
        if str(navigation.get("to") or "") != str(screen_id):
            continue
        if navigation.get("from") and navigation.get("from") == navigation.get("to"):
            continue
        decision = dict(operation_decider(item) or {}) if operation_decider else {}
        runs = [_review_run(row, screen_id=str(screen_id)) for row in (decision.get("runs") or [])]
        status = str(item.get("status") or "draft")
        operations.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "display_name": str(item.get("display_name") or item.get("summary") or item.get("name") or ""),
            "summary": str(item.get("display_summary") or item.get("summary") or ""),
            "status": status,
            "revision": item.get("revision"),
            "navigation": navigation,
            "promotion": {
                "ready": bool(decision.get("ready")),
                "consecutive_passes": int(decision.get("consecutive_passes") or 0),
                "required_passes": int(decision.get("required_passes") or 3),
                "gaps": list(decision.get("gaps") or []),
            },
            "runs": runs,
        })
    operations.sort(key=lambda row: (row["status"] != "draft", row["id"]))

    pending_bindings = [row["id"] for row in bindings if not row["reviewed"]]
    active_operation = next((row for row in operations if row["status"] == "active"), None)
    promotable_operation = next(
        (row for row in operations if row["status"] == "draft" and row["promotion"]["ready"]), None
    )
    gaps: list[dict[str, Any]] = []
    if str(screen.get("status") or "") != "active":
        gaps.append({"code": "screen_not_active", "detail": "页面骨架尚未转为在用"})
    if not bindings:
        gaps.append({"code": "no_bindings", "detail": "该页面还没有接口关联候选"})
    elif pending_bindings:
        gaps.append({
            "code": "bindings_pending", "detail": f"还有 {len(pending_bindings)} 条区域关联待审核",
            "ids": pending_bindings,
        })
    if not operations:
        gaps.append({"code": "no_entry_operation", "detail": "该页面还没有可回放的进入操作"})
    elif active_operation is None:
        gaps.append({
            "code": "operation_pending",
            "detail": "进入页面的操作尚未完成回放人审并转为在用",
            "ids": [row["id"] for row in operations if row["status"] == "draft"],
        })

    index_doc = read_index(profile, build=resolved_build) or {}
    unbound_endpoints = sum(
        1 for gap in (index_doc.get("gaps") or []) if gap.get("kind") == "unbound_endpoint"
    )
    complete = not gaps
    return {
        "app": profile.app,
        "build": resolved_build,
        "screens": screen_rows,
        "screen": {
            "id": str(screen_id),
            "display_name": str(screen.get("display_name") or screen_id),
            "description": str(screen.get("description") or ""),
            "status": str(screen.get("status") or "draft"),
            "regions": regions,
        },
        "bindings": bindings,
        "operations": operations,
        "module": {
            "status": "reviewed" if complete else (
                "ready_to_activate" if not pending_bindings and promotable_operation else "needs_review"
            ),
            "complete": complete,
            "gaps": gaps,
        },
        "non_blocking": {"unbound_endpoints": unbound_endpoints},
    }


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def load_consumer_context(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        raise ImpactError(f"consumer context 不存在：{path}")
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else json.loads(text)
    if not isinstance(doc, dict):
        raise ImpactError(f"consumer context 格式非法：{path}")
    return doc


def plan_change(
    app: str | AppProfile,
    manifest_doc: Mapping[str, Any],
    *,
    build: str | None = None,
    consumer_context: Mapping[str, Any] | None = None,
    start_screen: str = planner_mod.DEFAULT_START_SCREEN,
    statuses: Sequence[str] | None = ("active",),
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    profile = _profile(app, root=root)
    ingested = manifest_mod.ingest(dict(manifest_doc))
    resolved_build = resolve_build(profile, build or ingested.build)
    if ingested.app and ingested.app != profile.app:
        raise ImpactError(
            f"change manifest 指向 app={ingested.app}，与目标 app={profile.app} 不符"
        )
    screen_ids, _regions = screen_inventory(profile, resolved_build)
    plan = planner_mod.plan(
        app=profile.app,
        build=resolved_build,
        ingested=ingested,
        bindings_doc=load_bindings(profile),
        graph=operation_graph(profile),
        allowed_effects=profile.allowed_effects,
        consumer_context=consumer_context,
        known_screens=screen_ids,
        start_screen=start_screen,
        statuses=statuses,
    )
    policy_targets = [target for target in plan['targets'] if 'comparison_policy' in target]
    if policy_targets:
        try:
            screen_sha256 = hashlib.sha256(profile.screens_path(resolved_build).read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise ImpactError('comparison_screen_asset_unavailable') from exc
        for target in policy_targets:
            target['screen_sha256'] = screen_sha256
    validate_or_raise('impact-plan.v1.json', plan)
    return plan


def write_plan(run_dir: Path, plan: Mapping[str, Any]) -> Path:
    path = Path(run_dir) / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(path, dict(plan))


def stage_run_inputs(
    run_dir: Path,
    *,
    manifest_doc: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    consumer_context: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """把一次 impact run 的输入落进 run 目录（§13 的资产目录约定）。"""
    run_dir = Path(run_dir)
    written: dict[str, str] = {}
    if manifest_doc is not None:
        path = run_dir / CHANGE_MANIFEST_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, dict(manifest_doc))
        written["change_manifest"] = str(path)
    if consumer_context is not None:
        path = run_dir / CONSUMER_CONTEXT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        # 落盘前必过脱敏：run 产物会被打包、外发、进 AI 净化包
        write_json_atomic(path, ctx_mod.normalize_context(consumer_context))
        written["consumer_context"] = str(path)
    if plan is not None:
        written["plan"] = str(write_plan(run_dir, plan))
    _register_impact_evidence(run_dir)
    return written


def load_plan(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / PLAN_REL
    if not path.exists():
        raise ImpactError(f"run 目录下没有 plan：{path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ImpactError(f"plan 格式非法：{path}")
    return doc


def execute_run(
    run_dir: Path,
    *,
    operation_runner: execution_mod.OperationRunner,
    run_dir_resolver: runtime_evidence_mod.RunDirResolver | None = None,
    run_id: str | None = None,
    root: Path = APP_ASSET_ROOT,
    baseline_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Execute a staged plan through the existing operation-run adapter.

    The current profile build must still match the staged plan. Operation
    lifecycle/effects are revalidated by ``impact.execution`` immediately
    before the runner is called. The resulting reachability facts are rendered
    into an Impact Result; without API/semantic diff evidence, reached targets
    intentionally remain ``inconclusive``.
    """
    run_dir = Path(run_dir)
    plan = load_plan(run_dir)
    validate_or_raise("impact-plan.v1.json", plan)
    profile = _profile(str(plan.get("app") or ""), root=root)
    current_build = resolve_build(profile, None)
    planned_build = str(plan.get("build") or "")
    if current_build != planned_build:
        raise ImpactError(
            f"impact plan build={planned_build}，当前 profile build={current_build}；"
            "拒绝跨 build 执行，请重新生成 plan"
        )
    resolved_run_id = str(run_id or run_dir.name)
    context_path = run_dir / CONSUMER_CONTEXT_REL
    consumer_context = load_consumer_context(context_path) if context_path.is_file() else None
    try:
        screen_asset_path = profile.screens_path(planned_build)
    except (AttributeError, ValueError):
        screen_asset_path = None
    # 策略目标在任何 Operation 派发前冻结；重试不重新读取可变的绑定和页面字节。
    current_bindings = None
    for target in plan.get('targets') or []:
        try:
            if 'comparison_policy' not in target:
                policy_mod.verified(target, run_dir)
                continue
            if policy_mod.has_snapshot(target, run_dir):
                policy_mod.verified(target, run_dir)
                continue
            if current_bindings is None:
                current_bindings = {str(item['id']): item for item in load_bindings(profile)['bindings']}
            binding = current_bindings.get(str(target.get('binding_id')))
            if binding is None or screen_asset_path is None:
                raise policy_mod.PolicyError('comparison_asset_missing')
            policy_mod.freeze(target, run_dir, binding=binding, screen_asset_path=screen_asset_path)
        except (OSError, ValueError) as exc:
            raise ImpactError(str(exc)) from exc
    profile_dir = getattr(profile, "profile_dir", None)
    baseline_manifest_path = baseline_manifest_path or (
        Path(profile_dir) / "frames" / planned_build / "manifest.json"
        if profile_dir is not None
        else None
    )
    baseline_manifest_path = comparison.snapshot_baseline(baseline_manifest_path, run_dir)
    execution = execution_mod.execute(
        run_id=resolved_run_id,
        plan=plan,
        operations=load_operations(profile),
        operation_runner=operation_runner,
        consumer_context=consumer_context,
        executor_effects=executor_effects(profile),
    )
    execution_path = write_json_atomic(run_dir / EXECUTION_REL, execution)
    evidence = (
        runtime_evidence_mod.collect(
            plan=plan,
            execution=execution,
            run_dir_resolver=run_dir_resolver,
            impact_run_dir=run_dir,
            screen_asset_path=(
                screen_asset_path
                if screen_asset_path is not None and screen_asset_path.is_file()
                else None
            ),
            baseline_manifest_path=baseline_manifest_path,
        )
        if run_dir_resolver is not None
        else execution_mod.evidence(execution)
    )
    report = build_report(
        run_dir,
        run_id=resolved_run_id,
        evidence=evidence,
        executed_effects=execution.get("executed_effects") or [],
    )
    return {
        "execution": execution,
        "execution_path": str(execution_path),
        **report,
    }


def read_execution(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / EXECUTION_REL
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


# --------------------------------------------------------------------------
# result / report
# --------------------------------------------------------------------------

def build_report(
    run_dir: Path,
    *,
    run_id: str | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
    executed_effects: Sequence[str] = (),
) -> dict[str, Any]:
    """plan + 证据 → result.json + report.html。证据缺省时全部目标落 not_reached。"""
    run_dir = Path(run_dir)
    plan = load_plan(run_dir)
    doc = result_mod.build_result(
        run_id=str(run_id or run_dir.name),
        plan=plan,
        evidence=evidence,
        executed_effects=executed_effects,
    )
    result_path = report_mod.write_result(run_dir, doc)
    manifest_path = run_dir / CHANGE_MANIFEST_REL
    manifest_doc: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_doc = loaded if isinstance(loaded, dict) else None
        except ValueError:
            manifest_doc = None
    report_path = report_mod.write_report(
        run_dir, doc, plan=plan, manifest=manifest_doc
    )
    artifacts = [Artifact("impact/result.json", "other", redaction_status="redacted"),
                 Artifact("impact/report.html", "other", sensitivity="sensitive",
                          retention="local_only", share_policy="forbidden", ai_policy="forbidden")]
    artifacts.extend(a for a in default_phase2_artifacts(run_dir) if a.path.startswith("impact/private/"))
    if (run_dir / "artifact-manifest.json").is_file():
        append_artifacts(run_dir, artifacts)
    else:
        write_artifact_manifest(run_dir, str(run_id or run_dir.name), artifacts)
    return {
        "result": doc,
        "result_path": str(result_path),
        "report_path": str(report_path),
    }


def read_result(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / "impact" / "result.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


# --------------------------------------------------------------------------
# bindings 人审面
# --------------------------------------------------------------------------

def review_binding(
    app: str | AppProfile,
    binding_id: str,
    *,
    confidence: str | None = None,
    status: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    note: str | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    profile = _profile(app, root=root)
    path = bindings_mod.bindings_path(profile)
    if not path.exists():
        raise ImpactError(f"app={profile.app} 还没有 binding 登记表：{path}")
    item: dict[str, Any] = {}
    if confidence:
        item = bindings_mod.set_confidence(
            path, binding_id, confidence, evidence=dict(evidence or {}), note=note
        )
    if status:
        item = bindings_mod.set_status(path, binding_id, status)
    if not item:
        item = bindings_mod.by_id(bindings_mod.load(path), binding_id)
    return item


def add_binding(
    app: str | AppProfile,
    fields: Mapping[str, Any],
    *,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    profile = _profile(app, root=root)
    path = bindings_mod.bindings_path(profile)
    bindings_mod.ensure_file(path, profile.app)
    payload = dict(fields)
    payload.setdefault("build", resolve_build(profile, payload.get("build")))
    return bindings_mod.append_binding(path, payload)
