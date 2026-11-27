"""Fail-closed multi-plane joint verdict arbitration for entity-bound Cases (Stage 2).

Combines UI execution status, window-scoped API oracle assertions, and post-action
data-model readback evidence against a frozen business entity identity.  Every
required plane must agree ``pass`` on the bound entity; missing capture, API
failures behind an optimistic UI, wrong business entity identity, or divergent
post-action readback all fail closed with unambiguous ``fail_kind`` and
``mismatch_reasons``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autophone.assets.entity_binding import policy_from_entity_binding
from autophone.observation.frida.governance import mask_phone

_UI_PASS = frozenset({"pass", "passed"})
_UI_ERROR = frozenset({"error"})
_UI_SKIPPED = frozenset({"skipped", "blocked"})

_API_MISSING = frozenset({"", "pending", "missing", "skipped", "not_configured", "captured"})
_API_PASS = frozenset({"pass", "passed"})

_READBACK_PASS = frozenset({"pass", "passed", "verified", "ok", "ready"})
_READBACK_MISSING = frozenset({"", "missing", "not_executed", "skipped", "pending", "unverified"})

_ENTITY_KEY_HINTS = (
    "entity",
    "store",
    "shop",
    "dish",
    "item",
    "sku",
    "order",
    "profile",
    "preference",
    "account",
    "user",
    "member",
    "coupon",
    "product",
    "id",
)


def _extract_entity_info(
    entity_binding: Mapping[str, Any] | None,
    resolved_entity: Any | None,
) -> tuple[str, str | None, dict[str, Any], tuple[str, ...]]:
    entity_type = ""
    entity_id: str | None = None
    attributes: dict[str, Any] = {}
    unresolved: tuple[str, ...] = ()

    if resolved_entity is not None:
        if isinstance(resolved_entity, Mapping):
            entity_type = str(resolved_entity.get("entity_type") or "")
            raw_id = resolved_entity.get("entity_id")
            entity_id = None if raw_id is None else str(raw_id)
            raw_attrs = resolved_entity.get("attributes")
            attributes = dict(raw_attrs) if isinstance(raw_attrs, Mapping) else {}
            raw_unres = resolved_entity.get("unresolved") or ()
            unresolved = tuple(str(x) for x in raw_unres if str(x))
        else:
            entity_type = str(getattr(resolved_entity, "entity_type", "") or "")
            raw_id = getattr(resolved_entity, "entity_id", None)
            entity_id = None if raw_id is None else str(raw_id)
            raw_attrs = getattr(resolved_entity, "attributes", None)
            attributes = dict(raw_attrs) if isinstance(raw_attrs, Mapping) else {}
            raw_unres = getattr(resolved_entity, "unresolved", ()) or ()
            unresolved = tuple(str(x) for x in raw_unres if str(x))
    elif isinstance(entity_binding, Mapping):
        entity_type = str(entity_binding.get("entity_type") or "")
        raw_id = entity_binding.get("entity_id")
        if isinstance(raw_id, (str, int, float)) and not isinstance(raw_id, bool):
            entity_id = str(raw_id)

    if entity_id is not None:
        entity_id = mask_phone(entity_id)
    return entity_type, entity_id, attributes, unresolved


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _is_entity_targeted_field(
    field: Mapping[str, Any],
    *,
    entity_type: str,
    entity_id: str | None,
    attributes: Mapping[str, Any],
) -> bool:
    if field.get("entity_bound") is True:
        return True
    key = str(field.get("key") or "")
    key_lower = key.lower()
    if key_lower in ("status", "resp_error"):
        return False
    want_str = str(field.get("want") or "")
    if entity_id and entity_id in want_str:
        return True
    for attr_key, attr_val in attributes.items():
        if attr_key.lower() in key_lower:
            return True
        if attr_val not in (None, "") and str(attr_val) in want_str:
            return True
    if entity_type and entity_type.lower() in key_lower:
        return True
    leaf = key_lower.rsplit(".", 1)[-1]
    return any(hint == leaf or leaf.endswith(f"_{hint}") or leaf.startswith(f"{hint}_") or hint in leaf for hint in _ENTITY_KEY_HINTS)


def _classify_api_failure(
    api_assertions: list[dict[str, Any]],
    *,
    ui_passed: bool,
    entity_type: str,
    entity_id: str | None,
    attributes: Mapping[str, Any],
) -> tuple[str, list[str], str]:
    """Distinguish ``entity_api_mismatch`` vs ``optimistic_ui_api_failed`` deterministically."""
    failed = [a for a in api_assertions if isinstance(a, Mapping) and a.get("verdict") == "fail"]
    reasons: list[str] = []
    details: list[str] = []

    if not failed:
        if ui_passed:
            _add_reason(reasons, "optimistic_ui_api_failed")
            return "optimistic_ui_api_failed", reasons, "API plane failed while UI passed"
        _add_reason(reasons, "entity_api_mismatch")
        return "entity_api_mismatch", reasons, "API plane failed"

    has_unresolved_entity_var = False
    has_api_error_or_missing = False
    has_entity_mismatch = False
    has_payload_mismatch = False

    for assertion in failed:
        reason = str(assertion.get("reason") or "")
        detail = str(assertion.get("detail") or "")
        if detail:
            details.append(detail)
        http_status = assertion.get("status")
        resp_error = assertion.get("resp_error")

        if reason == "unresolved_var":
            missing_vars = assertion.get("missing_vars") or []
            if any(str(v).startswith("entity.") for v in missing_vars) or "entity." in detail:
                has_unresolved_entity_var = True
                _add_reason(reasons, "entity_binding_unresolved")
            else:
                has_api_error_or_missing = True
                _add_reason(reasons, "api_unresolved_var")
            continue

        if reason == "missing":
            has_api_error_or_missing = True
            _add_reason(reasons, "api_expected_call_missing")
            continue

        if reason == "unexpected":
            has_entity_mismatch = True
            _add_reason(reasons, "api_forbidden_call_observed")
            continue

        if reason == "ambiguous":
            has_api_error_or_missing = True
            _add_reason(reasons, "api_call_ambiguous")
            continue

        if resp_error not in (None, ""):
            has_api_error_or_missing = True
            _add_reason(reasons, "api_transport_error")

        if isinstance(http_status, int) and http_status >= 400:
            has_api_error_or_missing = True
            _add_reason(reasons, f"api_http_status_{http_status}")

        fields = assertion.get("fields") or []
        bad_fields = [f for f in fields if isinstance(f, Mapping) and not f.get("ok")]
        for field in bad_fields:
            fkey = str(field.get("key") or "")
            if fkey == "status":
                has_api_error_or_missing = True
                _add_reason(reasons, "api_status_mismatch")
            elif fkey == "resp_error":
                has_api_error_or_missing = True
                _add_reason(reasons, "api_resp_error_failed")
            elif _is_entity_targeted_field(
                field,
                entity_type=entity_type,
                entity_id=entity_id,
                attributes=attributes,
            ):
                has_entity_mismatch = True
                _add_reason(reasons, f"entity_api_field_mismatch:{fkey}")
            else:
                has_payload_mismatch = True
                _add_reason(reasons, f"api_payload_mismatch:{fkey}")

    if has_unresolved_entity_var:
        return "entity_binding_unresolved", reasons, "; ".join(details) or "unresolved entity variable in API assertion"

    if has_api_error_or_missing and ui_passed:
        _add_reason(reasons, "optimistic_ui_api_failed")
        if has_entity_mismatch:
            _add_reason(reasons, "entity_api_mismatch")
        return "optimistic_ui_api_failed", reasons, "; ".join(details) or "UI passed while backend API failed"

    if has_entity_mismatch:
        _add_reason(reasons, "entity_api_mismatch")
        if ui_passed:
            _add_reason(reasons, "ui_passed_while_entity_api_mismatched")
        return "entity_api_mismatch", reasons, "; ".join(details) or "API payload mismatched bound business entity"

    if has_payload_mismatch and ui_passed:
        _add_reason(reasons, "optimistic_ui_api_failed")
        _add_reason(reasons, "entity_api_mismatch")
        return "optimistic_ui_api_failed", reasons, "; ".join(details) or "UI passed while API write payload mismatched"

    if ui_passed:
        _add_reason(reasons, "optimistic_ui_api_failed")
        return "optimistic_ui_api_failed", reasons, "; ".join(details) or "UI passed while API assertions failed"

    _add_reason(reasons, "entity_api_mismatch")
    return "entity_api_mismatch", reasons, "; ".join(details) or "API assertions failed"


def _evaluate_readback_plane(
    readback_record: Mapping[str, Any] | None,
    *,
    require_readback: bool,
) -> tuple[str, str]:
    """Return ``(plane_verdict, detail)`` for the post-action readback plane."""
    if not isinstance(readback_record, Mapping) or not readback_record:
        return ("missing", "post-action entity readback missing") if require_readback else ("skipped", "")

    raw_status = str(readback_record.get("status") or "").strip().lower()
    ok_flag = readback_record.get("ok")
    mismatches = readback_record.get("mismatches")
    has_mismatches = isinstance(mismatches, list) and len(mismatches) > 0
    detail = str(readback_record.get("detail") or readback_record.get("reason") or "")

    if raw_status in _READBACK_MISSING and ok_flag is None and not has_mismatches:
        if not require_readback:
            return "skipped", detail
        return "missing", detail or "post-action entity readback was not executed"

    passed = (
        (raw_status in _READBACK_PASS or (raw_status == "" and ok_flag is True))
        and ok_flag is not False
        and not has_mismatches
    )
    if passed:
        return "pass", detail

    if has_mismatches and not detail:
        detail = "; ".join(str(item) for item in mismatches)
    return "fail", detail or f"entity readback failed (status={raw_status or 'fail'})"


def arbitrate_joint_verdict(
    *,
    ui_status: str,
    api_status: str | None = None,
    api_assertions: list[dict[str, Any]] | None = None,
    api_captured: bool = False,
    readback_record: Mapping[str, Any] | None = None,
    entity_binding: Mapping[str, Any] | None = None,
    resolved_entity: Any | None = None,
) -> dict[str, Any]:
    """Arbitrate UI, window-scoped API, and post-action readback into one fail-closed verdict.

    Returns a dictionary strictly conforming to ``run-result.v1.json#/$defs/jointVerdictResult``.
    """
    entity_type, entity_id, attributes, unresolved = _extract_entity_info(
        entity_binding, resolved_entity
    )
    policy = policy_from_entity_binding(entity_binding)
    require_ui = policy["require_ui"]
    require_api = policy["require_api"]
    require_readback = policy["require_readback"]

    ui_norm = str(ui_status or "").strip().lower()
    ui_passed = ui_norm in _UI_PASS
    ui_errored = ui_norm in _UI_ERROR
    ui_skipped = ui_norm in _UI_SKIPPED

    if ui_passed:
        ui_plane = "pass"
    elif ui_skipped and not require_ui:
        ui_plane = "skipped"
    elif ui_skipped:
        ui_plane = "skipped"
    elif not require_ui and not ui_norm:
        ui_plane = "skipped"
    else:
        ui_plane = "fail"

    assertions_list = list(api_assertions or [])
    has_failed_api_assertion = any(
        isinstance(a, Mapping) and a.get("verdict") == "fail" for a in assertions_list
    )
    api_norm = str(api_status).strip().lower() if api_status is not None else ""

    if not require_api:
        if has_failed_api_assertion or api_norm in ("fail", "failed"):
            api_plane = "fail"
        elif api_captured and api_norm in _API_PASS:
            api_plane = "pass"
        else:
            api_plane = "skipped"
    else:
        if not api_captured or api_norm in _API_MISSING:
            api_plane = "missing"
        elif has_failed_api_assertion or api_norm not in _API_PASS:
            api_plane = "fail"
        else:
            api_plane = "pass"

    readback_plane, readback_detail = _evaluate_readback_plane(
        readback_record, require_readback=require_readback
    )

    planes = {
        "ui": ui_plane,
        "api": api_plane,
        "readback": readback_plane,
    }

    mismatch_reasons: list[str] = []
    detail_parts: list[str] = []
    primary_status = "pass"
    primary_fail_kind = ""

    def _set_primary(status: str, fail_kind: str) -> None:
        nonlocal primary_status, primary_fail_kind
        if not primary_fail_kind:
            primary_status = status
            primary_fail_kind = fail_kind

    # 1. Unresolved entity binding references ALWAYS fail closed.
    if unresolved:
        _set_primary("fail", "entity_binding_unresolved")
        _add_reason(mismatch_reasons, "entity_binding_unresolved")
        for ref in unresolved:
            _add_reason(mismatch_reasons, f"unresolved:{ref}")
        detail_parts.append("unresolved entity binding refs: " + ", ".join(unresolved))

    # 2. UI error takes precedence over downstream missing planes (infra error, not product pass).
    if require_ui and ui_errored:
        _set_primary("error", "ui_error")
        _add_reason(mismatch_reasons, "ui_error")
        detail_parts.append("UI execution ended with error")

    # 3. UI failure/skip takes primary fail_kind precedence over downstream missing
    # API/readback planes (since aborted UI never fires post-action readback or later calls),
    # while downstream planes and mismatch_reasons are still recorded in full below.
    if require_ui and not ui_passed and not ui_errored:
        ui_fk = "ui_skipped" if ui_skipped else "ui_assertion_failed"
        _set_primary("fail", ui_fk)
        _add_reason(mismatch_reasons, ui_fk)
        detail_parts.append(f"UI plane did not pass (ui_status={ui_status!r})")

    # 4. API plane (when required).
    if require_api:
        if api_plane == "missing":
            _set_primary("fail", "api_evidence_missing")
            _add_reason(mismatch_reasons, "api_evidence_missing")
            detail_parts.append("required API capture/assertions missing")
        elif api_plane == "fail":
            api_fail_kind, api_reasons, api_detail = _classify_api_failure(
                assertions_list,
                ui_passed=ui_passed,
                entity_type=entity_type,
                entity_id=entity_id,
                attributes=attributes,
            )
            _set_primary("fail", api_fail_kind)
            for reason in api_reasons:
                _add_reason(mismatch_reasons, reason)
            if api_detail:
                detail_parts.append(api_detail)

    # 5. Readback plane (when required).
    if require_readback:
        if readback_plane == "missing":
            _set_primary("fail", "entity_readback_missing")
            _add_reason(mismatch_reasons, "entity_readback_missing")
            if readback_detail:
                detail_parts.append(readback_detail)
        elif readback_plane == "fail":
            _set_primary("fail", "entity_readback_mismatch")
            _add_reason(mismatch_reasons, "entity_readback_mismatch")
            if ui_passed:
                _add_reason(mismatch_reasons, "ui_passed_while_readback_mismatched")
            if readback_detail:
                detail_parts.append(readback_detail)

    detail = "; ".join(part for part in detail_parts if part) or "all required entity planes passed"

    return {
        "status": primary_status,
        "fail_kind": primary_fail_kind,
        "detail": detail,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "planes": planes,
        "mismatch_reasons": mismatch_reasons,
    }


__all__ = [
    "arbitrate_joint_verdict",
]
