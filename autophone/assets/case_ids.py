"""Case ID reservation contract and deterministic allocation.

``meta.id_reservations`` maps a stable Case ID to the external identity that is
allowed to claim it.  Reserved IDs stay outside the ordinary ``next_seq`` lane:
claiming one does not consume the fresh cursor, while ordinary appends never
allocate a reserved ID.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


_CASE_ID_RE = re.compile(r"^R([1-9][0-9]*)$")


def _external_id(case: dict[str, Any]) -> str:
    raw = case.get("raw")
    if not isinstance(raw, dict):
        return ""
    value = raw.get("external_id")
    return value.strip() if isinstance(value, str) else ""


def id_reservations(document: dict[str, Any]) -> dict[str, str]:
    """Return validated ``Case ID -> external_id`` reservations.

    JSON Schema/lint provide structured reporting for persisted registries.  The
    write path repeats the small shape/uniqueness guard so a malformed registry
    cannot be made more inconsistent by an append.
    """
    meta = document.get("meta")
    raw = meta.get("id_reservations", {}) if isinstance(meta, dict) else {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("meta.id_reservations 必须是 Case ID 到 external_id 的对象")
    reservations: dict[str, str] = {}
    external_ids: set[str] = set()
    for raw_id, raw_external_id in raw.items():
        case_id = str(raw_id)
        if _CASE_ID_RE.fullmatch(case_id) is None:
            raise ValueError(f"meta.id_reservations 包含非法 Case ID：{case_id!r}")
        if not isinstance(raw_external_id, str) or not raw_external_id.strip():
            raise ValueError(f"meta.id_reservations.{case_id} 必须是非空 external_id")
        external_id = raw_external_id.strip()
        if external_id in external_ids:
            raise ValueError(f"meta.id_reservations 重复预留 external_id：{external_id!r}")
        reservations[case_id] = external_id
        external_ids.add(external_id)
    return reservations


def retired_case_ids(document: dict[str, Any]) -> set[str]:
    """Return validated persistent Case ID tombstones from ``meta.retired_ids``."""
    meta = document.get("meta")
    raw = meta.get("retired_ids", []) if isinstance(meta, dict) else []
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError("meta.retired_ids 必须是 Case ID 列表")
    retired: set[str] = set()
    for raw_id in raw:
        if not isinstance(raw_id, str) or _CASE_ID_RE.fullmatch(raw_id) is None:
            raise ValueError(f"meta.retired_ids 包含非法 Case ID：{raw_id!r}")
        if raw_id in retired:
            raise ValueError(f"meta.retired_ids 包含重复 Case ID：{raw_id}")
        retired.add(raw_id)
    return retired


def reservation_issues(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cross-record reservation violations for registry lint."""
    issues: list[dict[str, Any]] = []
    meta = document.get("meta")
    raw = meta.get("id_reservations", {}) if isinstance(meta, dict) else {}
    if not isinstance(raw, dict):
        return issues  # JSON Schema owns shape findings.
    reservations = {
        str(case_id): external_id.strip()
        for case_id, external_id in raw.items()
        if _CASE_ID_RE.fullmatch(str(case_id))
        and isinstance(external_id, str)
        and external_id.strip()
    }
    try:
        retired = retired_case_ids(document)
    except ValueError:
        retired = set()  # JSON Schema owns shape/duplicate findings.
    ids_by_external: dict[str, list[str]] = {}
    for case_id, external_id in reservations.items():
        ids_by_external.setdefault(external_id, []).append(case_id)
    for external_id, case_ids in sorted(ids_by_external.items()):
        if len(case_ids) > 1:
            issues.append({
                "code": "duplicate_reserved_external_id",
                "path": "meta/id_reservations",
                "message": (
                    f"external_id {external_id!r} 被多个 Case ID 预留：{case_ids}"
                ),
                "external_id": external_id,
                "case_ids": case_ids,
            })
    by_external = {
        external_id: case_ids[0]
        for external_id, case_ids in ids_by_external.items()
        if len(case_ids) == 1
    }
    cases = document.get("cases")
    if not isinstance(cases, list):
        return issues
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "")
        external_id = _external_id(case)
        if case_id in retired:
            issues.append({
                "code": "retired_case_id_reused",
                "path": f"cases/{index}/id",
                "message": f"退休 Case ID {case_id} 不得重新出现在 cases",
                "case_id": case_id,
            })
        expected_external_id = reservations.get(case_id)
        if expected_external_id is not None and external_id != expected_external_id:
            issues.append({
                "code": "reserved_case_id_misbound",
                "path": f"cases/{index}/raw/external_id",
                "message": (
                    f"保留 Case ID {case_id} 只能绑定 external_id "
                    f"{expected_external_id!r}，实际为 {external_id!r}"
                ),
                "case_id": case_id,
                "expected_external_id": expected_external_id,
                "actual_external_id": external_id,
            })
        expected_case_id = by_external.get(external_id)
        if expected_case_id is not None and case_id != expected_case_id:
            issues.append({
                "code": "reserved_external_id_misbound",
                "path": f"cases/{index}/id",
                "message": (
                    f"external_id {external_id!r} 只能绑定保留 Case ID "
                    f"{expected_case_id}，实际为 {case_id}"
                ),
                "external_id": external_id,
                "expected_case_id": expected_case_id,
                "actual_case_id": case_id,
            })
    for case_id in sorted(set(reservations) & retired):
        issues.append({
            "code": "retired_case_id_reserved",
            "path": f"meta/id_reservations/{case_id}",
            "message": f"退休 Case ID {case_id} 不得重新加入 id_reservations",
            "case_id": case_id,
        })
    return issues


def allocate_case_ids(
    document: dict[str, Any],
    business_cases: Iterable[dict[str, Any]],
) -> tuple[list[str], int]:
    """Plan IDs for appends and return ``(ids, next_seq_after)``.

    Reserved rows are allocated by ``raw.external_id`` and do not advance the
    ordinary cursor.  Ordinary rows consume the cursor in their own relative
    order.  Therefore moving a reserved row before or after ordinary rows in one
    import does not change any assigned ID.
    """
    reservations = id_reservations(document)
    retired_ids = retired_case_ids(document)
    issues = reservation_issues(document)
    if issues:
        raise ValueError(issues[0]["message"])
    by_external = {external_id: case_id for case_id, external_id in reservations.items()}
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("cases 必须是列表")
    used = {
        str(case.get("id"))
        for case in cases
        if isinstance(case, dict) and case.get("id") is not None
    }
    meta = document.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是对象")
    next_seq = meta.get("next_seq")
    if not isinstance(next_seq, int) or isinstance(next_seq, bool) or next_seq < 1:
        raise ValueError("meta.next_seq 必须是正整数")

    reserved_ids = set(reservations)
    max_identity_seq = max(
        (
            int(match.group(1))
            for case_id in used | reserved_ids | retired_ids
            if (match := _CASE_ID_RE.fullmatch(case_id))
        ),
        default=0,
    )
    if next_seq <= max_identity_seq:
        raise ValueError(
            f"meta.next_seq={next_seq} 必须大于现存、保留或退休的最大 Case 序号 "
            f"{max_identity_seq}"
        )
    cursor = next_seq

    def next_ordinary_id() -> str:
        nonlocal cursor
        while (
            f"R{cursor}" in used
            or f"R{cursor}" in reserved_ids
            or f"R{cursor}" in retired_ids
        ):
            cursor += 1
        case_id = f"R{cursor}"
        cursor += 1
        return case_id

    allocated: list[str] = []
    for business_case in business_cases:
        external_id = _external_id(business_case)
        case_id = by_external.get(external_id) if external_id else None
        if case_id is None:
            case_id = next_ordinary_id()
        if case_id in used:
            raise ValueError(f"分配的 id {case_id} 已存在；保留 ID 已领取或 meta.next_seq 不一致")
        used.add(case_id)
        allocated.append(case_id)
    return allocated, cursor


def import_dedup_external_ids(document: dict[str, Any]) -> set[str]:
    """Return external identities that block an incremental template import.

    A deprecated ordinary Case releases its external identity and may be
    re-imported under a fresh ID.  A Case that has claimed an explicit
    reservation retains that stable identity in every lifecycle state, including
    ``deprecated``; re-import must deduplicate instead of allocating a new ID.
    """
    reservations = id_reservations(document)
    issues = reservation_issues(document)
    if issues:
        raise ValueError(issues[0]["message"])
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("cases 必须是列表")
    external_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            continue
        external_id = _external_id(case)
        if not external_id:
            continue
        case_id = str(case.get("id") or "")
        holds_reservation = reservations.get(case_id) == external_id
        if str(case.get("status") or "") != "deprecated" or holds_reservation:
            external_ids.add(external_id)
    return external_ids


def assert_reserved_recipe_binding(document: dict[str, Any], case_id: str) -> None:
    """Fail if a reserved draft recipe is not attached to its reserved Case."""
    reservations = id_reservations(document)
    case_id = str(case_id)
    cases = document.get("cases")
    match = next(
        (
            case for case in cases or []
            if isinstance(case, dict) and str(case.get("id") or "") == case_id
        ),
        None,
    )
    expected_external_id = reservations.get(case_id)
    if match is None:
        if expected_external_id is not None:
            raise ValueError(
                f"保留 Case ID {case_id} 尚未绑定 Case，不能加载同名 draft recipe"
            )
        return
    actual_external_id = _external_id(match)
    reserved_case_id = next(
        (
            reserved_id
            for reserved_id, reserved_external_id in reservations.items()
            if reserved_external_id == actual_external_id
        ),
        None,
    )
    if reserved_case_id is not None and reserved_case_id != case_id:
        raise ValueError(
            f"Case {case_id} 的 external_id {actual_external_id!r} 已保留给 "
            f"Case ID {reserved_case_id}，不能加载同名 draft recipe"
        )
    if expected_external_id is None:
        return
    if actual_external_id != expected_external_id:
        raise ValueError(
            f"保留 Case ID {case_id} 的 draft recipe 要求 external_id "
            f"{expected_external_id!r}，实际 Case 为 {actual_external_id!r}"
        )
