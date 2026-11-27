"""Case recipe read, validation and write rules without HTTP concerns."""
from __future__ import annotations

import hashlib
import inspect
import logging
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from autophone.assets.promotion import (
    case_requires_discrimination,
    discrimination_gaps,
    load_discrimination_proof,
)
from autophone.assets.promotion_identity import current_discrimination_context
from autophone.assets import store as asset_store
from autophone.assets.operations_store import operation_registry_write_lock
from autophone.assets import business_chains
from autophone.assets import cases as cases_store
from autophone.assets import operations as operations_store
from autophone.execution.admission import activation_blocker
from autophone.execution.app_recipes import import_app_recipes
from autophone.assets.cases import (
    active_case_validation_error,
    normalize_case_automation,
)
from autophone.assets.policy import assert_assets_writable
from autophone.execution import effects
from autophone.execution.engine.recipe import (
    RecipeError,
    load_recipe,
    operation_scope,
    set_app_recipe,
    validate_recipe_for_app,
)
from autophone.paths import CODE_ROOT

logger = logging.getLogger(__name__)


class CaseRecipeApplicationError(ValueError):
    pass


class CaseImportApplicationError(ValueError):
    def __init__(self, status_code: int, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **context}


class CaseImportApplication:
    """Commit staged workbooks as one Case Registry transaction.

    With ``batch_state_root`` set, a commit that added Cases also writes a batch
    receipt there (BL-038 stage B1).  The receipt is written after the Case
    Registry transaction and is fail-open: a receipt failure is reported in the
    result and never reverses the committed import.
    """

    def __init__(
        self, profile: Any, stage_directory: Path | None = None, *,
        batch_state_root: Path | None = None,
    ) -> None:
        self.profile = profile
        self.stage_directory = (
            stage_directory.resolve() if stage_directory is not None else None
        )
        self.batch_state_root = batch_state_root

    @staticmethod
    def ensure_writable() -> None:
        assert_assets_writable("commit case import")

    def commit(
        self, staged_path: str, *, family_selections: dict | None = None,
        family_snapshot: dict | None = None, actor: str | None = None,
        source_filename: Any = None,
    ) -> dict[str, Any]:
        from autophone.assets.case_import import from_template

        self.ensure_writable()
        filename = self._source_filename(source_filename)
        staged = str(staged_path or "").strip()
        if not staged:
            raise CaseImportApplicationError(
                422,
                "case_import_staged_path_required",
                "staged_path is required",
            )
        source = Path(staged).resolve()
        if self.stage_directory is None:
            raise CaseImportApplicationError(
                500,
                "case_import_stage_directory_required",
                "staged import requires a configured stage directory",
            )
        if source.parent != self.stage_directory:
            raise CaseImportApplicationError(
                400,
                "case_import_staged_path_app_mismatch",
                "staged_path must be a direct child of this App's import directory",
                staged_path=str(source),
            )

        with asset_store.path_write_lock(self.stage_directory / "commit"):
            if not source.exists():
                raise CaseImportApplicationError(
                    404,
                    "case_import_staged_file_not_found",
                    "staged case import file does not exist",
                    staged_path=str(source),
                )
            if not source.is_file():
                raise CaseImportApplicationError(
                    422,
                    "case_import_staged_path_not_file",
                    "staged_path must identify a staged upload file",
                    staged_path=str(source),
                )
            try:
                cases, title = from_template.extract(source)
                workbook_sha256 = self._workbook_sha256(source)
                self._apply_family_selections(source, cases, family_selections, family_snapshot)
                mode, new_ids = from_template.commit_to_path(
                    self.profile.cases_path(),
                    cases,
                    title,
                    source=str(source),
                    validate_new_rows=lambda rows: self._assert_import_families(
                        rows, staged_path=str(source), family_snapshot=family_snapshot
                        if family_selections else None,
                    ),
                )
            except CaseImportApplicationError:
                raise
            except SystemExit as exc:
                raise CaseImportApplicationError(
                    422,
                    "case_import_parse_error",
                    str(exc),
                    staged_path=str(source),
                ) from exc
            except (ValueError, yaml.YAMLError) as exc:
                raise CaseImportApplicationError(
                    422,
                    "case_import_contract_error",
                    str(exc),
                    staged_path=str(source),
                ) from exc
            source.unlink(missing_ok=True)

        added = new_ids or []
        result: dict[str, Any] = {
            "app": self.profile.app,
            "added": added,
            "count": len(added),
            "mode": mode,
            "parsed_count": len(cases),
        }
        if self.batch_state_root is not None:
            result.update(self._write_batch_receipt(
                added, parsed_count=len(cases), mode=mode, actor=actor,
                workbook_sha256=workbook_sha256, source_filename=filename,
                family_selection_count=len(family_selections or {}),
            ))
        return result

    @staticmethod
    def _source_filename(value: Any) -> str | None:
        """Display name of the uploaded workbook; only its base name is kept."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise CaseImportApplicationError(
                422, "case_import_source_filename_invalid", "source_filename must be a string",
            )
        name = Path(value.replace("\\", "/")).name.strip()
        return name[:255] or None

    @staticmethod
    def _workbook_sha256(source: Path) -> str | None:
        try:
            return hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            # The digest only annotates the receipt; extract() already read the file.
            logger.warning("case import workbook digest unavailable: path=%s error=%s", source, exc)
            return None

    def _write_batch_receipt(self, added: list[str], **fields: Any) -> dict[str, Any]:
        """Runs after the Case Registry commit; failures are reported, not raised."""
        from autophone.knowledge.case_import_batches import write_batch_receipt

        if not added:
            return {"batch_receipt_status": "skipped"}
        try:
            receipt = write_batch_receipt(
                self.profile.app, added=added, state_root=self.batch_state_root, **fields,
            )
        except Exception as exc:  # noqa: BLE001 - the receipt must not reverse a committed import
            logger.warning(
                "case import batch receipt not written: app=%s added=%s error=%s: %s",
                self.profile.app, added, type(exc).__name__, exc,
            )
            return {
                "batch_receipt_status": "failed",
                "batch_receipt_error": {
                    "code": "case_import_batch_receipt_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        logger.info(
            "case import batch receipt written: app=%s batch_id=%s added=%s",
            self.profile.app, receipt["batch_id"], len(added),
        )
        return {"batch_id": receipt["batch_id"], "batch_receipt_status": "written"}

    def _apply_family_selections(self, source: Path, cases: list[dict], selections: Any, snapshot: Any) -> None:
        from autophone.knowledge.family_candidates import import_snapshot

        if selections is None:
            return
        if not isinstance(selections, dict):
            raise CaseImportApplicationError(422, "invalid_family_selections", "family_selections must be an object")
        if not selections:
            return
        declaration = business_chains.load_business_chains(self.profile)
        if not declaration.loaded or snapshot != import_snapshot(source, declaration):
            raise CaseImportApplicationError(409, "family_preview_stale", "Family preview changed; stage the workbook again")
        by_row = {str(case["row"]): case for case in cases}
        for row, family_id in selections.items():
            if row not in by_row or not isinstance(family_id, str):
                raise CaseImportApplicationError(422, "invalid_family_selection", "Unknown workbook row or invalid Family")
            family = declaration.family(family_id)
            if family is None or family.status != "active":
                raise CaseImportApplicationError(422, "invalid_family_selection", "Select an active declared Family")
            by_row[row]["family_id"] = family_id

    def _assert_import_families(self, cases: list[dict[str, Any]], **context: Any) -> None:
        """V3/V4 for the rows left after external_id deduplication; they become draft Cases.

        Called by ``commit_to_path`` inside the Case Registry transaction; rows
        dropped as duplicates are not checked.
        """
        declaration = business_chains.load_business_chains(self.profile)
        snapshot = context.pop("family_snapshot", None)
        if snapshot is not None and (
            not declaration.loaded or declaration.sha256 != snapshot.get("declaration_sha256")
        ):
            raise CaseImportApplicationError(409, "family_preview_stale", "Family declaration changed")
        for index, case in enumerate(cases):
            issue = business_chains.family_reference_issue(
                declaration, {**case, "status": "draft"},
            )
            if issue is not None:
                code = issue.pop("code")
                message = issue.pop("message")
                raise CaseImportApplicationError(
                    422, code, message, row_index=index,
                    external_id=(case.get("raw") or {}).get("external_id"),
                    **issue, **context,
                )

    def import_source(
        self,
        source_path: str | Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Import a caller-owned workbook without consuming the source file."""
        from autophone.assets.case_import import from_template

        if not dry_run:
            self.ensure_writable()
        source = Path(source_path).resolve()
        try:
            cases, title = from_template.extract(source)
            mode, new_ids = from_template.commit_to_path(
                self.profile.cases_path(),
                cases,
                title,
                source=str(source),
                dry_run=dry_run,
                validate_new_rows=lambda rows: self._assert_import_families(
                    rows, source_path=str(source),
                ),
            )
        except CaseImportApplicationError:
            raise
        except SystemExit as exc:
            raise CaseImportApplicationError(
                422,
                "case_import_parse_error",
                str(exc),
                source_path=str(source),
            ) from exc
        except (ValueError, yaml.YAMLError) as exc:
            raise CaseImportApplicationError(
                422,
                "case_import_contract_error",
                str(exc),
                source_path=str(source),
            ) from exc
        added = new_ids or []
        return {
            "app": self.profile.app,
            "mode": mode,
            "added": added,
            "count": len(added),
            "parsed_count": len(cases),
            "dry_run": dry_run,
            "out": str(self.profile.cases_path()),
        }


class CaseLifecycleApplicationError(ValueError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class CaseLifecycleApplication:
    """Own Case Registry writes and promotion admission decisions."""

    def __init__(
        self,
        profile: Any,
        *,
        negctl_root: Path,
        activation_policy: Any = activation_blocker,
        proof_loader: Any = load_discrimination_proof,
        discrimination_required: Any = case_requires_discrimination,
        gap_evaluator: Any = discrimination_gaps,
    ) -> None:
        self.profile = profile
        self.negctl_root = negctl_root
        self.activation_policy = activation_policy
        self.proof_loader = proof_loader
        self.discrimination_required = discrimination_required
        self.gap_evaluator = gap_evaluator

    @staticmethod
    def requires_promotion(current: dict, prospective: dict) -> bool:
        current_in_lane = (
            (current.get("status") or "") == "active"
            and current.get("automation") == "ui_ready"
        )
        prospective_in_lane = (
            (prospective.get("status") or "") == "active"
            and prospective.get("automation") == "ui_ready"
        )
        return prospective_in_lane and not current_in_lane

    def promotion_required_detail(self, case_id: str) -> str:
        return (
            "promotion_required: 用例进入 active+ui_ready 必须通过用例转正闸；"
            "请先将用例保存为 draft，再使用 "
            f"POST /api/apps/{self.profile.app}/case-drafts/{case_id}/promote"
        )

    def family_reference_error(self, case: dict) -> dict | None:
        """V3/V4/V8 for one prospective Case state against the App declaration."""
        return business_chains.family_reference_issue(
            business_chains.load_business_chains(self.profile), case,
        )

    def assert_family_reference(self, case: dict) -> None:
        issue = self.family_reference_error(case)
        if issue is not None:
            logger.warning(
                "case family write rejected: app=%s case=%s code=%s family_id=%s",
                getattr(self.profile, "app", None), case.get("id"), issue["code"],
                issue["family_id"],
            )
            raise CaseLifecycleApplicationError(422, issue)

    @staticmethod
    def normalize_family_id(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise CaseLifecycleApplicationError(
                422, f"family_id 须为字符串，收到 {type(value).__name__}",
            )
        return value.strip()

    def case_by_id(self, case_id: str) -> dict | None:
        for case in CaseRecipeApplication(self.profile).rows():
            if str(case.get("id")) == str(case_id):
                return case
        return None

    def add(self, payload: dict) -> dict:
        status = str(payload.get("status") or "draft").strip()
        if status != "draft":
            raise CaseLifecycleApplicationError(
                422,
                "新建用例必须为 draft；转为 active 须走复核与转正流程",
            )
        fields = self.build_fields(payload)
        self.validate_operation_refs(fields)
        self.validate_login_precondition(fields)
        self.assert_family_reference({**fields, "status": status})
        try:
            return cases_store.append_case(
                self.profile.cases_path(),
                fields,
                status=status,
            )
        except ValueError as exc:
            raise CaseLifecycleApplicationError(422, str(exc)) from exc

    def build_fields(self, payload: dict) -> dict:
        item = str(payload.get("item") or "").strip()
        if not item:
            raise CaseLifecycleApplicationError(422, "item (用例名称) is required")
        try:
            automation = normalize_case_automation(
                payload["automation"] if "automation" in payload else ""
            )
        except ValueError as exc:
            raise CaseLifecycleApplicationError(422, str(exc)) from exc
        fields: dict[str, Any] = {
            "module": str(payload.get("module") or "").strip(),
            "item": item,
            "order_type": payload.get("order_type") or None,
            "level": str(payload.get("level") or "中").strip(),
            "automation": automation,
            "automation_reason": str(
                payload.get("automation_reason") or ""
            ).strip(),
            "desc": str(payload.get("desc") or "").strip(),
            "precondition": self.normalize_list(payload.get("precondition")),
            "steps": self.normalize_list(payload.get("steps")),
            "expects": self.normalize_list(payload.get("expects")),
            "suitable": {"api": "", "ui": ""},
            "case_type": str(payload.get("case_type") or "").strip(),
            "test_point": str(payload.get("test_point") or "").strip(),
            "owner": str(payload.get("owner") or "").strip(),
            "remark": str(payload.get("remark") or "").strip(),
            "business_family": str(
                payload.get("business_family") or ""
            ).strip(),
            "family_id": self.normalize_family_id(payload.get("family_id")),
            "pair_id": str(payload.get("pair_id") or "").strip(),
            "family_variant": str(
                payload.get("family_variant") or ""
            ).strip(),
            "raw": {
                "external_id": str(payload.get("external_id") or "").strip()
            },
        }
        for key in ("setup", "teardown", "post_case_cleanup"):
            if key not in payload:
                continue
            references = payload[key]
            if not isinstance(references, list):
                raise CaseLifecycleApplicationError(
                    422,
                    f"{key} 须为操作引用列表 [{{op, with}}]，"
                    f"收到 {type(references).__name__}",
                )
            if references:
                fields[key] = references
        if "scheduling" in payload:
            scheduling = payload["scheduling"]
            if not isinstance(scheduling, dict):
                raise CaseLifecycleApplicationError(422, "scheduling 须为对象")
            if scheduling:
                fields["scheduling"] = scheduling
        if "login_precondition" in payload and payload["login_precondition"]:
            fields["login_precondition"] = str(
                payload["login_precondition"]
            ).strip()
        return fields

    def edit(self, case_id: str, payload: dict) -> dict:
        changes = {
            key: value
            for key, value in payload.items()
            if key not in {"id", "execution_recipe"}
        }
        allowed = (
            cases_store.EDITABLE_SCALAR
            | cases_store.EDITABLE_LIST
            | cases_store.EDITABLE_STRUCT
            | cases_store.EDITABLE_OBJECT
            | cases_store.EDITABLE_RAW_HINTS
            | {"status"}
        )
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise CaseLifecycleApplicationError(422, f"unknown field(s): {unknown}")
        for key in cases_store.EDITABLE_LIST & set(changes):
            changes[key] = self.normalize_list(changes[key])
        if "automation" in changes:
            try:
                changes["automation"] = normalize_case_automation(changes["automation"])
            except ValueError as exc:
                raise CaseLifecycleApplicationError(422, str(exc)) from exc
        if "family_id" in changes:
            changes["family_id"] = self.normalize_family_id(changes["family_id"])
        recipe_changed = "execution_recipe" in payload
        if not changes and not recipe_changed:
            raise CaseLifecycleApplicationError(422, "no fields to update")
        self.validate_login_precondition(changes)

        cases_path = self.profile.cases_path()
        try:
            # Match promotion's Operation → Case → Recipe lock order. The nested
            # Recipe writer re-enters the Operation lock while this scope owns it.
            with operation_registry_write_lock(self.profile), cases_store.registry_write_lock(cases_path):
                self.validate_operation_refs(changes)
                current = self.case_by_id(case_id)
                if current is None:
                    raise KeyError(case_id)
                prospective = {**current, **changes}
                if self.requires_promotion(current, prospective):
                    raise CaseLifecycleApplicationError(
                        422,
                        self.promotion_required_detail(case_id),
                    )
                active_error = active_case_validation_error(prospective)
                if active_error is not None:
                    raise CaseLifecycleApplicationError(
                        422,
                        f"case_registry: {active_error}",
                    )
                # V3/V4：写 family_id 或变更生命周期时，按写后状态校验 Family 引用。
                # 只改其他字段时不因存量引用拒绝，存量由 Case lint 报告。
                if "family_id" in changes or "status" in changes:
                    self.assert_family_reference(prospective)
                entering_admission_scope = (
                    prospective.get("status") == "active"
                    and prospective.get("automation") == "ui_ready"
                    and (
                        current.get("status") != "active"
                        or current.get("automation") != "ui_ready"
                    )
                )
                if entering_admission_scope:
                    admission = self.activation_policy(
                        self.profile,
                        current,
                        changes=changes,
                    )
                    if admission is not None:
                        raise CaseLifecycleApplicationError(
                            422,
                            "asset_admission: "
                            f"{admission['code']} — {admission['recovery']}",
                        )
                expected_revision = current.get("revision")
                if changes and (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 1
                ):
                    raise ValueError(
                        f"Case {case_id} revision 非法：{expected_revision!r}"
                    )
                # 写入位置按写后的生命周期选择：草稿用例写 recipes/drafts/，读取同一位置。
                recipe = (
                    CaseRecipeApplication(self.profile).write(
                        case_id,
                        payload["execution_recipe"],
                        lifecycle=str(prospective.get("status") or ""),
                    )
                    if recipe_changed
                    else None
                )
                result = (
                    cases_store.update_case(
                        cases_path,
                        case_id,
                        changes,
                        expected_revision=expected_revision,
                    )
                    if changes
                    else current
                )
            if recipe is not None:
                result["execution_recipe"] = recipe
            return result
        except KeyError as exc:
            raise CaseLifecycleApplicationError(
                404,
                f"case {case_id} not found",
            ) from exc
        except CaseRecipeApplicationError as exc:
            raise CaseLifecycleApplicationError(422, str(exc)) from exc
        except CaseLifecycleApplicationError:
            raise
        except ValueError as exc:
            raise CaseLifecycleApplicationError(422, str(exc)) from exc

    def accept_family_candidate(self, candidate_id: str, *, state_root: Path | None = None) -> dict:
        """Apply an explicitly accepted suggestion using revision and classification CAS."""
        from autophone.knowledge.family_candidates import load_candidate

        assert_assets_writable("accept Family candidate")
        try:
            candidate = load_candidate(self.profile.app, candidate_id, state_root=state_root)
            path = self.profile.cases_path()
            with operation_registry_write_lock(self.profile), cases_store.registry_write_lock(path):
                declaration = business_chains.load_business_chains(self.profile)
                if not declaration.loaded or declaration.sha256 != candidate["declaration_sha256"]:
                    raise CaseLifecycleApplicationError(409, "family_candidate_stale_declaration")
                current = next((row for row in CaseRecipeApplication(self.profile).registry_rows()
                                if row.get("id") == candidate["case_id"]), None)
                if current is None:
                    raise CaseLifecycleApplicationError(404, "family_candidate_case_not_found")
                if current.get("status") != candidate["case_status"]:
                    raise CaseLifecycleApplicationError(409, "family_candidate_stale_status")
                issue = business_chains.family_reference_issue(
                    declaration, {**current, "family_id": candidate["family_id"]},
                )
                if issue is not None:
                    raise CaseLifecycleApplicationError(422, issue)
                return cases_store.update_case(
                    path, candidate["case_id"], {"family_id": candidate["family_id"]},
                    expected_revision=candidate["case_revision"],
                    expected_classification={"family_id": candidate["original_family_id"]},
                )
        except cases_store.CaseRevisionConflict as exc:
            raise CaseLifecycleApplicationError(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise CaseLifecycleApplicationError(404, "family_candidate_not_found") from exc
        except (ValueError, KeyError) as exc:
            if isinstance(exc, CaseLifecycleApplicationError):
                raise
            raise CaseLifecycleApplicationError(422, str(exc)) from exc

    def activate_many(self, case_ids: list[str]) -> dict:
        cases_path = self.profile.cases_path()
        with cases_store.registry_write_lock(cases_path):
            blocked: list[dict[str, str]] = []
            by_id = {
                str(case.get("id")): case
                for case in CaseRecipeApplication(self.profile).rows()
            }
            allowed: list[str] = []
            seen: set[str] = set()
            for case_id in case_ids:
                if case_id in seen:
                    continue
                seen.add(case_id)
                case = by_id.get(case_id)
                if case is not None and case.get("status") == "active":
                    active_error = active_case_validation_error(case)
                    if active_error is not None:
                        blocked.append({
                            "id": case_id,
                            "error": f"case_registry: {active_error}",
                        })
                        continue
                    allowed.append(case_id)
                    continue
                prospective = {**(case or {}), "status": "active"}
                if case is not None and self.requires_promotion(case, prospective):
                    blocked.append({
                        "id": case_id,
                        "error": self.promotion_required_detail(case_id),
                    })
                    continue
                if case is not None:
                    active_error = active_case_validation_error(prospective)
                    if active_error is not None:
                        blocked.append({
                            "id": case_id,
                            "error": f"case_registry: {active_error}",
                        })
                        continue
                    family_issue = self.family_reference_error(prospective)
                    if family_issue is not None:
                        blocked.append({
                            "id": case_id,
                            "code": family_issue["code"],
                            "error": f"case_family: {family_issue['code']} — {family_issue['message']}",
                        })
                        continue
                    admission = self.activation_policy(self.profile, case)
                    if admission is not None:
                        blocked.append({
                            "id": case_id,
                            "error": "asset_admission: "
                            f"{admission['code']} — {admission['recovery']}",
                        })
                        continue
                if (
                    self.profile.require_discrimination
                    and case is not None
                    and self.discrimination_required(case)
                ):
                    disc_ctx = current_discrimination_context(
                        self.profile, case_id=case_id,
                    )
                    proof = self._call_with_supported_kwargs(
                        self.proof_loader,
                        self.negctl_root,
                        case_id=case_id,
                        app=getattr(self.profile, "app", None),
                        preferred_build_label=getattr(self.profile, "build_label", None),
                        **disc_ctx,
                    )
                    gaps = self._call_with_supported_kwargs(
                        self.gap_evaluator,
                        proof,
                        current_build_label=self.profile.build_label,
                        **disc_ctx,
                    )
                    if gaps:
                        blocked.append({
                            "id": case_id,
                            "error": "discrimination_gate: " + ",".join(gaps),
                        })
                        continue
                allowed.append(case_id)

            result = (
                cases_store.activate_cases(cases_path, allowed)
                if allowed
                else {"updated": [], "failed": []}
            )
            result["failed"] = list(result.get("failed", [])) + blocked
        return {
            "activated_count": len(result["updated"]),
            "failed_count": len(result["failed"]),
            **result,
        }

    def deprecate(self, case_id: str, *, reason: str | None = None) -> dict:
        try:
            return cases_store.deprecate_case(
                self.profile.cases_path(),
                case_id,
                reason=reason,
            )
        except KeyError as exc:
            raise CaseLifecycleApplicationError(
                404,
                f"case {case_id} not found",
            ) from exc
        except ValueError as exc:
            raise CaseLifecycleApplicationError(422, str(exc)) from exc

    def deprecate_many(self, case_ids: list[str], *, reason: str | None = None) -> dict:
        result = cases_store.deprecate_cases(
            self.profile.cases_path(),
            case_ids,
            reason=reason,
        )
        return {
            "deprecated_count": len(result["deprecated"]),
            "failed_count": len(result["failed"]),
            **result,
        }

    def validate_operation_refs(self, fields: dict) -> None:
        for key in cases_store.EDITABLE_STRUCT & set(fields):
            try:
                operations_store.resolve_refs(
                    self.profile,
                    fields[key],
                    label=key,
                )
            except operations_store.OperationError as exc:
                raise CaseLifecycleApplicationError(
                    422,
                    f"{key}: {exc}",
                ) from exc

    @staticmethod
    def validate_login_precondition(fields: dict) -> None:
        if "login_precondition" not in fields:
            return
        value = fields["login_precondition"]
        if value not in cases_store.VALID_LOGIN_PRECONDITION:
            raise CaseLifecycleApplicationError(
                422,
                "login_precondition 非法："
                f"{value!r}（应为 {sorted(cases_store.VALID_LOGIN_PRECONDITION)}）",
            )

    @staticmethod
    def _call_with_supported_kwargs(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(*args, **kwargs)
        params = sig.parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(*args, **kwargs)
        supported = {key: value for key, value in kwargs.items() if key in params}
        return fn(*args, **supported)

    @staticmethod
    def normalize_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise CaseLifecycleApplicationError(
            422,
            "precondition/steps/expects must be a list or multiline string",
        )


class CaseRecipeApplication:
    def __init__(self, profile: Any) -> None:
        self.profile = profile

    def load(self, case_id: str, *, lifecycle: str | None = None) -> dict | None:
        """读用例当前的 Recipe，读取位置与 ``recipe_target`` 的写入位置一致。

        ``lifecycle="draft"``（草稿用例）只读 ``recipes/drafts/<id>.yaml``，草稿缺失时返回
        ``None``，不回退 live 路径；live 路径另有文件时在结果中带 ``legacy_live_path``。
        其他生命周期保持 live 优先、缺失时读草稿的规则。
        """
        if not hasattr(self.profile, "recipes_dir"):
            return None
        if lifecycle == "draft":
            path, source = self._draft_recipe_path(case_id), "draft"
            if not path.exists():
                return None
        else:
            path, source = self._recipe_path(case_id), "live"
            if not path.exists():
                path, source = self._draft_recipe_path(case_id), "draft"
                if not path.exists():
                    return None
        try:
            result = {
                "status": "valid",
                "source": source,
                "path": str(path),
                "steps": load_recipe(path),
            }
        except RecipeError as exc:
            result = {
                "status": "invalid",
                "source": source,
                "path": str(path),
                "steps": [],
                "error": str(exc),
            }
        return self._with_legacy_live_path(result, case_id, lifecycle=lifecycle)

    def recipe_target(self, case_id: str, *, lifecycle: str | None = None) -> dict | None:
        """用例编辑器保存 Recipe 时的写入位置；profile 没有 Recipe 目录时返回 ``None``。

        - 草稿用例（``lifecycle="draft"``）恒写 ``recipes/drafts/<id>.yaml``，与 AI 起草一致；
          live 路径已有文件时不读不写，只在 ``legacy_live_path`` 中报告。
        - 其他生命周期：live 文件存在时写 live；只有草稿文件时写草稿；都没有时写 live。
        """
        if not callable(getattr(self.profile, "recipes_dir", None)):
            return None
        live_path = self._recipe_path(case_id)
        draft_path = self._draft_recipe_path(case_id)
        if lifecycle == "draft":
            target = {"source": "draft", "path": str(draft_path)}
        elif not live_path.exists() and draft_path.exists():
            target = {"source": "draft", "path": str(draft_path)}
        else:
            target = {"source": "live", "path": str(live_path)}
        return self._with_legacy_live_path(target, case_id, lifecycle=lifecycle)

    def _with_legacy_live_path(
        self, result: dict, case_id: str, *, lifecycle: str | None,
    ) -> dict:
        """草稿用例在 live 路径另有文件时加 ``legacy_live_path``；该文件不被读取或改写。"""
        if lifecycle == "draft":
            live_path = self._recipe_path(case_id)
            if live_path.exists():
                result["legacy_live_path"] = str(live_path)
        return result

    def rows(self) -> list[dict]:
        from autophone.execution.engine.cases_loader import load_cases

        return load_cases(self.profile.cases_path())

    def lint(self, *, build_id: str | None = None) -> dict[str, Any]:
        from autophone.execution.admission import audit_active_assets
        from autophone.assets.case_lint import lint_case_registry_file

        cases_path = self.profile.cases_path()
        registry = lint_case_registry_file(cases_path)
        rows = (
            self.rows()
            if cases_path.exists() and not registry["errors"]
            else []
        )
        findings = self._audit_active_assets_with_symbols(
            rows,
            build_id=build_id,
            audit_fn=audit_active_assets,
        )
        families = self._family_lint(cases_path, registry_valid=not registry["errors"])
        errors = [*registry["errors"], *families["errors"]]
        warnings = [
            *registry["warnings"],
            *self._inactive_operation_warnings(rows),
            *families["warnings"],
        ]
        return {
            "app": self.profile.app,
            "path": str(cases_path),
            "errors": errors,
            "warnings": warnings,
            "findings": findings,
            "error_count": len(errors) + len(findings),
            "warning_count": len(warnings),
        }

    def _load_screen_asset_for_lint(self) -> tuple[dict[str, Any] | None, str | None]:
        path: Path | None = None
        try:
            candidate = self.profile.screens_path()
            if candidate.exists():
                path = candidate
        except ValueError:
            path = None
        if path is None:
            fallback = self.profile.profile_dir / "screens.yaml"
            if fallback.exists():
                path = fallback
        if path is None:
            return None, None
        try:
            from autophone.assets.screens import load_screen_asset

            return load_screen_asset(path), None
        except Exception as exc:  # noqa: BLE001
            return None, f"screen asset {path} is invalid: {exc}"

    def _audit_active_assets_with_symbols(
        self,
        rows: list[dict[str, Any]],
        *,
        build_id: str | None,
        audit_fn: Any,
    ) -> list[dict[str, Any]]:
        from autophone.assets import app_symbols
        from autophone.execution.contracts import blocker
        from autophone.execution.engine import recipe as recipe_mod
        from autophone.execution.golden_path_binding import GOLDEN_PATH_LIVE_DOCUMENT_KEY

        if not rows or not callable(getattr(self.profile, "recipes_dir", None)):
            return audit_fn(self.profile, cases=rows)

        symbol_steps_by_idx: dict[int, list[dict[str, Any]]] = {}
        for idx, row in enumerate(rows):
            if row.get("status") != "active" or row.get("automation") != "ui_ready":
                continue
            cid = str(row.get("id") or "")
            if not cid:
                continue
            rpath = self._recipe_path(cid)
            if not rpath.is_file():
                continue
            try:
                steps = load_recipe(rpath)
            except (OSError, RecipeError, ValueError):
                continue
            if recipe_mod.recipe_uses_symbols(steps):
                symbol_steps_by_idx[idx] = steps

        if not symbol_steps_by_idx:
            return audit_fn(self.profile, cases=rows)

        symbol_binding = None
        symbol_load_error: app_symbols.SymbolBindingError | None = None
        try:
            resolved = app_symbols.resolve_artifact_identity_for_app(
                self.profile,
                build_id=build_id,
                allow_default_build=True,
            )
            symbol_binding = app_symbols.load_symbol_binding_for_app(
                self.profile,
                build_id=resolved.build_id if resolved.kind == "ipa_build" else build_id,
                allow_default_build=True,
            )
        except app_symbols.SymbolBindingError as exc:
            symbol_load_error = exc

        screen_asset_doc, screen_load_error = self._load_screen_asset_for_lint()

        pre_findings: dict[int, dict[str, Any]] = {}
        audit_rows: list[dict[str, Any]] = []

        for idx, row in enumerate(rows):
            raw_steps = symbol_steps_by_idx.get(idx)
            if raw_steps is None:
                audit_rows.append(row)
                continue

            cid = str(row.get("id") or "")
            syntax_error: str | None = None
            for step_idx, step in enumerate(raw_steps):
                if isinstance(step, dict) and "symbol" in step:
                    try:
                        recipe_mod._validate_raw_symbol_step(step, index=step_idx)
                    except RecipeError as exc:
                        syntax_error = str(exc)
                        break
            if syntax_error is not None:
                pre_findings[idx] = {
                    "case_id": cid,
                    "admitted": False,
                    "blocker": blocker(
                        "asset",
                        "invalid_live_recipe",
                        recovery=f"repair the live recipe: {syntax_error}",
                    ),
                }
                continue

            if symbol_load_error is not None:
                pre_findings[idx] = {
                    "case_id": cid,
                    "admitted": False,
                    "blocker": blocker(
                        "asset",
                        getattr(symbol_load_error, "code", "symbol_binding_error"),
                        recovery=f"repair app-symbols binding for live recipe: {symbol_load_error}",
                    ),
                }
                continue

            if screen_load_error is not None:
                pre_findings[idx] = {
                    "case_id": cid,
                    "admitted": False,
                    "blocker": blocker(
                        "asset",
                        "invalid_live_recipe",
                        recovery=f"repair screens.yaml for live recipe: {screen_load_error}",
                    ),
                }
                continue

            try:
                expanded = recipe_mod.expand_symbol_steps(
                    raw_steps,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
            except (RecipeError, app_symbols.SymbolBindingError, ValueError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, app_symbols.SymbolBindingError)
                    else "invalid_live_recipe"
                )
                pre_findings[idx] = {
                    "case_id": cid,
                    "admitted": False,
                    "blocker": blocker(
                        "asset",
                        code,
                        recovery=f"repair the live recipe: {exc}",
                    ),
                }
                continue

            expanded_row = dict(row)
            expanded_row[GOLDEN_PATH_LIVE_DOCUMENT_KEY] = {
                "id": cid,
                "steps": expanded,
            }
            audit_rows.append(expanded_row)

        audit_findings = audit_fn(self.profile, cases=audit_rows) if audit_rows else []
        if not pre_findings:
            return audit_findings

        audit_by_case_id = {
            str(item.get("case_id") or ""): item
            for item in audit_findings
            if isinstance(item, dict)
        }
        ordered: list[dict[str, Any]] = []
        seen_case_ids: set[str] = set()
        for idx, row in enumerate(rows):
            if idx in pre_findings:
                ordered.append(pre_findings[idx])
                seen_case_ids.add(str(pre_findings[idx].get("case_id") or ""))
                continue
            cid = str(row.get("id") or "")
            if cid in audit_by_case_id:
                ordered.append(audit_by_case_id[cid])
                seen_case_ids.add(cid)
        for item in audit_findings:
            cid = str(item.get("case_id") or "")
            if cid not in seen_case_ids:
                ordered.append(item)
        return ordered

    def _family_lint(self, cases_path: Path, *, registry_valid: bool) -> dict[str, list]:
        """Case Family 引用规则（V3～V9，BL-038）；声明不合法时只报声明错误。"""
        declaration = business_chains.load_business_chains(self.profile)
        raw_rows = self.registry_rows() if registry_valid and cases_path.exists() else []
        return business_chains.lint_case_families(raw_rows, declaration)

    def _inactive_operation_warnings(self, rows: list[dict]) -> list[dict[str, Any]]:
        """存量报告：active Case 的 live Recipe 与 setup/teardown 引用非 active 操作库条目。

        规则「active Recipe 只能引用 active Operation」在转正与 live Recipe 写入口 fail-closed；
        存量资产只在 lint 中以 warning 列出，不改变退出码，也不改变运行期准入。
        """
        try:
            inactive_refs = self.active_case_inactive_operation_refs(rows)
        except operations_store.OperationError as exc:
            return [{
                "severity": "warning",
                "code": "operation_library_invalid",
                "path": str(self.profile.operations_path()),
                "message": str(exc),
            }]
        return [
            {
                "severity": "warning",
                "code": "inactive_operation_reference",
                "path": f"{ref['case_id']}.{ref['source']}[{ref['index']}]",
                "message": (
                    f"active 用例 {ref['case_id']} 引用非 active 操作 {ref['name']}"
                    f"（{ref['id'] or '无 id'}，status={ref['status']!r}）"
                ),
                **{key: ref[key] for key in ("case_id", "source", "name", "id", "status")},
            }
            for ref in inactive_refs
        ]

    def active_case_inactive_operation_refs(
        self,
        rows: list[dict] | None = None,
        *,
        library: dict[str, dict[str, Any]] | None = None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """列出 active Case 的 live Recipe 与 Case Operation 引用中引用非 active 操作库条目的位置（BL-038）。

        Case Operation 引用指 setup/teardown/post_case_cleanup。每条引用的判定复用
        ``operations.inactive_operation_refs``，结果另带 ``case_id``。``rows`` 缺省时读 Case
        Registry；profile 没有 Case Registry 时没有引用。``library`` 缺省时在遇到第一条 active
        Case 时读当前操作库；操作降级闸传入改写 status 前后的操作库。操作库非法时抛
        ``OperationError``，Case Registry 不可解析时抛异常，由调用方处理。

        ``strict=False``（lint）时 live Recipe 不可解析按无步骤处理；``strict=True``（降级闸）时
        抛 ``CaseRecipeApplicationError``，由调用方按扫描失败处理。
        """
        if rows is None:
            rows = self.registry_rows()
        found: list[dict[str, Any]] = []
        for case in rows:
            if case.get("status") != "active":
                continue
            if library is None:
                if not callable(getattr(self.profile, "operations_path", None)):
                    return []
                library = operations_store.load_operations(self.profile)
            case_id = str(case.get("id") or "")
            refs = operations_store.inactive_operation_refs(
                self.profile,
                steps=self._live_recipe_steps(case_id, strict=strict),
                case=case,
                library=library,
            )
            found.extend({"case_id": case_id, **ref} for ref in refs)
        return found

    def registry_rows(self) -> list[dict]:
        """读 Case Registry；profile 没有 cases_path 或文件不存在时返回空列表。

        结构非法（顶层不是 mapping、``cases`` 不是列表或含非 mapping 条目）时抛 ``ValueError``。
        """
        cases_path = getattr(self.profile, "cases_path", None)
        if not callable(cases_path) or not cases_path().exists():
            return []
        path = cases_path()
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, dict):
            raise ValueError(f"Case Registry 顶层必须是 mapping：{path}")
        rows = document.get("cases") or []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"Case Registry 的 cases 必须是 mapping 列表：{path}")
        return list(rows)

    def _live_recipe_steps(self, case_id: str, *, strict: bool = False) -> Any:
        """读 live Recipe 的 steps；profile 无 Recipe 目录或文件缺失时返回空列表。

        不可解析时：``strict=False`` 返回空列表（lint 行为）；``strict=True`` 抛
        ``CaseRecipeApplicationError``。
        """
        if not callable(getattr(self.profile, "recipes_dir", None)):
            return []
        recipe_path = self._recipe_path(case_id)
        if not recipe_path.exists():
            return []
        try:
            return load_recipe(recipe_path)
        except (OSError, RecipeError, ValueError, yaml.YAMLError) as exc:
            if strict:
                raise CaseRecipeApplicationError(
                    f"live recipe unreadable: {recipe_path}: {exc}"
                ) from exc
            return []

    def _preview_operation_library(self) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        """Operation library rows for import suggestions; unreadable libraries do not fail the preview."""
        operations_path = getattr(self.profile, "operations_path", None)
        if not callable(operations_path) or not operations_path().exists():
            return [], {"state": "missing"}
        try:
            library = operations_store.load_operations(self.profile)
        except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError) as exc:
            logger.warning(
                "case import preview: operation library unreadable: app=%s error=%s: %s",
                self.profile.app, type(exc).__name__, exc,
            )
            return None, {"state": "invalid", "error": f"{type(exc).__name__}: {exc}"}
        return list(library.values()), {"state": "loaded"}

    def preview_import(self, xlsx_path: Path, *, today: date | None = None) -> dict[str, Any]:
        """Parse one staged workbook and compare it with the Case Registry.

        ``today`` is the day the past-date validity check compares against
        (default: the local date).  Suggestions are returned only; nothing here
        writes Case assets.
        """
        from autophone.assets.case_import import from_template
        from autophone.assets.case_lint import validate_case_registry_or_raise
        from autophone.knowledge import import_suggestions

        cases, title = from_template.extract(xlsx_path)
        cases_path = self.profile.cases_path()
        existing = None
        if cases_path.exists():
            try:
                existing = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise CaseRecipeApplicationError(
                    f"解析 Case Registry 失败：{exc}"
                ) from exc
            validate_case_registry_or_raise(
                existing,
                source="case import preview",
            )
        new_rows, duplicate_external_ids = from_template.partition_import_cases(
            existing,
            cases,
        )
        from autophone.knowledge.family_candidates import import_snapshot, suggest_families

        declaration = business_chains.load_business_chains(self.profile)
        family_rows = [
            {"row": row["row"], "external_id": row["raw"]["external_id"],
             "item": row["item"], "candidates": [
                 self._with_chain_stage(candidate, declaration)
                 for candidate in suggest_families(row, declaration)
             ]}
            for row in new_rows
        ]
        operations, library = self._preview_operation_library()
        day = today or date.today()
        try:
            suggestion_rows = import_suggestions.suggestion_rows(
                new_rows, (existing or {}).get("cases") or [], operations,
                library_state=library["state"], today=day,
            )
        except ValueError as exc:
            raise CaseRecipeApplicationError(str(exc)) from exc
        return {
            "app": self.profile.app,
            "family_rows": family_rows,
            "suggestion_rows": suggestion_rows,
            "suggestion_context": {
                "source": import_suggestions.SOURCE,
                "today": day.isoformat(),
                "operation_library": library,
                "operation_hint_limit": import_suggestions.OPERATION_HINT_LIMIT,
                "validity_not_covered": list(import_suggestions.VALIDITY_NOT_COVERED),
            },
            "family_snapshot": import_snapshot(xlsx_path, declaration),
            "family_declaration_errors": list(declaration.errors),
            "staged_path": str(xlsx_path.resolve()),
            "title": title,
            "total": len(cases),
            "new": len(new_rows),
            "dup": len(duplicate_external_ids),
            "dup_external_ids": duplicate_external_ids,
            "new_sample": [
                {
                    "external_id": row["raw"]["external_id"],
                    "module": row["module"],
                    "item": row["item"],
                    "level": row["level"],
                    "automation": row["automation"],
                }
                for row in new_rows[:30]
            ],
        }

    @staticmethod
    def _with_chain_stage(candidate: dict[str, Any], declaration: Any) -> dict[str, Any]:
        """Add the declared Chain and Stage of a Family candidate for display."""
        family = declaration.family(candidate["family_id"])
        stage = declaration.stage(family.stage_id) if family is not None else None
        chain = declaration.chain(family.chain_id) if family is not None else None
        return {
            **candidate,
            "chain_id": chain.id if chain is not None else None,
            "chain_title": chain.title if chain is not None else None,
            "stage_id": stage.id if stage is not None else None,
            "stage_title": stage.title if stage is not None else None,
        }

    def write(self, case_id: str, value: Any, *, lifecycle: str | None = None) -> dict:
        """写用例编辑器提交的 Recipe 步骤；写入位置见 ``recipe_target``。

        ``lifecycle`` 为写入后用例的生命周期。草稿用例写草稿 Recipe，与 AI 起草写入位置相同；
        新建的草稿文件带 ``status: draft`` 与 ``provenance``，已有草稿只替换 ``id`` 与 ``steps``。
        静态校验对两种写入位置相同；「只能引用 active Operation」只约束 live 写入。
        """
        assert_assets_writable("write execution recipe")
        if not hasattr(self.profile, "recipes_dir"):
            raise CaseRecipeApplicationError(
                "app profile has no recipes_dir; execution_recipe is not editable"
            )
        if not isinstance(value, dict):
            raise CaseRecipeApplicationError("execution_recipe must be an object with steps")
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps:
            raise CaseRecipeApplicationError(
                "execution_recipe.steps must be a non-empty list"
            )

        profile_app = str(getattr(self.profile, "app", None) or "")
        profile_root = Path(getattr(self.profile, "root", None) or CODE_ROOT)
        self.ensure_operations_registered()
        try:
            with operation_scope(profile_app, root=profile_root):
                validate_recipe_for_app(steps, self.profile)
        except (RecipeError, effects.EffectPolicyError) as exc:
            raise CaseRecipeApplicationError(f"execution_recipe invalid: {exc}") from exc

        live_path = self._recipe_path(case_id)
        draft_path = self._draft_recipe_path(case_id)
        with (
            operation_registry_write_lock(self.profile),
            asset_store.path_write_lock(draft_path),
            asset_store.path_write_lock(live_path),
        ):
            target = self.recipe_target(case_id, lifecycle=lifecycle)
            if target is None:
                raise CaseRecipeApplicationError(
                    "app profile has no recipes_dir; execution_recipe is not editable"
                )
            source = target["source"]
            path = draft_path if source == "draft" else live_path
            if source == "live":
                # live Recipe 即 active Recipe：只能引用 active 操作库条目（BL-038）。
                try:
                    inactive_refs = operations_store.inactive_operation_refs(
                        self.profile, steps=steps
                    )
                except operations_store.OperationError as exc:
                    raise CaseRecipeApplicationError(
                        f"execution_recipe invalid: operation library unreadable: {exc}"
                    ) from exc
                if inactive_refs:
                    raise CaseRecipeApplicationError(
                        "execution_recipe invalid: live recipe references non-active "
                        "operation(s); promote them first: "
                        + operations_store.describe_operation_refs(inactive_refs)
                    )
            document: dict = {}
            if path.exists():
                try:
                    existing = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        document = existing
                except yaml.YAMLError:
                    pass
            elif source == "draft":
                # 新建草稿与 AI 起草（ai_convert.write_recipe_draft）同形：id、status、
                # provenance、steps。草稿身份只取文件摘要与 Case Registry 的 revision、
                # 保留编号，与这些键的取值无关。
                document = {
                    "id": str(case_id),
                    "status": "draft",
                    "provenance": {"source": "case_editor", "from": f"case:{case_id}"},
                }
            document["id"] = str(case_id)
            document["steps"] = steps
            asset_store.atomic_write_text(
                path,
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            )
            if source == "live":
                set_app_recipe(profile_app, str(case_id), steps, root=profile_root)
        result = {"status": "valid", "source": source, "path": str(path), "steps": steps}
        return self._with_legacy_live_path(result, case_id, lifecycle=lifecycle)

    def ensure_operations_registered(self) -> None:
        profile_app = str(getattr(self.profile, "app", None) or "")
        profile_root = Path(getattr(self.profile, "root", None) or CODE_ROOT)
        if not import_app_recipes(profile_app, root=profile_root):
            logger.debug("recipe package not found for app=%s", profile_app)

    def _recipe_path(self, case_id: str) -> Path:
        return self.profile.recipes_dir() / f"{str(case_id).lower()}.yaml"

    def _draft_recipe_path(self, case_id: str) -> Path:
        return self.profile.recipes_dir() / "drafts" / f"{str(case_id).lower()}.yaml"

    @staticmethod
    def derive_execution(case: dict, recipe: dict | None) -> dict:
        lifecycle = str(case.get("status") or "")
        automation = str(case.get("automation") or "")
        source = str((recipe or {}).get("source") or "") or None
        recipe_status = str((recipe or {}).get("status") or "") or None

        if lifecycle != "active":
            return {"state": "inactive", "runnable": False, "mode": None,
                    "recipe_source": source,
                    "reason": f"case status is {lifecycle or '<missing>'}"}
        if automation == "blocked_by_infra":
            return {"state": "blocked_by_infra", "runnable": False, "mode": None,
                    "recipe_source": source, "reason": "blocked by infrastructure"}
        if automation == "manual_checkpoint":
            return {"state": "manual_checkpoint", "runnable": False, "mode": None,
                    "recipe_source": source, "reason": "manual checkpoint required"}
        if recipe is None:
            return {"state": "missing_recipe", "runnable": False, "mode": None,
                    "recipe_source": None, "reason": "live recipe is missing"}
        if recipe_status != "valid":
            return {"state": "invalid_recipe", "runnable": False, "mode": None,
                    "recipe_source": source,
                    "reason": str(recipe.get("error") or "invalid recipe")}
        if source != "live":
            return {"state": "draft_recipe", "runnable": False, "mode": None,
                    "recipe_source": source, "reason": "recipe is still draft"}
        if automation == "ui_ready":
            return {"state": "default_safe", "runnable": True, "mode": "default",
                    "recipe_source": source,
                    "reason": "live recipe admitted to the default safe lane"}
        if automation == "needs_fixture":
            return {"state": "explicit_fixture", "runnable": True, "mode": "explicit",
                    "recipe_source": source,
                    "reason": "live recipe is runnable when explicitly selected; fixture/state effects may occur"}
        return {"state": "unsupported", "runnable": False, "mode": None,
                "recipe_source": source,
                "reason": f"unsupported automation class: {automation or '<empty>'}"}
