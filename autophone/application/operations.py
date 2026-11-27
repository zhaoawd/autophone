"""Operation catalog queries and edits without HTTP concerns."""
from __future__ import annotations

from copy import deepcopy
import logging
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from autophone.execution import effects
from autophone.assets import operations_store
from autophone.execution import operation_run
from autophone.assets.apps import AppProfile
from autophone.execution.app_recipes import import_app_recipes
from autophone.paths import CODE_ROOT
from autophone.execution.engine.recipe import RecipeError, operation_scope, validate_recipe_for_app

from autophone.assets.operation_promotion import (
    OperationPromotionError as OperationApplicationError, find_operation, promote_operation,
)
from autophone.assets.operations import OperationError, index_operations
from autophone.assets.store import path_write_lock

logger = logging.getLogger(__name__)


PromotionDecision = Callable[..., Mapping[str, Any]]


class OperationApplication:
    """Own Operation catalog read and reviewed-step update rules."""

    def __init__(
        self,
        profile: AppProfile,
        *,
        promotion_decision: PromotionDecision | None = None,
    ) -> None:
        self.profile = profile
        self.promotion_decision = promotion_decision

    def list(self, *, include_deprecated: bool = True) -> dict[str, Any]:
        document = self._document()
        operations = [deepcopy(item) for item in document.get("operations") or []]
        if not include_deprecated:
            operations = [item for item in operations if item.get("status") != "deprecated"]
        for operation in operations:
            operation["has_executor"] = self._has_executor(operation)
            if operation.get("status") != "draft" or self.promotion_decision is None:
                continue
            try:
                decision = self.promotion_decision(
                    draft_id=str(operation.get("id")),
                    profile=self.profile,
                    op_def=operation,
                )
            except Exception:  # noqa: BLE001 summary is optional; the catalog remains readable
                continue
            operation["promotion_verdict"] = {
                "ready": bool(decision.get("ready")),
                "consecutive_passes": decision.get("consecutive_passes"),
                "required_passes": decision.get("required_passes"),
                "gaps": list(decision.get("gaps") or []),
            }
        return {
            "app": self.profile.app,
            "operations_path": str(self.profile.operations_path()),
            "meta": deepcopy(document.get("meta") or {}),
            "count": len(operations),
            "operations": operations,
        }

    def get(self, op_id: str) -> dict[str, Any]:
        operation = self._find(op_id)
        return {
            **deepcopy(operation),
            "has_executor": self._has_executor(operation),
            "operations_path": str(self.profile.operations_path()),
        }

    def update_steps(self, op_id: str, steps: Any) -> dict[str, Any]:
        if not isinstance(steps, list) or not steps:
            raise OperationApplicationError("invalid", "steps must be a non-empty list")
        root = Path(getattr(self.profile, "root", None) or self.profile.operations_path().parents[3])
        # 目标不存在时不在此报错：保持原顺序，先按严格规则校验 steps，再由 update_operation 报 not_found。
        try:
            operation_name: str | None = str(self._find(op_id).get("name") or "")
        except OperationApplicationError:
            operation_name = None
        self._validate_steps(steps, operation_name=operation_name, root=root)
        try:
            result = operations_store.update_operation(
                self.profile.operations_path(),
                op_id,
                {"steps": deepcopy(steps)},
            )
        except KeyError as exc:
            raise OperationApplicationError("not_found", f"operation {op_id} not found") from exc
        except ValueError as exc:
            raise OperationApplicationError("invalid", str(exc)) from exc
        return {**result, "has_executor": self._has_executor(result)}

    def activate(self, op_id: str, *, force: bool = False) -> dict[str, Any]:
        return promote_operation(
            self.profile, op_id, force=force,
            validate_steps=self._validate_steps,
            promotion_decision=self.promotion_decision,
        )

    def deprecate(
        self, op_id: str, *, reason: str | None = None, force: bool = False,
    ) -> dict[str, Any]:
        """废弃一条操作；被 active Case 引用的 active 操作默认拒绝（BL-038 降级闸）。

        规则「active Recipe 只能引用 active Operation」的反方向。只在被废弃条目当前为 active 时
        扫描引用：某条 active Case 的 live Recipe 步骤 ``operation`` 或 Case Operation 引用
        （setup/teardown/post_case_cleanup）的 ``op`` 按 name 在改写前解析到 active 条目、改写后
        不再解析到 active 条目时，拒绝写入，``code=operation_referenced``，``references`` 列出
        App/Case/位置。draft 或其他非 active 条目的废弃不改变任何引用的 active 解析，不扫描。
        引用扫描失败（操作库、Case Registry 或 active Case 的 live Recipe 不可读取或结构非法）
        时 fail-closed，``code=reference_scan_failed``。

        ``force=True`` 是显式逃生口：放行，写 WARNING 审计日志，并把 forced 与受影响引用摘要
        （或扫描失败原因）追加到该条目的 ``deprecate_reason``。返回值带 ``forced``、
        ``affected_references``（本次写入使其失去 active 解析的引用；扫描失败时为 ``None``）与
        ``scan_error``（未失败时为 ``None``）。
        """
        with path_write_lock(self.profile.operations_path()):
            operation = find_operation(self.profile, op_id)
            references: list[dict[str, Any]] | None = []
            scan_error: str | None = None
            if operation.get("status") == "active":
                try:
                    references = self.demotion_references(op_id, status="deprecated")
                except Exception as exc:  # noqa: BLE001 任何扫描失败都按 fail-closed 处理，force 可放行
                    references = None
                    scan_error = " ".join(f"{type(exc).__name__}: {exc}".split())
            if not force:
                if scan_error is not None:
                    raise OperationApplicationError(
                        "reference_scan_failed",
                        f"operation {op_id} 引用扫描失败，不能废弃：{scan_error}",
                        op_id=op_id,
                        name=operation.get("name"),
                    )
                if references:
                    raise OperationApplicationError(
                        "operation_referenced",
                        f"operation {op_id}（{operation.get('name')}）被 active 用例引用，"
                        "废弃后这些引用不再解析到 active 操作："
                        + describe_references(references),
                        op_id=op_id,
                        name=operation.get("name"),
                        references=references,
                    )
            bypassed = bool(force) and (bool(references) or scan_error is not None)
            if bypassed:
                audit = (
                    f"引用扫描失败：{scan_error}" if scan_error is not None
                    else "受影响引用：" + describe_references(references or [])
                )
                reason = "；".join(
                    part for part in (reason, f"force 绕过操作降级闸，{audit}") if part
                )
                logger.warning(
                    "operation force-deprecated (reference gate bypassed): app=%s op=%s name=%r "
                    "references=%s scan_error=%s",
                    getattr(self.profile, "app", "<unknown>"),
                    op_id,
                    operation.get("name"),
                    describe_references(references or []) or "-",
                    scan_error or "-",
                )
            try:
                result = operations_store.deprecate_operation(
                    self.profile.operations_path(), op_id, reason=reason,
                )
            except KeyError as exc:
                raise OperationApplicationError("not_found", f"operation {op_id} not found") from exc
            except ValueError as exc:
                raise OperationApplicationError("invalid", str(exc)) from exc
        return {
            **result,
            "affected_references": references,
            "scan_error": scan_error,
            "forced": bool(force),
        }

    def demotion_references(self, op_id: str, *, status: str) -> list[dict[str, Any]]:
        """把 ``op_id`` 改写为 ``status`` 后，失去 active 解析的 active Case 引用。

        判定复用规则「active Recipe 只能引用 active Operation」的引用面：对改写前与改写后的
        操作库各算一次 ``active_case_inactive_operation_refs``（live Recipe 严格解析），只返回
        改写后新增的条目；draft Case 与仅代码注册的执行器由该判定面排除。改写前的操作库有
        active 重名（非法）而改写后合法时，只按改写后的操作库判定，返回引用该条目 name 且
        不再解析到 active 的引用；改写后仍非法时抛 ``OperationError``。
        """
        from autophone.application.cases import CaseRecipeApplication

        cases = CaseRecipeApplication(self.profile)
        rows = cases.registry_rows()
        if not any(case.get("status") == "active" for case in rows):
            return []
        current = self._document().get("operations") or []
        name = next(
            (str(item.get("name") or "") for item in current if str(item.get("id")) == op_id), "",
        )
        prospective = deepcopy(current)
        for item in prospective:
            if str(item.get("id")) == op_id:
                item["status"] = status
        source = self.profile.operations_path()
        after = cases.active_case_inactive_operation_refs(
            rows, library=index_operations(prospective, source=source), strict=True,
        )

        def key(ref: dict[str, Any]) -> tuple[Any, ...]:
            return ref["case_id"], ref["source"], ref["index"]

        try:
            before_library = index_operations(current, source=source)
        except OperationError:
            broken = [ref for ref in after if ref["name"] == name]
        else:
            before = cases.active_case_inactive_operation_refs(
                rows, library=before_library, strict=True,
            )
            existing = {key(ref) for ref in before}
            broken = [ref for ref in after if key(ref) not in existing]
        return [
            {
                "app": self.profile.app,
                "case_id": ref["case_id"],
                "source": ref["source"],
                "index": ref["index"],
                "location": f"{ref['case_id']}.{ref['source']}[{ref['index']}]",
                "name": ref["name"],
            }
            for ref in broken
        ]

    def _document(self) -> dict[str, Any]:
        path = self.profile.operations_path()
        if not path.exists():
            return {}
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, dict):
            raise OperationApplicationError("invalid", "operation catalog must be an object")
        return document

    def _find(self, op_id: str) -> Mapping[str, Any]:
        operation = next(
            (
                item
                for item in self._document().get("operations") or []
                if str(item.get("id")) == op_id
            ),
            None,
        )
        if operation is None:
            raise OperationApplicationError("not_found", f"operation not found: {op_id}")
        return operation

    def _validate_steps(
        self,
        steps: list[Any],
        *,
        operation_name: str | None = None,
        root: Path | None = None,
    ) -> None:
        """Static gate for one Operation's registered steps.

        §10.10 窄例外：steps 恰好是本 Operation 的无参自调用（``is_bare_self_call``）时，
        必填参数在运行时由 ``--with`` 经参数契约提供，这里不因缺参拒绝；步骤形状、执行器
        可解析、未知参数、值类型与效果策略照常校验。其余情形仍要求必填参数齐全。
        """
        root = root or Path(getattr(self.profile, "root", None) or CODE_ROOT)
        allow_missing_required = bool(operation_name) and operation_run.is_bare_self_call(
            steps, str(operation_name)
        )
        import_app_recipes(self.profile.app, root=root)
        try:
            with operation_scope(self.profile.app, root=root):
                validate_recipe_for_app(
                    steps, self.profile, allow_missing_required=allow_missing_required,
                )
        except (RecipeError, effects.EffectPolicyError) as exc:
            raise OperationApplicationError("invalid", f"operation steps invalid: {exc}") from exc

    def _has_executor(self, operation: Mapping[str, Any]) -> bool:
        return operation_run.has_builtin_executor(str(operation.get("name") or ""), app=self.profile.app) or (
            operation_run.is_typed_ir_steps(operation.get("steps"))
        )


def describe_references(references: list[dict[str, Any]]) -> str:
    """把降级闸的引用清单写成一行：``App/Case.位置 name``。"""
    return "；".join(
        f"{ref['app']}/{ref['location']} {ref['name']}" for ref in references
    )
