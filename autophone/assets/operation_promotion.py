"""Operation promotion intent: reload, validate, evaluate and persist under one lock."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any, Callable, Mapping

import yaml

from autophone.assets import operations_store
from autophone.assets.apps import AppProfile
from autophone.assets.store import path_write_lock
from autophone.assets.operations import OperationError, active_name_conflicts

logger = logging.getLogger(__name__)


class OperationPromotionError(ValueError):
    def __init__(self, code: str, detail: str, **context: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        # 结构化拒绝的附加字段（如降级闸的 references），由 HTTP 层并入响应 detail。
        self.context = context


def find_operation(profile: AppProfile, op_id: str) -> dict[str, Any]:
    path = profile.operations_path()
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(document, dict):
        raise OperationPromotionError("invalid", "operation catalog must be an object")
    matches = [
        item for item in document.get("operations", []) if str(item.get("id")) == op_id
    ]
    if not matches:
        raise OperationPromotionError("not_found", f"operation not found: {op_id}")
    if len(matches) != 1:
        raise OperationPromotionError("invalid", f"duplicate operation: {op_id}")
    return deepcopy(matches[0])


def _assert_name_available(profile: AppProfile, operation: dict[str, Any]) -> None:
    try:
        conflicts = active_name_conflicts(profile, operation)
    except OperationError as exc:
        raise OperationPromotionError("promotion_library_invalid", str(exc)) from exc
    if conflicts:
        raise OperationPromotionError(
            "promotion_name_conflict",
            f"operation name {operation.get('name')!r} is already active: {', '.join(conflicts)}",
            conflicting_operation_ids=conflicts,
        )


def promote_operation(
    profile: AppProfile,
    op_id: str,
    *,
    validate_steps: Callable[..., None],
    promotion_decision: Callable[..., Mapping[str, Any]] | None,
    force: bool = False,
) -> dict[str, Any]:
    """Preserve evidence overrides and idempotency; always enforce active-name uniqueness.

    ``validate_steps`` receives the registered steps and, as ``operation_name``, the
    name of the Operation reloaded under the lock, so the static check can apply
    the §10.10 bare self-call exception to exactly this Operation.
    """
    with path_write_lock(profile.operations_path()):
        operation = find_operation(profile, op_id)
        _assert_name_available(profile, operation)
        if operation.get("status") != "active" and isinstance(
            operation.get("steps"), list
        ):
            validate_steps(
                operation["steps"], operation_name=str(operation.get("name") or "")
            )

        if force:
            logger.warning(
                "operation force-activated (promotion gate bypassed): app=%s op=%s name=%r witness=%s",
                getattr(profile, "app", "<unknown>"),
                op_id,
                operation.get("name"),
                "yes" if operation.get("witness") else "no",
            )
        elif operation.get("status") != "active":
            if promotion_decision is None:
                raise OperationPromotionError(
                    "promotion_unavailable",
                    "operation promotion decision is not configured",
                )
            decision = promotion_decision(
                draft_id=op_id,
                profile=profile,
                op_def=deepcopy(operation),
            )
            if not decision.get("ready"):
                gaps = ",".join(decision.get("gaps") or ["not_ready"])
                raise OperationPromotionError(
                    "promotion_blocked", f"promotion_gate: {gaps}"
                )

        # Re-entrant callbacks may edit the registry despite holding this lock.
        if find_operation(profile, op_id) != operation:
            raise OperationPromotionError(
                "promotion_stale", "operation changed during promotion"
            )
        _assert_name_available(profile, operation)
        try:
            result = operations_store.update_operation(
                profile.operations_path(),
                op_id,
                {"status": "active"},
            )
        except KeyError as exc:
            raise OperationPromotionError(
                "not_found", f"operation {op_id} not found"
            ) from exc
        except ValueError as exc:
            raise OperationPromotionError("invalid", str(exc)) from exc
        logger.info(
            "operation activated: app=%s op=%s name=%r witness=%s force=%s",
            getattr(profile, "app", "<unknown>"),
            op_id,
            operation.get("name"),
            "yes" if operation.get("witness") else "no",
            force,
        )
        return result
