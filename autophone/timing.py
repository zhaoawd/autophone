"""Unified, fail-closed timing records for UI evaluation iterations.

The recorder is intentionally an in-memory Module.  Callers own persistence so
an operation that fails before its normal receipt is committed can still write
the timing snapshot from an exception handler.  External operations are kept
behind existing injectable adapters; this Module only measures their boundary.
"""

from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

from autophone.contracts import validate_or_raise, write_json


CACHE_MODE_CLEAN = "clean"
CACHE_MODE_ACCELERATED_COLD = "accelerated_cold"
CACHE_MODE_ACCELERATED_WARM = "accelerated_warm"
CACHE_MODE_JS_ONLY = "js_only"
CACHE_MODE_PREPARED_ACTUAL = "prepared_actual"

CACHE_MODES = frozenset({
    CACHE_MODE_CLEAN,
    CACHE_MODE_ACCELERATED_COLD,
    CACHE_MODE_ACCELERATED_WARM,
    CACHE_MODE_JS_ONLY,
    CACHE_MODE_PREPARED_ACTUAL,
})
NON_CLEAN_CACHE_MODES = CACHE_MODES - {CACHE_MODE_CLEAN}

SCENARIO_CLEAN = "clean"
SCENARIO_ACCELERATED_COLD = "accelerated_cold"
SCENARIO_ACCELERATED_WARM = "accelerated_warm"
SCENARIO_SINGLE_JS_CHANGE_WARM = "single_js_change_warm"
SCENARIO_JS_ONLY_REPACKAGE = "js_only_repackage"
SCENARIO_PREPARED_ACTUAL = "prepared_actual"

COMPARISON_SCENARIOS = frozenset({
    SCENARIO_CLEAN,
    SCENARIO_ACCELERATED_COLD,
    SCENARIO_ACCELERATED_WARM,
    SCENARIO_SINGLE_JS_CHANGE_WARM,
    SCENARIO_JS_ONLY_REPACKAGE,
    SCENARIO_PREPARED_ACTUAL,
})

_DEFAULT_SCENARIO_BY_CACHE_MODE = {
    CACHE_MODE_CLEAN: SCENARIO_CLEAN,
    CACHE_MODE_ACCELERATED_COLD: SCENARIO_ACCELERATED_COLD,
    CACHE_MODE_ACCELERATED_WARM: SCENARIO_ACCELERATED_WARM,
    CACHE_MODE_JS_ONLY: SCENARIO_JS_ONLY_REPACKAGE,
    CACHE_MODE_PREPARED_ACTUAL: SCENARIO_PREPARED_ACTUAL,
}

_CACHE_MODE_BY_SCENARIO = {
    **{scenario: mode for mode, scenario in _DEFAULT_SCENARIO_BY_CACHE_MODE.items()},
    SCENARIO_SINGLE_JS_CHANGE_WARM: CACHE_MODE_ACCELERATED_WARM,
}

STAGE_JS_ONLY_REPACKAGE = "js_only_repackage"
STAGE_PACKAGE_STORE_ADD = "package_store_add"
STAGE_PACKAGE_STORE_HASH = "package_store_hash"
STAGE_SCP = "scp"
STAGE_IPA_INSTALL = "ipa_install"
STAGE_UICACHE = "uicache"
STAGE_APP_RESTART = "app_restart"
STAGE_STATE_READY = "state_ready"
STAGE_ACTUAL_CAPTURE = "actual_capture"
STAGE_END_TO_END = "end_to_end"

STAGES = frozenset({
    STAGE_JS_ONLY_REPACKAGE,
    STAGE_PACKAGE_STORE_ADD,
    STAGE_PACKAGE_STORE_HASH,
    STAGE_SCP,
    STAGE_IPA_INSTALL,
    STAGE_UICACHE,
    STAGE_APP_RESTART,
    STAGE_STATE_READY,
    STAGE_ACTUAL_CAPTURE,
    STAGE_END_TO_END,
})

_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JS_SOURCE_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})


class IterationTimingError(ValueError):
    """The requested timing operation would produce an ambiguous record."""


def clean_acceptance_eligible(cache_mode: str) -> bool:
    """Return the formal-acceptance eligibility implied by one cache mode."""
    if cache_mode not in CACHE_MODES:
        raise IterationTimingError(f"unsupported cache_mode: {cache_mode!r}")
    return cache_mode == CACHE_MODE_CLEAN


def resolve_comparison_scenario(cache_mode: str, declared: object = None) -> str:
    """Resolve one timing row to an explicit experiment scenario.

    ``accelerated_warm`` is intentionally not inferred as a single-JS-change
    run.  That scenario changes source identity and therefore requires an
    explicit declaration plus derivation evidence.
    """
    clean_acceptance_eligible(cache_mode)
    scenario = (
        _DEFAULT_SCENARIO_BY_CACHE_MODE[cache_mode]
        if declared is None
        else declared
    )
    if not isinstance(scenario, str) or scenario not in COMPARISON_SCENARIOS:
        raise IterationTimingError(
            f"unsupported comparison_scenario: {scenario!r}",
        )
    expected_mode = _CACHE_MODE_BY_SCENARIO[scenario]
    if expected_mode != cache_mode:
        raise IterationTimingError(
            f"comparison_scenario={scenario!r} requires "
            f"cache_mode={expected_mode!r}; got {cache_mode!r}",
        )
    return scenario


def normalize_single_js_derivation(
    value: object,
    *,
    derived_source_tree_sha256: str | None,
) -> dict[str, Any]:
    """Validate the hash-bound relationship used by the JS experiment.

    This relation does not claim that a source tree diff was executed by this
    Module.  It binds the baseline and derived frozen tree identities to one
    declared JavaScript-path change so the summary can cross-check every row in
    the two-source matrix without pretending both trees are identical.
    """
    if not isinstance(value, Mapping):
        raise IterationTimingError(
            "single_js_change_warm requires source_derivation evidence",
        )
    if value.get("kind") != "single_js_change":
        raise IterationTimingError(
            "source_derivation.kind must be 'single_js_change'",
        )
    baseline = value.get("baseline_source_tree_sha256")
    derived = value.get("derived_source_tree_sha256")
    if not isinstance(baseline, str) or _SHA256.fullmatch(baseline) is None:
        raise IterationTimingError(
            "source_derivation baseline_source_tree_sha256 must be SHA-256",
        )
    if not isinstance(derived, str) or _SHA256.fullmatch(derived) is None:
        raise IterationTimingError(
            "source_derivation derived_source_tree_sha256 must be SHA-256",
        )
    if baseline == derived:
        raise IterationTimingError(
            "single-JS source derivation must change the frozen source tree",
        )
    if derived_source_tree_sha256 is None or derived != derived_source_tree_sha256:
        raise IterationTimingError(
            "source_derivation derived identity differs from the timing receipt",
        )
    changes = value.get("changes")
    if not isinstance(changes, list) or len(changes) != 1:
        raise IterationTimingError(
            "single_js_change source_derivation must contain exactly one change",
        )
    change = changes[0]
    if not isinstance(change, Mapping):
        raise IterationTimingError("source_derivation change must be an object")
    path = change.get("path")
    if not isinstance(path, str) or not path or "\\" in path:
        raise IterationTimingError(
            "source_derivation change path must be a relative POSIX path",
        )
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or any(
        part in {"", ".", ".."} for part in pure_path.parts
    ):
        raise IterationTimingError(
            "source_derivation change path must stay within the frozen source root",
        )
    if pure_path.suffix.lower() not in _JS_SOURCE_SUFFIXES:
        raise IterationTimingError(
            "single_js_change path must identify a JavaScript/TypeScript source file",
        )
    before = change.get("before_sha256")
    after = change.get("after_sha256")
    if not isinstance(before, str) or _SHA256.fullmatch(before) is None:
        raise IterationTimingError(
            "source_derivation before_sha256 must be SHA-256",
        )
    if not isinstance(after, str) or _SHA256.fullmatch(after) is None:
        raise IterationTimingError(
            "source_derivation after_sha256 must be SHA-256",
        )
    if before == after:
        raise IterationTimingError(
            "single_js_change before_sha256 and after_sha256 must differ",
        )
    return {
        "kind": "single_js_change",
        "baseline_source_tree_sha256": baseline,
        "derived_source_tree_sha256": derived,
        "changes": [{
            "path": path,
            "before_sha256": before,
            "after_sha256": after,
        }],
    }


def _exception_failure_code(error: BaseException) -> str:
    declared = getattr(error, "code", None)
    if isinstance(declared, str) and _FAILURE_CODE.fullmatch(declared):
        return declared
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    if not name or not name[0].isalpha():
        return "operation_failed"
    return name[:128]


@dataclass(frozen=True)
class StageTiming:
    stage: str
    status: str
    elapsed_seconds: float
    failure_code: str | None
    cache_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "failure_code": self.failure_code,
            "cache_mode": self.cache_mode,
        }


class IterationTimingRecorder:
    """Measure one evaluation path with a frozen cache/derivation mode.

    A recorder rejects overlapping attempts of the same stage and rejects
    repeated completed stages.  This keeps one stage name bound to one duration
    instead of silently merging retries or double-counting a source operation.
    Create a new recorder for a retry.
    """

    def __init__(
        self,
        cache_mode: str,
        *,
        source_tree_sha256: str | None = None,
        comparison_scenario: str | None = None,
        source_derivation: Mapping[str, Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        clean_acceptance_eligible(cache_mode)
        self.cache_mode = cache_mode
        if source_tree_sha256 is not None and (
            not isinstance(source_tree_sha256, str)
            or _SHA256.fullmatch(source_tree_sha256) is None
        ):
            raise IterationTimingError("source_tree_sha256 must be SHA-256")
        self.source_tree_sha256 = source_tree_sha256
        self.comparison_scenario = resolve_comparison_scenario(
            cache_mode,
            comparison_scenario,
        )
        if self.comparison_scenario == SCENARIO_SINGLE_JS_CHANGE_WARM:
            self.source_derivation = normalize_single_js_derivation(
                source_derivation,
                derived_source_tree_sha256=source_tree_sha256,
            )
        elif source_derivation is not None:
            raise IterationTimingError(
                "source_derivation is only valid for single_js_change_warm",
            )
        else:
            self.source_derivation = None
        self._monotonic = monotonic
        self._records: list[StageTiming] = []
        self._active: set[str] = set()

    @property
    def records(self) -> tuple[StageTiming, ...]:
        return tuple(self._records)

    def is_active(self, stage: str) -> bool:
        return stage in self._active

    @property
    def has_active_stages(self) -> bool:
        return bool(self._active)

    def _elapsed(self, started: float) -> float:
        elapsed = float(self._monotonic()) - started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise IterationTimingError(
                "monotonic clock returned a negative or non-finite duration",
            )
        return elapsed

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        if stage not in STAGES:
            raise IterationTimingError(f"unsupported timing stage: {stage!r}")
        if stage in self._active or any(row.stage == stage for row in self._records):
            raise IterationTimingError(
                f"timing stage already active or recorded: {stage}",
            )
        self._active.add(stage)
        started = float(self._monotonic())
        if not math.isfinite(started):
            self._active.remove(stage)
            raise IterationTimingError("monotonic clock returned a non-finite value")
        try:
            yield
        except BaseException as error:
            elapsed = self._elapsed(started)
            self._records.append(StageTiming(
                stage=stage,
                status="failed",
                elapsed_seconds=elapsed,
                failure_code=_exception_failure_code(error),
                cache_mode=self.cache_mode,
            ))
            raise
        else:
            elapsed = self._elapsed(started)
            self._records.append(StageTiming(
                stage=stage,
                status="succeeded",
                elapsed_seconds=elapsed,
                failure_code=None,
                cache_mode=self.cache_mode,
            ))
        finally:
            self._active.remove(stage)

    def to_dict(self) -> dict[str, Any]:
        if self._active:
            raise IterationTimingError(
                "cannot snapshot timing while a stage is active: "
                + ", ".join(sorted(self._active)),
            )
        failed = any(row.status == "failed" for row in self._records)
        completed_end_to_end = any(
            row.stage == STAGE_END_TO_END and row.status == "succeeded"
            for row in self._records
        )
        payload = {
            "schema_version": "ui-evaluation-iteration-timing/v1",
            "status": "failed" if failed else "succeeded",
            "cache_mode": self.cache_mode,
            "comparison_scenario": self.comparison_scenario,
            "clean_acceptance_eligible": (
                not failed
                and completed_end_to_end
                and clean_acceptance_eligible(self.cache_mode)
            ),
            "stages": [row.to_dict() for row in self._records],
        }
        if self.source_tree_sha256 is not None:
            payload["source_tree_sha256"] = self.source_tree_sha256
        if self.source_derivation is not None:
            payload["source_derivation"] = self.source_derivation
        validate_or_raise("ui-evaluation-iteration-timing.v1.json", payload)
        return payload

    def write(self, path: Path) -> dict[str, Any]:
        payload = self.to_dict()
        write_json(Path(path), payload)
        return payload


def measure_stage(
    timing: IterationTimingRecorder | None,
    stage: str,
):
    """Return a no-op context when a caller did not request timing."""
    if timing is None:
        return nullcontext()
    return timing.stage(stage)


def assert_document_clean_acceptance_eligible(
    document: Mapping[str, Any],
) -> None:
    """Reject any document that explicitly identifies a non-clean path.

    Historical clean build receipts predate the explicit fields, so absence is
    accepted only when no known non-clean marker is present.  New non-clean
    receipts cannot upgrade themselves by setting the boolean to true because
    cache_mode and build_acceleration are checked independently.
    """
    mode = document.get("cache_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in CACHE_MODES:
            raise IterationTimingError(f"unsupported cache_mode: {mode!r}")
        if mode in NON_CLEAN_CACHE_MODES:
            raise IterationTimingError(
                f"formal acceptance requires cache_mode={CACHE_MODE_CLEAN}; got {mode}",
            )
    schema_version = str(document.get("schema_version") or "")
    if "js-reverification-build-receipt" in schema_version:
        raise IterationTimingError(
            "formal acceptance rejects JS-only repackaging receipts",
        )
    capture_input = document.get("capture_input")
    if (
        isinstance(capture_input, Mapping)
        and capture_input.get("mode") == "prepared_bundle"
    ):
        raise IterationTimingError(
            "formal acceptance rejects Prepared Actual recomputation",
        )
    stages = document.get("stages")
    if isinstance(stages, list) and any(
        isinstance(row, Mapping) and row.get("stage") == STAGE_JS_ONLY_REPACKAGE
        for row in stages
    ):
        raise IterationTimingError(
            "formal acceptance rejects JS-only repackaging timing evidence",
        )
    if document.get("build_acceleration") is not None:
        acceleration = document.get("build_acceleration")
        acceleration_mode = (
            str(acceleration.get("mode", "unknown"))
            if isinstance(acceleration, Mapping)
            else "unknown"
        )
        raise IterationTimingError(
            "formal acceptance rejects build acceleration "
            f"(mode={acceleration_mode})",
        )
    eligibility = document.get("clean_acceptance_eligible")
    if eligibility is False:
        raise IterationTimingError(
            "document declares clean_acceptance_eligible=false",
        )
    if eligibility is not None and eligibility is not True:
        raise IterationTimingError(
            "clean_acceptance_eligible must be a boolean when present",
        )


__all__ = [
    "CACHE_MODE_ACCELERATED_COLD",
    "CACHE_MODE_ACCELERATED_WARM",
    "CACHE_MODE_CLEAN",
    "CACHE_MODE_JS_ONLY",
    "CACHE_MODE_PREPARED_ACTUAL",
    "CACHE_MODES",
    "COMPARISON_SCENARIOS",
    "IterationTimingError",
    "IterationTimingRecorder",
    "NON_CLEAN_CACHE_MODES",
    "SCENARIO_ACCELERATED_COLD",
    "SCENARIO_ACCELERATED_WARM",
    "SCENARIO_CLEAN",
    "SCENARIO_JS_ONLY_REPACKAGE",
    "SCENARIO_PREPARED_ACTUAL",
    "SCENARIO_SINGLE_JS_CHANGE_WARM",
    "STAGE_ACTUAL_CAPTURE",
    "STAGE_APP_RESTART",
    "STAGE_END_TO_END",
    "STAGE_IPA_INSTALL",
    "STAGE_JS_ONLY_REPACKAGE",
    "STAGE_PACKAGE_STORE_ADD",
    "STAGE_PACKAGE_STORE_HASH",
    "STAGE_SCP",
    "STAGE_STATE_READY",
    "STAGE_UICACHE",
    "assert_document_clean_acceptance_eligible",
    "clean_acceptance_eligible",
    "measure_stage",
    "normalize_single_js_derivation",
    "resolve_comparison_scenario",
]
