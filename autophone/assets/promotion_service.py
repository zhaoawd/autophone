"""Asset promotion with injected run evidence and execution operations."""

from __future__ import annotations
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from autophone.paths import CODE_ROOT
from autophone.assets.policy import assert_assets_writable
from autophone.assets.operations_store import operation_registry_write_lock
from autophone.assets.operation_promotion import find_operation
from autophone.assets import cases as cases_store
from autophone.assets.cases import active_case_validation_error
from autophone.assets.business_chains import family_reference_issue, load_business_chains
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.assets.promotion_identity import (
    current_discrimination_context,
    current_draft_recipe_identity,
    trial_matches_identity,
)
from autophone.contracts import ContractError, write_json
from autophone.assets.operations import (
    OperationError,
    active_name_conflicts,
    describe_operation_refs,
    inactive_operation_refs,
)
from autophone.assets.promotion import operation_requires_discrimination
from autophone.assets.promotion import (
    case_requires_discrimination,
    evaluate_promotion_gate,
    load_discrimination_proof,
)
from autophone.util import now_iso as _now

from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromotionExecution:
    """Execution-owned evidence review and recipe validation/publication operations."""

    review_verification: Callable[..., dict[str, Any]]
    review_operation: Callable[..., dict[str, Any]]
    load_cases: Callable[..., list[dict[str, Any]]]
    normalize_evidence: Callable[..., Any]
    publish_recipe: Callable[..., None]
    validate_recipe: Callable[..., None]
    recording_attribution_gaps: Callable[..., list[str]] | None = None


class PromotionService:
    """Review frozen evidence and publish Case/Recipe assets under their locks."""

    def __init__(
        self,
        *,
        negctl_root: Path,
        promotion_runs: Callable[..., list[dict[str, Any]]],
        find_run_dir: Callable[[str], Path],
        detail: Callable[[str], dict[str, Any]],
        verification_manifest: Callable[[Path, str], Path],
        operation_manifest: Callable[[Path, str], Path],
        execution: PromotionExecution,
    ) -> None:
        self.negctl_root = negctl_root
        self._promotion_runs = promotion_runs
        self.find_run_dir = find_run_dir
        self.detail = detail
        self._write_verification_manifest = verification_manifest
        self._write_operation_manifest = operation_manifest
        self.execution = execution

    def promotion_decision(
        self,
        *,
        draft_id: str,
        required_passes: int = 3,
        profile: AppProfile | None = None,
        current_artifact_content_digest: str | None = None,
        current_binding_sha256: str | None = None,
        uses_symbol_binding: bool | None = None,
    ) -> dict[str, Any]:
        """验证单证据决策，不直接授权 Case 进入 Active。

        传 profile（app-aware）时把区分力证据一并算进 decision，证据按 draft_id
        取自 negctl，新鲜度对 profile.build_label 与当前精确制品摘要（D6）。该 decision 只描述验证单证据，
        不决定 Case 的 automation 或 status。验证单收录只生成未分类 Case Draft；
        后续须补 Recipe，再由 case_promotion_decision 的连续 Trial、人审和区分力门
        决定是否进入 Active。
        不传 profile 则退回纯「人审 + 连续通过」判定（无 app 上下文时不臆测 build）。
        """
        runs = self._promotion_runs(kind="verification", asset_id=draft_id)
        require_disc = bool(
            profile is not None and getattr(profile, "require_discrimination", True)
        )
        disc_ctx = (
            current_discrimination_context(
                profile,
                current_artifact_content_digest=current_artifact_content_digest,
                current_binding_sha256=current_binding_sha256,
                uses_symbol_binding=uses_symbol_binding,
            )
            if require_disc
            else {
                "current_artifact_content_digest": None,
                "current_binding_sha256": None,
                "uses_symbol_binding": False,
            }
        )
        proof = (
            load_discrimination_proof(
                self.negctl_root,
                draft_id=draft_id,
                app=profile.app if profile is not None else None,
                preferred_build_label=getattr(profile, "build_label", None),
                **disc_ctx,
            )
            if require_disc
            else None
        )
        decision = evaluate_promotion_gate(
            runs,
            draft_id=draft_id,
            required_passes=required_passes,
            discrimination=proof,
            require_discrimination=require_disc,
            current_build_label=getattr(profile, "build_label", None)
            if profile
            else None,
            **disc_ctx,
        )
        decision["runs"] = runs
        return decision

    def review_verification(
        self,
        run_id: str,
        *,
        status: str,
        reviewer: str = "web",
        note: str = "",
    ) -> dict[str, Any]:
        run_dir = self.find_run_dir(run_id)
        verification = self.execution.review_verification(
            run_dir,
            status=status,
            reviewer=reviewer,
            note=note,
        )
        manifest_path = self._write_verification_manifest(run_dir, run_id)
        return {
            "run_id": run_id,
            "review_status": verification.get("review_status"),
            "reviewed_by": verification.get("reviewed_by"),
            "review_note": verification.get("review_note"),
            "reviewed_at": verification.get("reviewed_at"),
            "verification_result": str(run_dir / "verification-result.json"),
            "verification_report": str(run_dir / "verification-report.html"),
            "artifact_manifest": str(manifest_path),
        }

    def operation_runs(
        self,
        *,
        draft_id: str | None = None,
        app: str | None = None,
        revision: int | None = None,
    ) -> list[dict[str, Any]]:
        """扫所有 type=operation 的 run，归一成转正闸读得的形态（与 verification_runs 对称）。

        record-replay-design §五：录制/AI 起草的 operation draft 走回放转正，须按 draft_id 统计连续
        passed。每条带 draft_id / status / review_status / ended_at——喂 evaluate_promotion_gate。
        """
        return self._promotion_runs(
            kind="operation",
            asset_id=draft_id,
            app=app,
            revision=revision,
        )

    def operation_promotion_decision(
        self,
        *,
        draft_id: str,
        required_passes: int = 3,
        profile: AppProfile | None = None,
        op_def: dict[str, Any] | None = None,
        app: str | None = None,
        current_artifact_content_digest: str | None = None,
        current_binding_sha256: str | None = None,
        uses_symbol_binding: bool | None = None,
    ) -> dict[str, Any]:
        """operation draft 的转正闸决策（与 promotion_decision 对称，读 operation runs 而非验证单）。

        人审 approved + N 连续 passed 才 ready。区分力闸只对**声明了 witness** 的操作要求
        （operation_requires_discrimination）——录制/AI 起草的纯 fixture 操作无 witness、不卡；
        传 profile 时按 draft 的操作定义查 witness，证据按 operation name 取自 negctl、新鲜度对
        profile.build_label。不传 profile 退回纯「人审 + 连续通过」。AI/录制都不自行入库，转正恒走这道门。

        `op_def`（写边界传入）：调用方已手握权威操作定义时直接传进来，**绕过** `_operation_def_for_draft`
        的历史缺失回退。候选按稳定 ID 读取，避免同名 active 条目遮蔽 draft；配置操作库时，
        决策同时报告 active 重名冲突。写边界仍在锁内重读并强制检查。
        """
        if op_def is None:
            op_def = self._operation_def_for_draft(draft_id, profile)
        # Operation ids are allocated per app (many registries legitimately have OP1),
        # so draft_id alone is not a global identity.  Every app-aware gate must scope
        # history to the owning app.  Draft content is revisioned too: passes from r1
        # must not satisfy a materially edited r2.
        scope_app = app or (profile.app if profile is not None else None)
        scope_revision = (
            int(op_def["revision"])
            if op_def and op_def.get("revision") is not None
            else None
        )
        runs = self.operation_runs(
            draft_id=draft_id,
            app=scope_app,
            revision=scope_revision,
        )
        # 转正门只把**原速(1.0)**的 run 计入连续 passed：倍速回放（1.5x/4x）仅供调试/取数，4x 跑通不
        # 等于操作在自然节奏下稳定（甚至更易 false-fail）。非 1x run 对闸**不可见**（既不计也不破连续，
        # 避免一次调试快放打断合法 1x streak），但仍随 decision["runs"] 全量返回供面板显示。
        gate_runs = [r for r in runs if float(r.get("replay_speed") or 1.0) == 1.0]
        # profile.require_discrimination:false 显式 opt out 区分力闸（与 verification/case 侧对称、
        # 兑现 api.activate_operation 文档与 ADR-0001 §opt-out 承诺）——人审 + 连续 passed 仍把守。
        require_disc = bool(
            profile is not None
            and op_def is not None
            and getattr(profile, "require_discrimination", True)
            and operation_requires_discrimination(op_def)
        )
        current_operation_identity = None
        if require_disc and op_def:
            from autophone.assets.operation_witness import operation_identity

            try:
                current_operation_identity = operation_identity(scope_app or "", op_def)
            except ValueError:
                current_operation_identity = {}
        disc_ctx = (
            current_discrimination_context(
                profile,
                op_def=op_def,
                current_artifact_content_digest=current_artifact_content_digest,
                current_binding_sha256=current_binding_sha256,
                uses_symbol_binding=uses_symbol_binding,
            )
            if require_disc and op_def
            else {
                "current_artifact_content_digest": None,
                "current_binding_sha256": None,
                "uses_symbol_binding": False,
            }
        )
        proof = (
            load_discrimination_proof(
                self.negctl_root, operation=op_def.get("name"), app=scope_app,
                current_operation_identity=current_operation_identity,
                preferred_build_label=getattr(profile, "build_label", None),
                **disc_ctx,
            )
            if require_disc and op_def
            else None
        )
        decision = evaluate_promotion_gate(
            gate_runs,
            draft_id=draft_id,
            required_passes=required_passes,
            discrimination=proof,
            require_discrimination=require_disc,
            current_operation_identity=current_operation_identity,
            current_build_label=getattr(profile, "build_label", None)
            if profile
            else None,
            **disc_ctx,
        )
        if (
            profile is not None and op_def is not None
            and callable(getattr(profile, "operations_path", None))
        ):
            try:
                conflicts = active_name_conflicts(profile, op_def)
            except OperationError as exc:
                decision["ready"] = False
                decision["gaps"].append("operation_library_invalid")
                decision["operation_library_error"] = str(exc)
            else:
                if conflicts:
                    decision["ready"] = False
                    decision["gaps"].append("active_name_conflict")
                    decision["conflicting_operation_ids"] = conflicts
        decision["runs"] = runs
        return decision

    @staticmethod
    def _operation_def_for_draft(
        draft_id: str, profile: AppProfile | None
    ) -> dict[str, Any] | None:
        """按 draft_id（操作 id，如 OP12）从该 app 操作库取操作定义——拿它的 witness 判区分力是否要求。"""
        if profile is None:
            return None
        try:
            # Lookup by stable ID: the name index hides same-name drafts behind Active.
            return find_operation(profile, draft_id)
        except Exception:  # noqa: BLE001 - missing legacy definitions keep the existing evidence-only view
            return None

    @staticmethod
    def _trial_matches_identity(
        run_case: dict[str, Any],
        current_identity: dict[str, Any] | None,
    ) -> bool:
        return trial_matches_identity(run_case, current_identity)

    def case_draft_runs(
        self, *, app: str | None = None, case_id: str | None = None
    ) -> list[dict[str, Any]]:
        """扫所有**草稿试跑** run，按用例归一成转正闸读得的形态。

        草稿试跑的判据从 run-spec 派生（case_selector.statuses 含 draft，或显式
        trial_draft_recipes=true）；result 同时冻结实际执行的 Case revision、保留
        external_id 和 draft recipe SHA-256；
        一个试跑 run 可能点名多条 draft 用例，故按 (run, case) 展开，draft_id=case_id。
        per-(run,case) 人审落 run 目录 case-draft-review.json（对称 verification/operation 的
        review 落点），键为 case_id。
        """
        return self._promotion_runs(
            kind="case",
            asset_id=case_id,
            app=app,
        )

    def case_promotion_decision(
        self,
        *,
        case_id: str,
        required_passes: int = 3,
        profile: AppProfile | None = None,
        runs: list[dict[str, Any]] | None = None,
        case: dict[str, Any] | None = None,
        current_artifact_content_digest: str | None = None,
        current_binding_sha256: str | None = None,
        uses_symbol_binding: bool | None = None,
    ) -> dict[str, Any]:
        """用例草稿的转正闸决策（对称 promotion_decision/operation_promotion_decision）。

        人审 approved + N 连续 passed 才 ready。生命周期 draft 的用例转正为 active+ui_ready；
        已 active、仅 recipe 仍为 draft 的用例只发布 recipe，并保留原 automation lane（R112
        这类有状态用例继续 needs_fixture 显式运行）。区分力闸按**转正后的目标 lane**判断；只有
        ui_ready 才要求 negctl。`runs`/`case` 允许列表调用方传入预扫结果，避免逐行重复读盘。
        """
        if case is None and profile is not None:
            case = next(
                (
                    row
                    for row in self.execution.load_cases(profile.cases_path())
                    if str(row.get("id")) == str(case_id)
                ),
                None,
            )
            if case is None:
                raise ContractError(f"用例 {case_id} 不存在")
        target = (
            self._case_promotion_target(case)
            if case is not None
            else {
                "promotion_scope": "case_and_recipe",
                "target_status": "active",
                "target_automation": "ui_ready",
            }
        )
        if runs is None:
            runs = self.case_draft_runs(
                app=profile.app if profile is not None else None,
                case_id=case_id,
            )

        all_runs = list(runs)
        current_identity = (
            current_draft_recipe_identity(profile, case_id)
            if profile is not None
            else None
        )
        matching_runs = [
            run
            for run in all_runs
            if self._trial_matches_identity(run, current_identity)
        ]
        rejected_runs = [
            run
            for run in all_runs
            if not self._trial_matches_identity(run, current_identity)
        ]
        evidence = self.execution.normalize_evidence(matching_runs)
        target_case = {
            **(case or {}),
            "status": target["target_status"],
            "automation": target["target_automation"],
        }
        require_disc = bool(
            profile is not None
            and getattr(profile, "require_discrimination", True)
            and case_requires_discrimination(target_case)
        )
        draft_steps = (
            self._draft_recipe_steps(profile, case_id) if profile is not None else []
        )
        disc_ctx = (
            current_discrimination_context(
                profile,
                case_id=case_id,
                recipe_source="draft",
                steps=draft_steps,
                current_artifact_content_digest=current_artifact_content_digest,
                current_binding_sha256=current_binding_sha256,
                uses_symbol_binding=uses_symbol_binding,
            )
            if require_disc
            else {
                "current_artifact_content_digest": None,
                "current_binding_sha256": None,
                "uses_symbol_binding": False,
            }
        )
        proof = (
            load_discrimination_proof(
                self.negctl_root,
                case_id=case_id,
                app=profile.app if profile is not None else None,
                preferred_build_label=getattr(profile, "build_label", None),
                current_recipe_identity=current_identity,
                **disc_ctx,
            )
            if require_disc
            else None
        )
        # 通用 negctl 证据带 Recipe 身份：须等于当前 draft 身份才计入（旧格式 probe 不比对）。
        decision = evaluate_promotion_gate(
            evidence,
            draft_id=case_id,
            required_passes=required_passes,
            discrimination=proof,
            require_discrimination=require_disc,
            current_build_label=getattr(profile, "build_label", None)
            if profile
            else None,
            current_recipe_identity=current_identity,
            **disc_ctx,
        )
        if rejected_runs:
            decision["gaps"] = list(
                dict.fromkeys(
                    [
                        *decision.get("gaps", []),
                        "trial_identity_mismatch",
                    ]
                )
            )
        if current_identity is None:
            decision["gaps"] = list(
                dict.fromkeys(
                    [
                        *decision.get("gaps", []),
                        "current_draft_recipe_identity_missing",
                    ]
                )
            )
        if profile is not None:
            # 规则「active Recipe 只能引用 active Operation」：转正发布的草稿 Recipe
            # 与转正后 Case 的 setup/teardown 不得引用非 active 操作库条目。
            try:
                inactive_refs = inactive_operation_refs(
                    profile,
                    steps=draft_steps,
                    case=target_case,
                )
                library_gap = None
            except OperationError as exc:
                inactive_refs = []
                library_gap = str(exc)
            gaps = list(decision.get("gaps", []))
            if inactive_refs:
                gaps.append("inactive_operation_reference")
            if library_gap is not None:
                gaps.append("operation_library_invalid")
                decision["operation_library_error"] = library_gap
            if inactive_refs or library_gap is not None:
                decision["gaps"] = list(dict.fromkeys(gaps))
                decision["ready"] = False
            decision["inactive_operation_refs"] = inactive_refs
        decision["runs"] = all_runs
        decision["identity_rejected_runs"] = rejected_runs
        decision["current_draft_recipe_identity"] = current_identity
        if profile is not None:
            path = profile.recipes_dir() / "drafts" / f"{case_id.lower()}.yaml"
            if path.is_file():
                import yaml
                document = None
                try:
                    document = yaml.safe_load(path.read_text(encoding="utf-8"))
                    validator = self.execution.recording_attribution_gaps
                    if not isinstance(document, dict):
                        attribution = ["recipe_invalid"]
                    elif validator is not None:
                        attribution = validator(profile, document)
                    elif "automates_case" in document or (document.get("provenance") or {}).get("source") == "recording":
                        attribution = ["automates_case_validator_unavailable"]
                    else:
                        attribution = []
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    attribution = [f"automates_case_invalid:{exc}"]
                declared = isinstance(document, dict) and "automates_case" in document
                decision["automates_case"] = {"gaps": attribution, "identity_verified": declared and not attribution,
                                               "declared": declared}
                if attribution:
                    decision["gaps"] = list(dict.fromkeys([*decision.get("gaps", []), *attribution]))
                    decision["ready"] = False
        decision["evidence_runs"] = evidence
        decision.update(target)
        return decision

    @staticmethod
    def _draft_recipe_steps(profile: AppProfile, case_id: str) -> list[Any]:
        """读取待转正草稿 Recipe 的 steps；缺失或不可解析时返回空列表（由其他闸报告）。"""
        import yaml

        path = profile.recipes_dir() / "drafts" / f"{str(case_id).lower()}.yaml"
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        steps = document.get("steps") if isinstance(document, dict) else None
        return steps if isinstance(steps, list) else []

    @staticmethod
    def _case_promotion_target(case: dict[str, Any]) -> dict[str, str]:
        """Derive the post-promotion lane without conflating Case and Recipe lifecycle."""
        lifecycle = str(case.get("status") or "")
        if lifecycle == "draft":
            return {
                "promotion_scope": "case_and_recipe",
                "target_status": "active",
                "target_automation": "ui_ready",
            }
        if lifecycle == "active":
            return {
                "promotion_scope": "recipe_only",
                "target_status": "active",
                "target_automation": str(case.get("automation") or ""),
            }
        raise ContractError(
            f"用例 {case.get('id')} 生命周期为 {lifecycle}，只有 draft 或 active+draft recipe 可转正"
        )

    def review_case_draft(
        self,
        run_id: str,
        *,
        case_id: str,
        status: str,
        reviewer: str = "web",
        note: str = "",
        profile: AppProfile | None = None,
    ) -> dict[str, Any]:
        """给一次草稿试跑 run 里的某条用例记人审结论（approved/needs_changes/rejected）。

        fail-closed：run 必须是草稿试跑 run（run-spec 含 draft 生命周期，或显式
        trial_draft_recipes=true）且真跑过该用例——
        否则普通回归 run 也能被刷成转正证据。结论落 run 目录 case-draft-review.json（按
        case_id 键控，一 run 多用例互不覆盖）。
        """
        if status not in {"approved", "needs_changes", "rejected"}:
            raise ContractError(
                f"非法人审结论 {status!r}（approved/needs_changes/rejected）"
            )
        run_dir = self.find_run_dir(run_id)
        try:
            initial_spec = json.loads(
                (run_dir / "run-spec.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"run {run_id} 的 run-spec 不可读取：{exc}") from exc
        run_app = str(initial_spec.get("app") or "")
        if profile is None:
            try:
                profile = load_app_profile(run_app)
            except (FileNotFoundError, ValueError) as exc:
                raise ContractError(
                    f"run {run_id} 的 App {run_app!r} 无法解析，不能校验 trial identity"
                ) from exc
        if str(profile.app) != run_app:
            raise ContractError(
                f"run {run_id} app={run_app!r} 与 review profile app={profile.app!r} 不一致"
            )
        from autophone.assets import store as asset_store

        cases_path = profile.cases_path()
        draft_path = profile.recipes_dir() / "drafts" / f"{case_id.lower()}.yaml"
        path = run_dir / "case-draft-review.json"
        with (
            cases_store.registry_write_lock(cases_path),
            asset_store.path_write_lock(draft_path),
            asset_store.path_write_lock(path),
        ):
            try:
                spec = json.loads(
                    (run_dir / "run-spec.json").read_text(encoding="utf-8")
                )
                platform_result = json.loads(
                    (run_dir / "platform-run-result.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractError(f"run {run_id} 的试跑产物不可读取：{exc}") from exc
            if str(spec.get("app") or "") != str(profile.app):
                raise ContractError(f"run {run_id} 的 App 在 review 锁等待期间发生变化")
            selector = spec.get("case_selector") or {}
            statuses = [str(s) for s in (selector.get("statuses") or [])]
            if "draft" not in statuses and not bool(
                selector.get("trial_draft_recipes")
            ):
                raise ContractError(
                    f"run {run_id} 不是草稿试跑 run（selector 未声明 "
                    "draft/trial_draft_recipes），不能记转正人审"
                )
            if spec.get("negative_control") is not None:
                raise ContractError(
                    f"run {run_id} 是区分力反向 Run（执行扰动后的 Recipe），不能记转正人审"
                )
            run_case = next(
                (
                    row
                    for row in (platform_result.get("cases") or [])
                    if isinstance(row, dict) and str(row.get("id") or "") == case_id
                ),
                None,
            )
            if run_case is None:
                raise ContractError(f"run {run_id} 未执行用例 {case_id}，不能记人审")
            current_identity = current_draft_recipe_identity(profile, case_id)
            if not self._trial_matches_identity(run_case, current_identity):
                raise ContractError(
                    f"run {run_id} 的用例 {case_id} trial identity 缺失或与当前 "
                    "Case/draft recipe 不一致"
                )
            assert current_identity is not None
            doc: dict[str, Any] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        doc = loaded
                except (OSError, json.JSONDecodeError):
                    pass
            doc[case_id] = {
                "review_status": status,
                "reviewed_by": reviewer,
                "review_note": note,
                "reviewed_at": _now(),
                "draft_recipe_identity": dict(current_identity),
            }
            write_json(path, doc)
            return {"run_id": run_id, "case_id": case_id, **doc[case_id]}

    def promote_case_draft(
        self,
        *,
        profile: AppProfile,
        case_id: str,
        required_passes: int = 3,
        current_artifact_content_digest: str | None = None,
        current_binding_sha256: str | None = None,
        uses_symbol_binding: bool | None = None,
    ) -> dict[str, Any]:
        """把一条达标的用例/recipe 草稿转正。

        生命周期 draft：recipe 落 live、case→active+ui_ready。case 已 active 但 recipe 仍 draft：
        只发布 recipe，保留 automation（例如 needs_fixture）。其余形态 fail-closed。
        """
        assert_assets_writable(
            "promote case and recipe assets", path=profile.cases_path()
        )
        import yaml

        from autophone.assets import store as asset_store
        from autophone.assets.case_ids import assert_reserved_recipe_binding

        case = None
        for row in self.execution.load_cases(profile.cases_path()):
            if str(row.get("id")) == str(case_id):
                case = row
                break
        if case is None:
            raise ContractError(f"用例 {case_id} 不存在")
        try:
            registry = (
                yaml.safe_load(profile.cases_path().read_text(encoding="utf-8")) or {}
            )
            assert_reserved_recipe_binding(registry, str(case_id))
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise ContractError(f"draft recipe 绑定非法：{exc}") from exc
        target = self._case_promotion_target(case)
        recipes_dir = profile.recipes_dir()
        live_path = recipes_dir / f"{str(case_id).lower()}.yaml"
        draft_path = recipes_dir / "drafts" / f"{str(case_id).lower()}.yaml"
        if (
            target["promotion_scope"] == "recipe_only"
            and live_path.exists()
            and not draft_path.exists()
        ):
            raise ContractError(
                f"用例 {case_id} 已有 live recipe，无待转正的 draft recipe"
            )
        decision = self.case_promotion_decision(
            case_id=str(case_id),
            required_passes=required_passes,
            profile=profile,
            case=case,
            current_artifact_content_digest=current_artifact_content_digest,
            current_binding_sha256=current_binding_sha256,
            uses_symbol_binding=uses_symbol_binding,
        )
        if decision.get("operation_library_error"):
            raise ContractError(
                f"operation_lifecycle: 操作库不可读取，不能转正：{decision['operation_library_error']}"
            )
        if decision.get("inactive_operation_refs"):
            raise ContractError(
                f"operation_lifecycle: 用例 {case_id} 转正后引用非 active 操作，先转正这些操作："
                + describe_operation_refs(decision["inactive_operation_refs"])
            )
        if not decision["ready"]:
            raise ContractError("promotion_gate: " + ",".join(decision["gaps"]))
        decision_identity = decision.get("current_draft_recipe_identity")
        if not isinstance(decision_identity, dict):
            raise ContractError(
                f"用例 {case_id} 的 current trial identity 缺失，请重新试跑并人审"
            )
        expected_revision = decision_identity.get("case_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ContractError(f"用例 {case_id} 的 trial identity revision 非法")

        cases_path = profile.cases_path()
        expectations_path_getter = getattr(profile, "api_expectations_path", None)
        expectations_path = (
            Path(expectations_path_getter())
            if callable(expectations_path_getter)
            else None
        )
        with ExitStack() as locks:
            locks.enter_context(operation_registry_write_lock(profile))
            locks.enter_context(cases_store.registry_write_lock(cases_path))
            locks.enter_context(asset_store.path_write_lock(draft_path))
            locks.enter_context(asset_store.path_write_lock(live_path))
            if expectations_path is not None:
                locks.enter_context(asset_store.path_write_lock(expectations_path))
            initial_document = yaml.safe_load(draft_path.read_text(encoding="utf-8")) or {}
            if isinstance(initial_document, dict) and "automates_case" in initial_document:
                locks.enter_context(asset_store.path_write_lock(profile.screens_path(profile.build_label)))

            # The gate was evaluated before waiting for the cooperative locks.
            # Reload every mutable identity component after acquiring them.
            locked_case = next(
                (
                    row
                    for row in self.execution.load_cases(cases_path)
                    if str(row.get("id") or "").upper() == str(case_id).upper()
                ),
                None,
            )
            if locked_case is None:
                raise ContractError(f"用例 {case_id} 不存在")
            locked_target = self._case_promotion_target(locked_case)
            if any(
                locked_target[name] != decision.get(name)
                for name in ("promotion_scope", "target_status", "target_automation")
            ):
                raise ContractError(
                    f"用例 {case_id} 的转正目标在闸决策后发生变化，请重新试跑并人审"
                )
            locked_decision = self.case_promotion_decision(
                case_id=str(case_id),
                required_passes=required_passes,
                profile=profile,
                case=locked_case,
                current_artifact_content_digest=current_artifact_content_digest,
                current_binding_sha256=current_binding_sha256,
                uses_symbol_binding=uses_symbol_binding,
            )
            if not locked_decision["ready"]:
                raise ContractError(
                    "promotion_gate: " + ",".join(locked_decision.get("gaps") or [])
                )
            if (
                locked_decision.get("current_draft_recipe_identity")
                != decision_identity
            ):
                raise ContractError(
                    f"用例 {case_id} 的 current trial identity 在锁内重评时发生变化，"
                    "请重新试跑并人审"
                )
            self._assert_case_api_sidecar_active(expectations_path, str(case_id))
            if any(
                locked_decision.get(name) != decision.get(name)
                for name in ("promotion_scope", "target_status", "target_automation")
            ):
                raise ContractError(
                    f"用例 {case_id} 的转正目标在锁内重评时发生变化，请重新试跑并人审"
                )
            decision = locked_decision
            publish_identity = current_draft_recipe_identity(profile, str(case_id))
            if publish_identity != decision_identity:
                raise ContractError(
                    f"用例 {case_id} 的 current trial identity 在转正闸决策后发生变化，"
                    "请重新试跑并人审"
                )

            target_case = {
                **locked_case,
                "status": locked_target["target_status"],
                "automation": locked_target["target_automation"],
            }
            active_error = active_case_validation_error(target_case)
            if active_error is not None:
                raise ContractError(f"case_registry: {active_error}")
            if locked_case.get("status") != target_case["status"]:
                # V4：生命周期变更为 active 时，非空 family_id 须指向 active Family。
                # 不要求 Case 必须有 Family（Promotion Gate 不变，Case Family 提案 D6）。
                family_issue = family_reference_issue(
                    load_business_chains(profile), target_case
                )
                if family_issue is not None:
                    raise ContractError(
                        f"case_family: {family_issue['code']} — {family_issue['message']}"
                    )

            try:
                draft_bytes = draft_path.read_bytes()
                existing = yaml.safe_load(draft_bytes) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ContractError(
                    f"用例 {case_id} 的 draft recipe 在转正发布前不可读取：{exc}"
                ) from exc
            if hashlib.sha256(draft_bytes).hexdigest() != decision_identity.get(
                "draft_recipe_sha256"
            ):
                raise ContractError(
                    f"用例 {case_id} 的 current trial identity 在转正发布前发生变化，"
                    "请重新试跑并人审"
                )
            if not isinstance(existing, dict):
                raise ContractError(f"用例 {case_id} 的 draft recipe 必须是对象")
            steps = existing.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ContractError(f"用例 {case_id} 的 draft recipe 缺少非空 steps")
            self.execution.validate_recipe(profile, case_id, steps)
            try:
                inactive_refs = inactive_operation_refs(
                    profile, steps=steps, case=target_case
                )
            except OperationError as exc:
                raise ContractError(
                    f"operation_lifecycle: 操作库不可读取，不能转正：{exc}"
                ) from exc
            if inactive_refs:
                raise ContractError(
                    f"operation_lifecycle: 用例 {case_id} 转正后引用非 active 操作，先转正这些操作："
                    + describe_operation_refs(inactive_refs)
                )
            final_identity = current_draft_recipe_identity(profile, str(case_id))
            if final_identity != decision_identity:
                raise ContractError(
                    f"用例 {case_id} 的 current trial identity 在转正发布前发生变化，"
                    "请重新试跑并人审"
                )

            doc: dict[str, Any] = {"id": str(case_id)}
            if "automates_case" in existing:
                doc["automates_case"] = dict(existing["automates_case"])
            if isinstance(existing.get("provenance"), dict):
                doc["provenance"] = existing["provenance"]
            doc["steps"] = steps

            gate_note = (
                f"连续 passed {decision['consecutive_passes']} 次 + 人审 approved"
                + (
                    ""
                    if not decision.get("discrimination_required")
                    else " + 区分力证据"
                )
            )
            if locked_target["promotion_scope"] == "case_and_recipe":
                changes = {
                    "status": "active",
                    "automation": "ui_ready",
                    "automation_reason": f"草稿转正门放行：{gate_note}",
                }
            else:
                previous_reason = str(
                    locked_case.get("automation_reason") or ""
                ).strip()
                published_note = (
                    f"draft recipe 转正门放行并发布 live：{gate_note}；"
                    f"保持 {locked_target['target_automation'] or '原'} lane 显式运行"
                )
                changes = {
                    "automation_reason": (
                        f"{previous_reason}；{published_note}"
                        if previous_reason
                        else published_note
                    ),
                }

            if "automates_case" in doc:
                doc["automates_case"]["promoted_case_revision"] = expected_revision + int(
                    any(cases_store.CASES_SPEC.bumps_revision(key) for key in changes)
                )
            live_text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
            old_live = (
                live_path.read_text(encoding="utf-8") if live_path.exists() else None
            )
            asset_store.atomic_write_text(live_path, live_text)
            try:
                updated = cases_store.update_case(
                    cases_path,
                    str(case_id),
                    changes,
                    expected_revision=expected_revision,
                )
            except Exception:
                if old_live is None:
                    live_path.unlink(missing_ok=True)
                else:
                    asset_store.atomic_write_text(live_path, old_live)
                raise
            self.execution.publish_recipe(
                profile.app,
                str(case_id),
                steps,
                root=getattr(profile, "root", None) or CODE_ROOT,
            )
            try:
                draft_path.unlink()
            except OSError:
                logger.warning(
                    "转正后删除草稿 recipe 失败（不影响执行，live 优先）：%s",
                    draft_path,
                )
        logger.info(
            "用例草稿转正 %s：streak=%s recipe=%s",
            case_id,
            decision.get("streak"),
            live_path,
        )
        return {
            "case": updated,
            "recipe_path": str(live_path),
            "decision": {k: v for k, v in decision.items() if k != "runs"},
        }

    @staticmethod
    def _assert_case_api_sidecar_active(
        expectations_path: Path | None,
        case_id: str,
    ) -> None:
        """同名 API Sidecar 存在时，Case/Recipe 转正前必须已完成人工转正。"""
        if expectations_path is None or not expectations_path.exists():
            return
        import yaml

        try:
            doc = yaml.safe_load(expectations_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ContractError(f"API Sidecar 不可读取：{exc}") from exc
        rows = [
            row
            for row in (doc.get("expectations") or [])
            if isinstance(row, dict)
            and str(row.get("id") or "").upper() == str(case_id).upper()
        ]
        if len(rows) > 1:
            raise ContractError(f"用例 {case_id} 存在重复 API Sidecar")
        if not rows:
            return
        status = str(rows[0].get("status") or "").strip()
        if status != "active":
            raise ContractError(
                f"用例 {case_id} 的 API Sidecar 状态为 {status or 'missing'}；"
                "须先完成人工审核并转为 active"
            )

    def review_operation(
        self,
        run_id: str,
        *,
        status: str,
        reviewer: str = "web",
        note: str = "",
    ) -> dict[str, Any]:
        """给一条 operation run 落人审（与 review_verification 对称）——转正闸要 approved。"""
        run_dir = self.find_run_dir(run_id)
        operation = self.execution.review_operation(
            run_dir, status=status, reviewer=reviewer, note=note
        )
        manifest_path = self._write_operation_manifest(run_dir, run_id)
        return {
            "run_id": run_id,
            "review_status": operation.get("review_status"),
            "reviewed_by": operation.get("reviewed_by"),
            "review_note": operation.get("review_note"),
            "reviewed_at": operation.get("reviewed_at"),
            "draft_id": operation.get("draft_id"),
            "operation_result": str(run_dir / "operation-result.json"),
            "operation_report": str(run_dir / "operation-report.html"),
            "artifact_manifest": str(manifest_path),
        }

    @staticmethod
    def _verification_to_case_fields(
        run_id: str,
        verification: dict[str, Any],
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把一张已审核通过的验证单派生成 Case Draft 业务字段（纯函数，可测）。

        checkpoint 是断言 → 落 expects；requirement → item/desc；fixture.order.type
        → order_type。验证单只证明检查点结果，不证明 live Recipe 和连续 Trial 已完成，
        因此 automation 保持空值待评估。provenance 记录来源 run_id/draft_id，供去重与回溯。
        """
        if overrides is not None and not isinstance(overrides, dict):
            raise ContractError("验证单收录 overrides 必须是对象")
        ov = overrides or {}
        requested_automation = ov.get("automation")
        if requested_automation is not None and (
            not isinstance(requested_automation, str) or requested_automation.strip()
        ):
            raise ContractError(
                "验证单收录 automation 必须为空；自动化分类由 Case Draft 流程评估"
            )
        requested_status = ov.get("status")
        if requested_status is not None and (
            not isinstance(requested_status, str)
            or requested_status.strip() not in {"", "draft"}
        ):
            raise ContractError("验证单收录 status 必须为 draft")
        draft_id = verification.get("draft_id")
        requirement = verification.get("requirement") or "验证单收录用例"
        checkpoints = verification.get("checkpoints") or []
        expects = [
            str(c.get("title") or c.get("id") or "")
            for c in checkpoints
            if c.get("title") or c.get("id")
        ]
        precondition = verification.get("precondition") or {}
        fixture_order = ((precondition.get("fixture")) or {}).get("order") or {}
        # order_type 优先取旧形态 fixture.order.type；缺失则回退到 setup 里 create_order 的入参
        # ——precondition 引用操作后，造单意图落在 setup 而非 fixture（roadmap §八.2）。
        order_type = fixture_order.get("type")
        if order_type is None:
            for ref in precondition.get("setup") or []:
                if isinstance(ref, dict) and ref.get("op") == "create_order":
                    order_type = (ref.get("with") or {}).get("order_type")
                    break
        return {
            "module": ov.get("module") or "验证收录",
            "item": ov.get("item") or requirement,
            "order_type": ov.get("order_type", order_type),
            "level": ov.get("level") or "中",
            "automation": "",
            "automation_reason": ov.get("automation_reason")
            or (
                f"由验证单 {run_id}（draft {draft_id}）审核通过后收录为 Case Draft；"
                "自动化状态、Recipe 与转正证据待补齐"
            ),
            "desc": requirement,
            "steps": ov.get("steps") or [f"复现验证单 {draft_id or run_id} 的操作路径"],
            "expects": expects or [requirement],
            "provenance": {
                "source": "verification",
                "run_id": run_id,
                "draft_id": draft_id,
            },
        }

    def promote_verification(
        self,
        run_id: str,
        *,
        profile: AppProfile,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把一张审核通过的验证单收录为未分类 Case Draft。

        fail-closed：非 verification run / 未审核通过 / 缺 draft_id / 同一 draft 已收录过
        → 抛 ContractError。该入口不创建 Active、ui_ready 或 live Recipe；后续必须走
        Case Draft Trial、人审和专用转正端点。
        """
        assert_assets_writable(
            "promote verification to case asset", path=profile.cases_path()
        )
        detail = self.detail(run_id)
        result = detail.get("result") or {}
        verification = detail.get("verification") or {}
        if result.get("type") != "verification":
            raise ContractError(f"run {run_id} 不是验证单 run，不能收录")
        if verification.get("review_status") != "approved":
            raise ContractError("仅「审核通过(approved)」的验证单可收录")
        draft_id = verification.get("draft_id")
        if not draft_id:
            # 收录 verification 必须有 draft_id：用于重复收录保护和后续 Case Draft 证据定位。
            raise ContractError(
                "验证单缺 draft_id，不能收录（收录需 draft_id 做去重与后续证据定位）"
            )

        cases_path = profile.cases_path()
        for case in self.execution.load_cases(cases_path):
            prov = case.get("provenance") or {}
            if (
                prov.get("source") == "verification"
                and prov.get("draft_id") == draft_id
            ):
                raise ContractError(
                    f"draft {draft_id} 已收录为用例 {case.get('id')}，勿重复沉淀"
                )

        fields = self._verification_to_case_fields(run_id, verification, overrides)
        return cases_store.append_case(cases_path, fields, status="draft")
