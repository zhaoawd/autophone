"""Deterministic cumulative evidence-level verifier."""
from __future__ import annotations

from typing import Any, Iterable

from autophone.knowledge.evidence_levels import LEVELS


def _overlap_scope(rows: list[dict[str, Any]]) -> bool:
    return _all_build_scopes_confirmed(rows) and len({
        (row.get("build_scope") or {}).get("subject_build_key")
        for row in rows
    }) == 1


def _all_build_scopes_confirmed(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(
        (row.get("build_scope") or {}).get("state") == "confirmed"
        and bool((row.get("build_scope") or {}).get("subject_build_key"))
        for row in rows
    )


def _confirmed_build_key(row: dict[str, Any]) -> str | None:
    scope = row.get("build_scope") or {}
    key = scope.get("subject_build_key")
    if scope.get("state") != "confirmed" or not key:
        return None
    return str(key)


def _repeated_build_keys(rows: list[dict[str, Any]]) -> set[str]:
    runs_by_build: dict[str, set[str]] = {}
    for row in rows:
        build_key = _confirmed_build_key(row)
        if build_key is not None:
            runs_by_build.setdefault(build_key, set()).add(str(row["run_id"]))
    return {
        build_key for build_key, run_ids in runs_by_build.items()
        if len(run_ids) >= 2
    }


def _contrast_arms_use_distinct_runs(rows: list[dict[str, Any]]) -> bool:
    """Return whether every run represents exactly one of at least two arms."""
    inputs_by_run: dict[str, set[str]] = {}
    target_inputs: set[str] = set()
    for row in rows:
        contrast = row["contrast"]
        target_input = str(contrast.get("target_input") or "")
        run_id = str(row.get("run_id") or "")
        if not target_input or not run_id:
            return False
        target_inputs.add(target_input)
        inputs_by_run.setdefault(run_id, set()).add(target_input)
    return (
        len(target_inputs) >= 2
        and len(inputs_by_run) >= 2
        and all(len(inputs) == 1 for inputs in inputs_by_run.values())
    )


def _contrast_inputs_change_as_expected(rows: list[dict[str, Any]]) -> bool:
    target_inputs = {
        str(row["contrast"].get("target_input") or "")
        for row in rows
    }
    return (
        "" not in target_inputs
        and len(target_inputs) >= 2
        and all(row["contrast"].get("change_as_expected") is True for row in rows)
    )


def verify(
    claim: dict[str, Any],
    observations: Iterable[dict[str, Any]],
    *,
    current_subject_build_key: str | None = None,
) -> dict[str, Any]:
    refs = {str(ref) for ref in claim.get("evidence_refs") or []}
    linked = [row for row in observations if str(row.get("id")) in refs]
    runtime = [row for row in linked if row.get("type") != "static_hint"]
    static = [row for row in linked if row.get("type") == "static_hint"]
    scope_key = (claim.get("scope") or {}).get("subject_build_key")
    contradictions = [
        row for row in linked
        if row.get("relation") == "contradicts"
        and scope_key
        and _confirmed_build_key(row) == str(scope_key)
    ]
    unconfirmed_contradictions = [
        row for row in linked
        if row.get("relation") == "contradicts"
        and _confirmed_build_key(row) is None
    ]
    gaps: list[str] = []
    if unconfirmed_contradictions:
        gaps.append("contradiction_build_scope_unconfirmed")

    level = "static_hint"
    if not linked:
        gaps.append("evidence_missing")
    elif runtime:
        complete = [
            row for row in runtime
            if row.get("run_id") and row.get("action_id")
            and row.get("subject_epoch_ref") and row.get("occurred_at")
            and isinstance(row.get("producer"), dict)
        ]
        if complete:
            level = "observed"
        else:
            gaps.append("correlation_key_incomplete")

        runs = {str(row.get("run_id")) for row in complete}
        repeated_build_keys = _repeated_build_keys(complete)
        claim_build_repeated = bool(
            scope_key and str(scope_key) in repeated_build_keys
        )
        if claim_build_repeated and not contradictions:
            level = "repeated"
        else:
            if len(runs) < 2:
                gaps.append("repeated_runs_missing")
            confirmed_build_keys = {
                key for row in complete
                if (key := _confirmed_build_key(row)) is not None
            }
            if any(_confirmed_build_key(row) is None for row in complete):
                gaps.append("repeated_build_scope_unconfirmed")
            if (
                len(confirmed_build_keys) >= 2
                or bool(
                    scope_key
                    and confirmed_build_keys
                    and str(scope_key) not in confirmed_build_keys
                )
            ):
                gaps.append("repeated_build_scope_mismatch")
            if contradictions:
                gaps.append("repeated_uncontradicted_evidence_missing")

        claim_id = str(claim.get("id") or "")
        contrasts = [
            row for row in complete
            if isinstance(row.get("contrast"), dict)
            and str(row["contrast"].get("claim_id") or "") == claim_id
            and bool(row["contrast"].get("group"))
            and bool(row["contrast"].get("experiment_id"))
            and bool(row["contrast"].get("experiment_instance"))
        ]
        contrast_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        contrast_builds: dict[tuple[str, str, str, str], set[str]] = {}
        unconfirmed_contrasts: list[dict[str, Any]] = []
        for row in contrasts:
            contrast = row["contrast"]
            contrast_identity = (
                claim_id,
                str(contrast["group"]),
                str(contrast["experiment_id"]),
                str(contrast["experiment_instance"]),
            )
            build_key = _confirmed_build_key(row)
            if build_key is None:
                unconfirmed_contrasts.append(row)
                continue
            contrast_builds.setdefault(contrast_identity, set()).add(build_key)
            witness_identity = (*contrast_identity, build_key)
            contrast_groups.setdefault(witness_identity, []).append(row)
        expected_contrasts = [
            (identity, group) for identity, group in contrast_groups.items()
            if _overlap_scope(group) and _contrast_inputs_change_as_expected(group)
        ]
        claim_scope_expected_contrasts = [
            (identity, group) for identity, group in expected_contrasts
            if scope_key and identity[-1] == str(scope_key)
        ]
        controlled_witnesses = [
            (identity, group) for identity, group in claim_scope_expected_contrasts
            if _contrast_arms_use_distinct_runs(group)
        ]
        controlled = bool(controlled_witnesses)
        if unconfirmed_contrasts:
            gaps.append("controlled_contrast_build_scope_unconfirmed")
        if any(
            len(build_keys) >= 2
            or bool(scope_key and build_keys and str(scope_key) not in build_keys)
            for build_keys in contrast_builds.values()
        ):
            gaps.append("controlled_contrast_build_scope_mismatch")
        if level == "repeated" and controlled:
            level = "controlled_contrast"
        else:
            if claim_scope_expected_contrasts and not controlled:
                gaps.append("controlled_contrast_distinct_runs_missing")
            else:
                gaps.append("controlled_contrast_missing")

        independent_readback = False
        for _identity, witness in controlled_witnesses:
            witness_producers = {
                str((row.get("producer") or {}).get("kind") or "")
                for row in witness
                if row.get("independent_readback") is not True
            } - {""}
            readback_producers = {
                str((row.get("producer") or {}).get("kind") or "")
                for row in witness
                if row.get("independent_readback") is True
            } - {""}
            if witness_producers and readback_producers - witness_producers:
                independent_readback = True
                break
        if level == "controlled_contrast" and independent_readback:
            level = "independent_readback"
        else:
            gaps.append("independent_readback_missing")
    elif static:
        level = "static_hint"

    if contradictions:
        state = "contradicted"
    elif not linked:
        state = "missing"
    else:
        confirmed_runtime_build_keys = {
            key for row in runtime
            if (key := _confirmed_build_key(row)) is not None
        }
        if not scope_key or not current_subject_build_key:
            state = "missing"
            gaps.append("build_identity_unconfirmed")
        elif scope_key != current_subject_build_key:
            state = "stale"
        elif runtime and str(scope_key) in confirmed_runtime_build_keys:
            state = "current"
        elif runtime and confirmed_runtime_build_keys:
            state = "missing"
            gaps.append("evidence_build_scope_mismatch")
        elif runtime:
            state = "missing"
            gaps.append("evidence_build_scope_unconfirmed")
        else:
            state = "current"

    if (claim.get("provenance") or {}).get("source") == "ai":
        if LEVELS.index(level) > LEVELS.index("observed"):
            level = "observed"
            gaps.append("ai_evidence_level_capped")
    if any(row.get("correlation_state") == "legacy_derived" for row in linked):
        if LEVELS.index(level) > LEVELS.index("observed"):
            level = "observed"
        gaps.append("legacy_derived_evidence_capped")

    return {
        "claim_id": claim.get("id"),
        "evidence_level": level,
        "evidence_state": state,
        "observation_count": len(linked),
        "run_count": len({row.get("run_id") for row in runtime}),
        "gaps": list(dict.fromkeys(gaps)),
        "verified": (
            claim.get("status") == "active"
            and state == "current"
            and LEVELS.index(level) >= LEVELS.index("observed")
        ),
    }
