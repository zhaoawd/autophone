"""Declared negative controls: the generic discrimination evidence contract.

A negative control proves that a Case's assertion has discriminating power
(ADR-0001): the same Case runs twice on the same Build, once unchanged
(positive Run) and once with a declared minimal contrasting state (negative
Run).  The evidence counts only when the positive Run passes and the negative
Run fails **on the declared assertion step or Operation witness**.

This module is pure (no device, no Control Plane):

* the declaration file ``apps/<app>/discrimination/negative_controls.yaml``
  (``negative-controls.v1.json``) and its semantic resolution against Recipe
  steps (``resolve_negative_control``);
* the static constraints a negative-control Run Spec must satisfy;
* the judgement rule (``judge_negative_control``) that turns the two settled
  Run Results into one probe in the ``runs/negctl/<build_label>/artifact.json``
  format the Promotion Gate already reads;
* the artifact merge writer.

The negative state is chosen only by the declaration.  Nothing here decides a
perturbation at run time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from autophone.assets.build_identity import (
    build_identity_unconfirmed,
    label_build_mismatch,
    subject_build_key,
)

NEGATIVE_CONTROLS_SCHEMA = "autophone.negative-controls/1"
NEGATIVE_CONTROLS_FILE = Path("discrimination") / "negative_controls.yaml"
PROBE_SCHEMA = "autophone.negctl-probe/2"
ARTIFACT_NAME = "artifact.json"

#: The only negative verdict that counts as discrimination evidence.
VERDICT_COUNTED = "failed_on_expected_step"
#: The negative Run passed: the assertion did not discriminate (toothless).
VERDICT_PASSED = "passed"
#: Verdicts that neither prove nor disprove discriminating power.
INCONCLUSIVE_VERDICTS = frozenset({
    "not_run",
    "not_applied",
    "failed_elsewhere",
    "failed_unexpected_kind",
    "error",
    "blocked",
    "skipped",
    "identity_unconfirmed",
    "witness_imprecise",
})
NEGATIVE_VERDICTS = frozenset({VERDICT_COUNTED, VERDICT_PASSED}) | INCONCLUSIVE_VERDICTS

#: 未声明 ``expect.fail_kinds`` 时反向 Run 必须落在的失败归类。设施类归类（如 AX provider
#: 缺失时的 ``ax_unavailable``）即使出现在断言步骤上也不计入。
DEFAULT_FAIL_KINDS = ("assertion_failed",)
JOINT_EXPECT_PLANES = frozenset({"api", "readback", "joint"})
ALL_EXPECT_PLANES = frozenset({"ui"}) | JOINT_EXPECT_PLANES
#: Infrastructure / missing-evidence fail kinds that MUST NEVER count as discrimination teeth.
NON_DISCRIMINATING_JOINT_FAIL_KINDS = frozenset({
    "api_evidence_missing",
    "entity_readback_missing",
    "entity_binding_unresolved",
    "ui_error",
})
DEFAULT_API_FAIL_KINDS = (
    "entity_api_mismatch",
    "optimistic_ui_api_failed",
    "api_assertion_failed",
)
DEFAULT_READBACK_FAIL_KINDS = (
    "entity_readback_mismatch",
    "readback_assertion_failed",
)
DEFAULT_JOINT_FAIL_KINDS = (
    "entity_api_mismatch",
    "optimistic_ui_api_failed",
    "entity_readback_mismatch",
    "api_assertion_failed",
    "readback_assertion_failed",
)
_LOCATOR_KINDS = ("operation", "action")
#: Selector keys that select Cases other than by explicit ID.
_BROAD_SELECTOR_KEYS = ("tag", "module", "suite", "flow")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RN_TRIPLET_KEYS = ("base_ipa_sha256", "main_jsbundle_sha256", "assets_tree_sha256")
_ARTIFACT_DIGEST_DECL_KEYS = (
    "artifact_content_digest",
    "artifact_sha256",
    "rn_candidate_digests",
    "candidate_identity",
    *_RN_TRIPLET_KEYS,
)


def normalize_sha256(value: Any) -> str | None:
    """Return a canonical lowercase 64-hex SHA-256 digest, or ``None``."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SHA256_RE.match(normalized) else None


def rn_candidate_digests_from(mapping: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Extract the pure-content RN candidate SHA-256 triplet (never ``target_fingerprint``)."""
    if not isinstance(mapping, Mapping):
        return None
    base = normalize_sha256(mapping.get("base_ipa_sha256"))
    bundle = normalize_sha256(mapping.get("main_jsbundle_sha256"))
    assets = normalize_sha256(mapping.get("assets_tree_sha256"))
    if not (base and bundle and assets):
        return None
    return {
        "base_ipa_sha256": base,
        "main_jsbundle_sha256": bundle,
        "assets_tree_sha256": assets,
    }


def rn_candidate_content_digest(
    base_ipa_sha256: Any,
    main_jsbundle_sha256: Any,
    assets_tree_sha256: Any,
) -> str | None:
    """Canonical pure-content digest for an RN candidate (shared contract with M2)."""
    base = normalize_sha256(base_ipa_sha256)
    bundle = normalize_sha256(main_jsbundle_sha256)
    assets = normalize_sha256(assets_tree_sha256)
    if not (base and bundle and assets):
        return None
    canonical = json.dumps(
        {
            "kind": "rn_candidate",
            "base_ipa_sha256": base,
            "main_jsbundle_sha256": bundle,
            "assets_tree_sha256": assets,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _declares_artifact_digest(mapping: Mapping[str, Any] | None) -> bool:
    if not isinstance(mapping, Mapping):
        return False
    if mapping.get("kind") == "rn_candidate":
        return True
    return any(mapping.get(key) is not None for key in _ARTIFACT_DIGEST_DECL_KEYS)


def canonical_artifact_content_digest(identity: Mapping[str, Any] | None) -> str | None:
    """Derive the canonical lowercase 64-hex pure-content digest from an identity mapping.

    Supports ``ipa_build`` (``artifact_sha256``), ``rn_candidate`` (triplet of
    ``base_ipa_sha256``, ``main_jsbundle_sha256``, ``assets_tree_sha256`` via
    direct keys, ``rn_candidate_digests``, or ``candidate_identity``), and
    explicit ``artifact_content_digest``.  Strictly ignores ``target_fingerprint``
    and fails closed (returns ``None``) on malformed or contradictory fields.
    """
    if not isinstance(identity, Mapping):
        return None

    rn_source: Mapping[str, Any] | None = None
    has_rn_marker = identity.get("kind") == "rn_candidate"
    for nested_key in ("rn_candidate_digests", "candidate_identity"):
        nested = identity.get(nested_key)
        if nested is not None:
            has_rn_marker = True
            if not isinstance(nested, Mapping):
                return None
            if rn_source is None:
                rn_source = nested
            else:
                first_triplet = rn_candidate_digests_from(rn_source)
                second_triplet = rn_candidate_digests_from(nested)
                if first_triplet is None or second_triplet != first_triplet:
                    return None
    if any(identity.get(key) is not None for key in _RN_TRIPLET_KEYS):
        has_rn_marker = True
        if rn_source is None:
            rn_source = identity
        else:
            first_triplet = rn_candidate_digests_from(rn_source)
            second_triplet = rn_candidate_digests_from(identity)
            if first_triplet is None or second_triplet != first_triplet:
                return None

    explicit_raw = identity.get("artifact_content_digest")
    explicit = normalize_sha256(explicit_raw) if explicit_raw is not None else None
    if explicit_raw is not None and explicit is None:
        return None

    if has_rn_marker:
        triplet = rn_candidate_digests_from(rn_source)
        if triplet is None:
            return None
        base_raw = identity.get("artifact_sha256")
        if base_raw is not None:
            base_norm = normalize_sha256(base_raw)
            if base_norm != triplet["base_ipa_sha256"]:
                return None
        derived = rn_candidate_content_digest(
            triplet["base_ipa_sha256"],
            triplet["main_jsbundle_sha256"],
            triplet["assets_tree_sha256"],
        )
        if explicit is not None and explicit != derived:
            return None
        return derived

    sha_raw = identity.get("artifact_sha256")
    if sha_raw is not None:
        sha = normalize_sha256(sha_raw)
        if sha is None:
            return None
        if explicit is not None and explicit != sha:
            return None
        return sha

    return explicit


def probe_artifact_content_digest(proof: Mapping[str, Any] | None) -> str | None:
    """Extract and cross-check the exact artifact content digest recorded on a probe."""
    if not isinstance(proof, Mapping):
        return None
    sources: list[Mapping[str, Any]] = []
    build_identity = proof.get("build_identity")
    if isinstance(build_identity, Mapping) and _declares_artifact_digest(build_identity):
        sources.append(build_identity)
    top_level = {
        key: proof[key]
        for key in (*_ARTIFACT_DIGEST_DECL_KEYS, "kind")
        if key in proof and proof[key] is not None
    }
    if top_level and _declares_artifact_digest(top_level):
        sources.append(top_level)
    build_identity_ref = proof.get("build_identity_ref")
    if isinstance(build_identity_ref, Mapping) and _declares_artifact_digest(build_identity_ref):
        sources.append(build_identity_ref)
    if not sources:
        return None
    digests: set[str] = set()
    for source in sources:
        digest = canonical_artifact_content_digest(source)
        if digest is None:
            return None
        digests.add(digest)
    return next(iter(digests)) if len(digests) == 1 else None


def probe_binding_sha256(proof: Mapping[str, Any] | None) -> str | None:
    """Extract and cross-check the symbol ``binding_sha256`` recorded on a probe."""
    if not isinstance(proof, Mapping):
        return None
    raw_values: list[Any] = []
    if proof.get("binding_sha256") is not None:
        raw_values.append(proof.get("binding_sha256"))
    recipe_identity = proof.get("recipe_identity")
    if isinstance(recipe_identity, Mapping) and recipe_identity.get("binding_sha256") is not None:
        raw_values.append(recipe_identity.get("binding_sha256"))
    if not raw_values:
        return None
    normalized: set[str] = set()
    for raw in raw_values:
        digest = normalize_sha256(raw)
        if digest is None:
            return None
        normalized.add(digest)
    return next(iter(normalized)) if len(normalized) == 1 else None


class NegativeControlError(ValueError):
    """A negative control declaration or request cannot be trusted."""


# ── declarations ──────────────────────────────────────────────────────────────
def negative_controls_path(profile: Any) -> Path:
    return Path(profile.profile_dir) / NEGATIVE_CONTROLS_FILE


def load_negative_controls(profile: Any) -> list[dict[str, Any]]:
    """Load and validate an App's declarations; a missing file declares none."""
    import yaml

    from autophone.contracts import ContractError, validate_or_raise

    path = negative_controls_path(profile)
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise NegativeControlError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise NegativeControlError(f"{path}: document must be a mapping")
    try:
        validate_or_raise("negative-controls.v1.json", doc)
    except ContractError as exc:
        raise NegativeControlError(f"{path}: {exc}") from exc
    controls = [copy.deepcopy(row) for row in doc["controls"]]
    seen: set[str] = set()
    for row in controls:
        if row["id"] in seen:
            raise NegativeControlError(f"{path}: duplicate negative control id {row['id']!r}")
        seen.add(row["id"])
        row["case_id"] = str(row["case_id"]).upper()
    return controls


def find_negative_control(profile: Any, case_id: str, control_id: str) -> dict[str, Any]:
    key = str(case_id).upper()
    for row in load_negative_controls(profile):
        if row["id"] == control_id:
            if row["case_id"] != key:
                raise NegativeControlError(
                    f"negative control {control_id!r} belongs to Case {row['case_id']}, not {key}"
                )
            return row
    raise NegativeControlError(f"negative control not declared: {control_id!r} (Case {key})")


def declaration_sha256(control: Mapping[str, Any]) -> str:
    """Digest of one declaration entry; binds the executed perturbation to it."""
    canonical = json.dumps(control, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── resolution against Recipe steps ───────────────────────────────────────────
def _step_ref(step: Any) -> tuple[str, str] | None:
    if not isinstance(step, dict):
        return None
    for kind in _LOCATOR_KINDS:
        if kind in step:
            return kind, str(step[kind])
    return None


def _locator_ref(locator: Mapping[str, Any]) -> tuple[str, str]:
    for kind in _LOCATOR_KINDS:
        if kind in locator:
            return kind, str(locator[kind])
    raise NegativeControlError(f"step locator names neither operation nor action: {dict(locator)!r}")


def describe_locator(locator: Mapping[str, Any]) -> str:
    kind, name = _locator_ref(locator)
    occurrence = locator.get("occurrence")
    return f"{kind} {name}" + (f" #{occurrence}" if occurrence else "")


def locate_step(steps: list[dict[str, Any]], locator: Mapping[str, Any]) -> int:
    """Index of the one step the locator names; fail-closed on none or ambiguity."""
    ref = _locator_ref(locator)
    matches = [index for index, step in enumerate(steps) if _step_ref(step) == ref]
    occurrence = locator.get("occurrence")
    if not matches:
        raise NegativeControlError(f"step {describe_locator(locator)} is not in the Recipe")
    if occurrence is None:
        if len(matches) > 1:
            raise NegativeControlError(
                f"step {describe_locator(locator)} matches {len(matches)} steps; declare occurrence"
            )
        return matches[0]
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise NegativeControlError(f"occurrence must be a positive integer: {occurrence!r}")
    if occurrence > len(matches):
        raise NegativeControlError(
            f"step {describe_locator(locator)} occurs only {len(matches)} time(s)"
        )
    return matches[occurrence - 1]


def expect_plane_of(expect: Mapping[str, Any] | None) -> str:
    """Return the normalized expected discrimination plane (defaults to ``'ui'``)."""
    raw = (expect or {}).get("plane")
    return str(raw) if raw is not None else "ui"


def is_joint_plane_expect(expect: Mapping[str, Any] | None) -> bool:
    """True when the negative control expects failure on api/readback/joint planes."""
    return expect_plane_of(expect) in JOINT_EXPECT_PLANES


def _default_fail_kinds_for_plane(plane: str) -> tuple[str, ...]:
    if plane == "api":
        return DEFAULT_API_FAIL_KINDS
    if plane == "readback":
        return DEFAULT_READBACK_FAIL_KINDS
    if plane == "joint":
        return DEFAULT_JOINT_FAIL_KINDS
    return DEFAULT_FAIL_KINDS


def _describe_expect(expect: Mapping[str, Any]) -> str:
    plane = expect_plane_of(expect)
    step = expect.get("step")
    if isinstance(step, Mapping) and any(kind in step for kind in _LOCATOR_KINDS):
        step_desc = describe_locator(step)
        return f"{plane}:{step_desc}" if plane != "ui" else step_desc
    return f"plane={plane}"


@dataclass(frozen=True)
class ResolvedControl:
    """A declaration applied to one concrete Recipe step list."""

    steps: list[dict[str, Any]]
    perturbed_index: int
    expected_index: int | None
    expected_index_after: int | None
    operation_identity: dict[str, Any] | None = None
    witness: dict[str, Any] | None = None
    expect_plane: str = "ui"

    def record(self, control: Mapping[str, Any], base_steps: list[dict[str, Any]]) -> dict[str, Any]:
        perturbation = control["perturbation"]
        out: dict[str, Any] = {
            "kind": perturbation["kind"],
            "step_index": self.perturbed_index,
            "step": copy.deepcopy(base_steps[self.perturbed_index]),
        }
        if perturbation["kind"] == "replace_args":
            out["args"] = copy.deepcopy(perturbation["args"])
        expect: dict[str, Any] = {
            "step_index": self.expected_index_after,
        }
        if self.expected_index is not None:
            expect["base_step_index"] = self.expected_index
            expect["step"] = copy.deepcopy(base_steps[self.expected_index])
        if self.expect_plane != "ui":
            expect["plane"] = self.expect_plane
        default_kinds = _default_fail_kinds_for_plane(self.expect_plane)
        expect["fail_kinds"] = list(control["expect"].get("fail_kinds") or default_kinds)
        record = {"perturbation": out, "expect": expect}
        if self.operation_identity is not None and self.witness is not None:
            out["target"] = self.operation_identity["name"]
            out["args"] = copy.deepcopy(self.steps[self.perturbed_index].get("args") or {})
            expect["fail_kinds"] = ["operation_witness_failed"]
            expect["positive_witness_stage"] = self.witness["expect_stage"]
            expect["witness_stage"] = self.witness["negative_control"]["expect_stage"]
            record["operation_identity"] = copy.deepcopy(self.operation_identity)
        return record


def resolve_negative_control(
    steps: list[dict[str, Any]],
    control: Mapping[str, Any],
    *,
    operations: Mapping[str, Mapping[str, Any]] | None = None,
    app: str | None = None,
) -> ResolvedControl:
    """Apply a declaration to Recipe steps; never mutates ``steps``.

    Rules (all fail-closed):
    - the perturbed step resolves to exactly one step;
    - when ``expect.plane`` is ``ui`` (default), ``expect.step`` must resolve to
      exactly one assertion step (name starting with ``assert``) strictly after
      the perturbed step (or the same Operation step for ``flip_witness``);
    - when ``expect.plane`` is ``api`` / ``readback`` / ``joint``, ``expect.step``
      is optional (if present, it must still come strictly after the perturbed step);
    - ``replace_args`` must change at least one parameter value;
    - ``flip_witness`` changes the same Operation step using only its declared
      witness negative control and validated effective parameters.
    """
    if not isinstance(steps, list) or not steps:
        raise NegativeControlError("Recipe has no steps")
    perturbation = control["perturbation"]
    expect_cfg = control["expect"]
    plane = expect_plane_of(expect_cfg)
    if plane not in ALL_EXPECT_PLANES:
        raise NegativeControlError(f"unsupported expect.plane {plane!r}")
    perturbed = locate_step(steps, perturbation["step"])
    raw_expect_step = expect_cfg.get("step")

    if perturbation["kind"] == "flip_witness":
        from autophone.assets.operation_witness import operation_identity
        from autophone.assets.operations import _validate_args, OperationError

        if plane != "ui":
            raise NegativeControlError("flip_witness requires expect.plane='ui'")
        if not isinstance(raw_expect_step, Mapping):
            raise NegativeControlError("flip_witness requires expect.step")
        expected = locate_step(steps, raw_expect_step)
        kind, name = _locator_ref(raw_expect_step)
        if "fail_kinds" in expect_cfg:
            raise NegativeControlError("flip_witness cannot override protocol fail_kinds")
        if kind != "operation" or expected != perturbed:
            raise NegativeControlError("flip_witness must target and expect the same Operation step")
        op = (operations or {}).get(name)
        witness = (op or {}).get("witness") or {}
        flip = witness.get("negative_control") or {}
        if not op or not witness.get("expect_stage") or not flip.get("expect_stage"):
            raise NegativeControlError("flip_witness requires a declared Operation witness negative_control")
        if flip["expect_stage"] == witness["expect_stage"]:
            raise NegativeControlError("flip_witness stages must differ")
        try:
            current = _validate_args(dict(op), dict(steps[perturbed].get("args") or {}))
            updated = _validate_args(dict(op), {**current, flip["flip_arg"]: flip["flip_to"]})
            identity = operation_identity(app or "", op)
        except (ValueError, KeyError, OperationError) as exc:
            raise NegativeControlError(f"flip_witness contract invalid: {exc}") from exc
        if current == updated:
            raise NegativeControlError("flip_witness does not change effective arguments")
        out = copy.deepcopy(steps)
        out[perturbed]["args"] = updated
        return ResolvedControl(out, perturbed, expected, expected, identity, copy.deepcopy(witness))

    expected_idx: int | None = None
    if plane == "ui":
        if not isinstance(raw_expect_step, Mapping):
            raise NegativeControlError("expect.step is required when expect.plane is 'ui'")
        expected_idx = locate_step(steps, raw_expect_step)
        kind, name = _locator_ref(raw_expect_step)
        if not name.startswith("assert"):
            raise NegativeControlError(
                f"expected step {kind} {name} is not an assertion (name must start with 'assert')"
            )
        if expected_idx <= perturbed:
            raise NegativeControlError(
                "expected assertion step must come after the perturbed step "
                f"(perturbed index {perturbed}, expected index {expected_idx})"
            )
    elif raw_expect_step is not None:
        if not isinstance(raw_expect_step, Mapping):
            raise NegativeControlError("expect.step must be a step locator mapping")
        expected_idx = locate_step(steps, raw_expect_step)
        if expected_idx <= perturbed:
            raise NegativeControlError(
                "expected step must come after the perturbed step "
                f"(perturbed index {perturbed}, expected index {expected_idx})"
            )

    out = copy.deepcopy(steps)
    if perturbation["kind"] == "skip_step":
        del out[perturbed]
        expected_after = (expected_idx - 1) if expected_idx is not None else None
        return ResolvedControl(out, perturbed, expected_idx, expected_after, expect_plane=plane)
    if perturbation["kind"] == "replace_args":
        target = out[perturbed]
        replacement = copy.deepcopy(perturbation["args"])
        if "operation" in target:
            current = dict(target.get("args") or {})
            updated = {**current, **replacement}
            if updated == current:
                raise NegativeControlError("replace_args does not change the step arguments")
            target["args"] = updated
        else:
            before = {k: v for k, v in target.items() if k != "action"}
            target.update(replacement)
            if {k: v for k, v in target.items() if k != "action"} == before:
                raise NegativeControlError("replace_args does not change the step parameters")
        return ResolvedControl(out, perturbed, expected_idx, expected_idx, expect_plane=plane)
    raise NegativeControlError(f"unsupported perturbation kind {perturbation['kind']!r}")


# ── Run Spec constraints ──────────────────────────────────────────────────────
def validate_negative_control_request(spec: Mapping[str, Any]) -> None:
    """Static constraints of a Run Spec that carries ``negative_control``.

    The negative Run executes exactly one explicitly named Case of a regression
    Run, so its result cannot be mixed with other Cases or planned batches.
    """
    from autophone.contracts import ContractError

    request = spec.get("negative_control")
    if request is None:
        return
    if str(spec.get("type") or "") != "regression":
        raise ContractError("negative_control is only valid on a regression Run")
    if spec.get("execution_plan"):
        raise ContractError("negative_control Run must not use an execution_plan")
    selector = spec.get("case_selector") or {}
    ids = selector.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    case_id = str((request or {}).get("case_id") or "").upper()
    if [str(item).upper() for item in ids] != [case_id]:
        raise ContractError(
            "negative_control Run must select exactly its Case by case_selector.ids"
        )
    broad = [key for key in _BROAD_SELECTOR_KEYS if selector.get(key)]
    if broad:
        raise ContractError(f"negative_control Run must not select by {broad}")


# ── judgement ─────────────────────────────────────────────────────────────────
def _case_row(run_result: Mapping[str, Any] | None, case_id: str) -> dict[str, Any] | None:
    for row in (run_result or {}).get("cases") or []:
        if isinstance(row, dict) and str(row.get("id") or "").upper() == case_id:
            return row
    return None


def _candidate_source_of(run_result: Mapping[str, Any] | None) -> tuple[Any, bool]:
    """Locate an RN candidate identity mapping in a Run Result, if declared."""
    if not isinstance(run_result, Mapping):
        return None, False
    package = run_result.get("package")
    package = package if isinstance(package, Mapping) else {}
    for container in (package, run_result):
        for key in ("candidate_identity", "rn_candidate_digests"):
            if container.get(key) is not None:
                return container.get(key), True
    bi = run_result.get("build_identity")
    if isinstance(bi, Mapping):
        for key in ("candidate_identity", "rn_candidate_digests"):
            if bi.get(key) is not None:
                return bi.get(key), True
    if any(package.get(key) is not None for key in _RN_TRIPLET_KEYS):
        return package, True
    return None, False


def build_identity_of(run_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """The Build identity one Run recorded in ``package`` (with exact content digest)."""
    package = (run_result or {}).get("package")
    package = package if isinstance(package, Mapping) else {}
    ref = package.get("registry_build_ref")
    ref = ref if isinstance(ref, Mapping) else {}
    candidate_raw, has_candidate = _candidate_source_of(run_result)
    candidate_map = candidate_raw if isinstance(candidate_raw, Mapping) else None

    build_id = (
        ref.get("build_id")
        or package.get("build_id")
        or (candidate_map.get("base_build_id") if candidate_map else None)
    )
    raw_sha = (
        ref.get("artifact_sha256")
        if ref.get("artifact_sha256") is not None
        else package.get("artifact_sha256")
    )
    norm_sha = normalize_sha256(raw_sha)
    artifact_sha256 = norm_sha if norm_sha is not None else raw_sha

    rn_digests: dict[str, str] | None = None
    artifact_content_digest: str | None = None
    if has_candidate:
        rn_digests = rn_candidate_digests_from(candidate_map)
        if rn_digests is not None and (
            raw_sha is None or (norm_sha is not None and norm_sha == rn_digests["base_ipa_sha256"])
        ):
            artifact_content_digest = rn_candidate_content_digest(
                rn_digests["base_ipa_sha256"],
                rn_digests["main_jsbundle_sha256"],
                rn_digests["assets_tree_sha256"],
            )
    else:
        artifact_content_digest = norm_sha

    sbk = package.get("subject_build_key") or (run_result or {}).get("subject_build_key")
    if not sbk:
        obs = package.get("device_build_observation")
        if isinstance(obs, Mapping):
            sbk = subject_build_key(
                obs.get("bundle_id"), obs.get("short_version"), obs.get("build_version"),
            )

    out: dict[str, Any] = {
        "profile_build_label": package.get("profile_build_label"),
        "build_identity_match": package.get("build_identity_match"),
        "build_id": build_id,
        "artifact_sha256": artifact_sha256,
        "artifact_content_digest": artifact_content_digest,
    }
    if sbk is not None:
        out["subject_build_key"] = sbk
    if rn_digests is not None:
        out["rn_candidate_digests"] = rn_digests
    return out


def _extract_row_binding_sha256(
    row: Mapping[str, Any] | None,
    *,
    run_result: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Extract and cross-check ``binding_sha256`` from a Case result row.

    Checks ``row["binding_sha256"]``, ``row["recipe_identity"]["binding_sha256"]``,
    ``row["negative_control"]["binding_sha256"]``,
    ``row["negative_control"]["base_recipe_identity"]["binding_sha256"]``,
    and (only when none of the above is set) top-level ``run_result["binding_sha256"]``.
    Returns ``(digest, error_reason)``; ``error_reason`` is non-None on malformed
    or conflicting values.
    """
    candidates: list[Any] = []
    if isinstance(row, Mapping):
        if row.get("binding_sha256") is not None:
            candidates.append(row.get("binding_sha256"))
        recipe_identity = row.get("recipe_identity")
        if isinstance(recipe_identity, Mapping) and recipe_identity.get("binding_sha256") is not None:
            candidates.append(recipe_identity.get("binding_sha256"))
        negctl = row.get("negative_control")
        if isinstance(negctl, Mapping):
            if negctl.get("binding_sha256") is not None:
                candidates.append(negctl.get("binding_sha256"))
            base_id = negctl.get("base_recipe_identity")
            if isinstance(base_id, Mapping) and base_id.get("binding_sha256") is not None:
                candidates.append(base_id.get("binding_sha256"))
    if not candidates and isinstance(run_result, Mapping) and run_result.get("binding_sha256") is not None:
        candidates.append(run_result.get("binding_sha256"))
    if not candidates:
        return None, None
    normalized: set[str] = set()
    for raw in candidates:
        digest = normalize_sha256(raw)
        if digest is None:
            return None, "invalid"
        normalized.add(digest)
    if len(normalized) > 1:
        return None, "conflict"
    return next(iter(normalized)), None


def binding_sha256_of(
    run_result: Mapping[str, Any] | None,
    case_id: str | None = None,
) -> str | None:
    """Return the validated ``binding_sha256`` for one Case in a Run Result, or ``None``."""
    row = _case_row(run_result, str(case_id).upper()) if case_id is not None else None
    digest, err = _extract_row_binding_sha256(row, run_result=run_result)
    return None if err is not None else digest


def _recipe_identity_core(identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(identity, Mapping):
        return None
    return {key: value for key, value in identity.items() if key != "binding_sha256"}


def verified_recipe_identity(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    identity = (row or {}).get("recipe_identity")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("recipe_sha256"), str)
        or "unverified_reason" in identity
    ):
        return None
    return dict(identity)


def _run_ref(run_result: Mapping[str, Any] | None, row: Mapping[str, Any] | None,
             assignment_id: str | None) -> dict[str, Any] | None:
    if run_result is None:
        return None
    return {
        "run_id": run_result.get("run_id"),
        "assignment_id": assignment_id,
        "run_status": run_result.get("status"),
        "case_status": (row or {}).get("status"),
        "blocker": (row or {}).get("blocker"),
    }


def _plane_failed(plane_val: Any) -> bool:
    if isinstance(plane_val, Mapping):
        return str(plane_val.get("verdict") or "").strip().lower() == "fail"
    if isinstance(plane_val, str):
        return plane_val.strip().lower() == "fail"
    return False


def _effective_joint_fail_kind(
    row: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    plane: str,
) -> tuple[str | None, bool, bool, bool]:
    jv = row.get("joint_verdict")
    if not isinstance(jv, Mapping):
        jv = record.get("joint_verdict") if isinstance(record.get("joint_verdict"), Mapping) else None
    row_planes = row.get("planes") if isinstance(row.get("planes"), Mapping) else {}
    rec_planes = record.get("planes") if isinstance(record.get("planes"), Mapping) else {}
    jv_planes = jv.get("planes") if isinstance(jv, Mapping) and isinstance(jv.get("planes"), Mapping) else {}

    # Accept both RunResult nested dicts ({verdict: "fail"}) and flat strings ("fail").
    # Never treat "missing" as an assertion failure — missing evidence is inconclusive.
    api_failed = (
        _plane_failed(row_planes.get("api"))
        or _plane_failed(rec_planes.get("api"))
        or _plane_failed(jv_planes.get("api"))
    )
    readback_failed = (
        _plane_failed(row_planes.get("readback"))
        or _plane_failed(rec_planes.get("readback"))
        or _plane_failed(jv_planes.get("readback"))
    )
    jv_failed = isinstance(jv, Mapping) and jv.get("status") == "fail"

    effective_fail_kind: str | None = None
    if jv_failed and isinstance(jv.get("fail_kind"), str) and jv.get("fail_kind"):
        effective_fail_kind = str(jv["fail_kind"])
    elif isinstance(record.get("fail_kind"), str) and record.get("fail_kind"):
        effective_fail_kind = str(record["fail_kind"])
    elif plane == "api" and api_failed:
        effective_fail_kind = "api_assertion_failed"
    elif plane == "readback" and readback_failed:
        effective_fail_kind = "readback_assertion_failed"
    elif plane == "joint":
        if api_failed:
            effective_fail_kind = "api_assertion_failed"
        elif readback_failed:
            effective_fail_kind = "readback_assertion_failed"
    return effective_fail_kind, jv_failed, api_failed, readback_failed


def _negative_verdict(
    row: Mapping[str, Any] | None,
    *,
    control_id: str,
    declaration_digest: str,
) -> tuple[str, dict[str, Any] | None]:
    if row is None:
        return "not_run", None
    record = row.get("negative_control")
    if (
        not isinstance(record, dict)
        or record.get("control_id") != control_id
        or record.get("declaration_sha256") != declaration_digest
    ):
        return "not_applied", record if isinstance(record, dict) else None
    status = str(row.get("status") or "")
    if status in {"error", "blocked", "skipped"}:
        return status, record
    if record.get("applied") is not True:
        return "not_applied", record
    if status == "passed":
        return VERDICT_PASSED, record
    if status == "failed":
        if row.get("blocker"):
            return "blocked", record
        expect = record.get("expect") or {}
        plane = expect_plane_of(expect)
        if plane in JOINT_EXPECT_PLANES:
            expected_step_idx = expect.get("step_index")
            failed_step_idx = record.get("failed_step_index")
            # When expect.step is omitted, any premature UI recipe step failure is
            # "failed_elsewhere" even if downstream API/readback did not fire.
            if expected_step_idx is None and failed_step_idx is not None:
                return "failed_elsewhere", record
            if expected_step_idx is not None and failed_step_idx is not None and failed_step_idx != expected_step_idx:
                return "failed_elsewhere", record
            effective_fail_kind, jv_failed, api_failed, readback_failed = _effective_joint_fail_kind(
                row, record, plane=plane,
            )
            if effective_fail_kind in NON_DISCRIMINATING_JOINT_FAIL_KINDS:
                return "failed_unexpected_kind", record
            if plane == "api":
                if not api_failed:
                    return "failed_elsewhere", record
            elif plane == "readback":
                if not readback_failed:
                    return "failed_elsewhere", record
            else:  # plane == "joint"
                if not (api_failed or readback_failed):
                    return "failed_elsewhere", record
            kinds = expect.get("fail_kinds") or list(_default_fail_kinds_for_plane(plane))
            if effective_fail_kind not in kinds:
                return "failed_unexpected_kind", record
            return VERDICT_COUNTED, record
        if record.get("failed_step_index") != expect.get("step_index"):
            return "failed_elsewhere", record
        kinds = expect.get("fail_kinds") or list(DEFAULT_FAIL_KINDS)
        if record.get("fail_kind") not in kinds:
            return "failed_unexpected_kind", record
        return VERDICT_COUNTED, record
    return "not_run", record


def judge_negative_control(
    *,
    app: str,
    control: Mapping[str, Any],
    build_label: str,
    positive: Mapping[str, Any] | None,
    negative: Mapping[str, Any] | None,
    positive_assignment_id: str | None = None,
    negative_assignment_id: str | None = None,
    target_rig_id: str | None = None,
    positive_rig_id: str | None,
    negative_rig_id: str | None,
    generated_at: str | None = None,
    planned_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Judge one positive/negative Run pair into a Promotion-Gate-readable probe.

    Counted only when all hold:
    - the positive Run's Case passed, with a verified executed Recipe identity
      and a confirmed Build identity (``build_identity_match=match`` at an
      accepted granularity, see ``build_identity_unconfirmed``, and the
      current ``build_label``);
    - the negative Run applied exactly this declaration and its Case ended
      ``failed`` on the declared assertion step (and fail kind, if declared),
      or the same Operation reports the exact opposite witness with observation frames;
    - both Runs executed the same Recipe identity on the same Build and on the
      same Rig (the Rig recorded by each Assignment; a requested target Rig
      must also match).

    A negative Run that passed is ``toothless``; every other outcome (error,
    blocked, failed on another step, not applied, identity not confirmed) is
    inconclusive and is never counted.
    """
    case_id = str(control["case_id"]).upper()
    control_id = str(control["id"])
    digest = declaration_sha256(control)
    positive_row = _case_row(positive, case_id)
    negative_row = _case_row(negative, case_id)
    target_green = bool(positive_row is not None and positive_row.get("status") == "passed")
    verdict, record = _negative_verdict(
        negative_row, control_id=control_id, declaration_digest=digest,
    )

    identity = verified_recipe_identity(positive_row)
    positive_build = build_identity_of(positive)
    negative_build = build_identity_of(negative) if negative is not None else None
    positive_binding, positive_binding_err = _extract_row_binding_sha256(
        positive_row, run_result=positive,
    )
    negative_binding, negative_binding_err = (
        _extract_row_binding_sha256(negative_row, run_result=negative)
        if negative is not None
        else (None, None)
    )
    identity_reasons: list[str] = []
    if target_green:
        if identity is None:
            identity_reasons.append("positive_recipe_identity_unverified")
        elif positive_row.get("revision") != identity.get("case_revision"):
            identity_reasons.append("positive_case_revision_mismatch")
        # match at an accepted granularity: build_marker, or info_plist only
        # for a source_revision.kind=unknown Build (BL-241).
        if build_identity_unconfirmed((positive or {}).get("package")) is not None:
            identity_reasons.append("positive_build_identity_unconfirmed")
        if positive_build.get("artifact_content_digest") is None:
            identity_reasons.append("positive_artifact_content_digest_missing")
        if positive_binding_err is not None:
            identity_reasons.append(f"positive_binding_sha256_{positive_binding_err}")
        if positive_build["profile_build_label"] != build_label:
            identity_reasons.append("positive_build_label_mismatch")
        # The compared Build must be the one the label names (BL-190), the
        # same rule as the preparation card and golden-path preflight.
        if label_build_mismatch(build_label, (positive or {}).get("package")) is not None:
            identity_reasons.append("positive_build_not_label_build")
        if not positive_rig_id:
            identity_reasons.append("positive_rig_unknown")
        elif target_rig_id and positive_rig_id != target_rig_id:
            identity_reasons.append("positive_rig_not_target")
    if record is not None and verdict in {VERDICT_COUNTED, VERDICT_PASSED}:
        if _recipe_identity_core(record.get("base_recipe_identity")) != _recipe_identity_core(identity):
            identity_reasons.append("negative_recipe_identity_mismatch")
        if (negative_build or {}).get("artifact_content_digest") != positive_build.get("artifact_content_digest"):
            identity_reasons.append("negative_artifact_content_digest_mismatch")
        if negative_build != positive_build:
            identity_reasons.append("negative_build_identity_mismatch")
        if negative_binding_err is not None:
            identity_reasons.append(f"negative_binding_sha256_{negative_binding_err}")
        elif negative_binding != positive_binding:
            identity_reasons.append("negative_binding_sha256_mismatch")
        if label_build_mismatch(build_label, (negative or {}).get("package")) is not None:
            identity_reasons.append("negative_build_not_label_build")
        if not negative_rig_id or negative_rig_id != positive_rig_id:
            identity_reasons.append("negative_rig_mismatch")
    if identity_reasons and verdict in {VERDICT_COUNTED, VERDICT_PASSED, "not_run"}:
        verdict = "identity_unconfirmed"

    witness_flip = control["perturbation"]["kind"] == "flip_witness"
    witness_stage = None
    witness_as_predicted = None
    operation_ref = None
    if witness_flip:
        planned = planned_record or {}
        operation_ref = planned.get("operation_identity")
        expect = planned.get("expect") or {}
        if not operation_ref or operation_ref.get("app") != app:
            identity_reasons.append("planned_operation_identity_missing")
        if record is not None and any(
            record.get(key) != planned.get(key) for key in ("operation_identity", "expect", "perturbation")
        ):
            identity_reasons.append("negative_operation_plan_mismatch")

        def observation(row, index):
            matches = [item for item in (row or {}).get("operation_witnesses") or []
                       if isinstance(item, dict) and item.get("step_index") == index]
            return matches[0] if len(matches) == 1 else {}

        positive_witness = observation(positive_row, expect.get("base_step_index"))
        negative_witness = observation(negative_row, expect.get("step_index"))
        if target_green and (
            positive_witness.get("operation_identity") != operation_ref
            or positive_witness.get("ok") is not True
            or not expect.get("positive_witness_stage")
            or positive_witness.get("stage") != expect.get("positive_witness_stage")
            or not positive_witness.get("shots")
        ):
            identity_reasons.append("positive_operation_witness_unconfirmed")
        if negative is not None and negative_witness.get("operation_identity") != operation_ref:
            identity_reasons.append("negative_operation_identity_mismatch")
        if negative is not None and not negative_witness.get("shots"):
            identity_reasons.append("negative_witness_observation_missing")
        witness_stage = negative_witness.get("stage")
        witness_as_predicted = bool(
            expect.get("witness_stage") and witness_stage == expect["witness_stage"]
            and negative_witness.get("ok") is False
        )
        if identity_reasons and verdict in {VERDICT_COUNTED, VERDICT_PASSED}:
            verdict = "identity_unconfirmed"
        elif verdict == VERDICT_COUNTED and not witness_as_predicted:
            verdict = "witness_imprecise"

    joint_expect = is_joint_plane_expect(control["expect"])
    expect_plane = expect_plane_of(control["expect"])
    flipped = verdict == VERDICT_COUNTED
    counted = bool(target_green and flipped and not identity_reasons)
    if flipped and target_green and counted:
        teeth_at: str | None = (
            "operation" if witness_flip else ("joint" if joint_expect else "recipe")
        )
    elif witness_flip and verdict == "witness_imprecise":
        teeth_at = "operation"
    elif verdict == VERDICT_PASSED and target_green:
        teeth_at = "none"
    else:
        teeth_at = None

    effective_fail_kind = (record or {}).get("fail_kind")
    if joint_expect and negative_row is not None and record is not None:
        joint_fk, _jvf, _apif, _rbf = _effective_joint_fail_kind(
            negative_row, record, plane=expect_plane,
        )
        if joint_fk is not None:
            effective_fail_kind = joint_fk

    expect_desc = _describe_expect(control["expect"])
    perturbed_step = describe_locator(control["perturbation"]["step"])
    if not target_green:
        status = (positive_row or {}).get("status") or (positive or {}).get("status") or "not_run"
        finding = f"不计入：正向 Run 未通过（case_status={status}），没有绿基线"
    elif counted and joint_expect:
        finding = (f"计入：反向 Run 在声明的联合判定面 {expect_desc} 失败"
                   f"（fail_kind={effective_fail_kind!r}）")
    elif counted:
        finding = (f"计入：反向 Run 在声明的步骤 {expect_desc} 失败"
                   f"（fail_kind={effective_fail_kind!r}）")
    elif verdict == VERDICT_PASSED:
        finding = f"无区分力：{control['perturbation']['kind']} {perturbed_step} 后判定仍通过"
    elif verdict == "identity_unconfirmed":
        finding = "不计入：正反两次 Run 的 Recipe、Operation 或 Build 身份未确认一致（" + ",".join(identity_reasons) + "）"
    else:
        finding = f"不计入：反向 Run 结果不可判（negative_verdict={verdict}）"

    if witness_flip:
        discriminator_label = f"{expect_desc}（Operation witness）"
    elif joint_expect:
        discriminator_label = f"{expect_desc}（joint 联合判定）"
    else:
        discriminator_label = f"{expect_desc}（recipe 层断言）"

    probe: dict[str, Any] = {
        "schema": PROBE_SCHEMA,
        "app": app,
        "case_id": case_id,
        "control_id": control_id,
        "declaration_sha256": digest,
        "build_label": build_label,
        "discriminator": discriminator_label,
        "hypothesis": control["hypothesis"],
        "perturbation_source": "declaration",
        "perturbation": copy.deepcopy((record or {}).get("perturbation"))
        or {"kind": control["perturbation"]["kind"],
            "step": copy.deepcopy(dict(control["perturbation"]["step"]))},
        "expect": copy.deepcopy((record or {}).get("expect"))
        or copy.deepcopy(dict(control["expect"])),
        "target_green": target_green,
        "discriminator_flipped": flipped,
        "teeth_at": teeth_at,
        "expect_stage": ((planned_record or {}).get("expect") or {}).get("witness_stage"),
        "witness_stage": witness_stage,
        "witness_as_predicted": witness_as_predicted,
        "negative_verdict": verdict,
        "identity_reasons": identity_reasons,
        "counted": counted,
        "finding": finding,
        "recipe_identity": identity,
        "recipe_steps_digest": (record or {}).get("base_steps_digest"),
        "build_identity": positive_build,
        "binding_sha256": positive_binding,
        "positive_run_id": (positive or {}).get("run_id"),
        "negative_run_id": (negative or {}).get("run_id"),
        "positive": _run_ref(positive, positive_row, positive_assignment_id),
        "negative": _run_ref(negative, negative_row, negative_assignment_id),
        "negative_failure": None if record is None else {
            "failed_step_index": record.get("failed_step_index"),
            "fail_kind": effective_fail_kind,
            **({"plane": expect_plane} if joint_expect else {}),
        },
        "target_rig_id": target_rig_id,
        "positive_rig_id": positive_rig_id,
        "negative_rig_id": negative_rig_id,
    }
    if witness_flip:
        probe["operation_identity"] = copy.deepcopy(operation_ref)
        probe["positive_witness"] = copy.deepcopy(positive_witness)
        probe["negative_witness"] = copy.deepcopy(negative_witness)
    if generated_at is not None:
        probe["generated_at"] = generated_at
    return probe


def proof_matches_draft_identity(
    proof: Mapping[str, Any],
    current_identity: Mapping[str, Any] | None,
) -> bool:
    """Whether a generic probe was produced by the current draft Recipe identity."""
    identity = proof.get("recipe_identity")
    return bool(
        current_identity is not None
        and isinstance(identity, dict)
        and identity.get("source") == "draft"
        and identity.get("recipe_sha256") == current_identity.get("draft_recipe_sha256")
        and identity.get("case_revision") == current_identity.get("case_revision")
        and identity.get("reserved_external_id") == current_identity.get("reserved_external_id")
    )


# ── evidence artifact ─────────────────────────────────────────────────────────
def _probe_key(probe: Mapping[str, Any]) -> tuple[Any, ...] | None:
    if probe.get("schema") != PROBE_SCHEMA:
        return None
    return (probe.get("app"), probe.get("case_id"), probe.get("control_id"))


def _attempt_summary(probe: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(probe.get(key))
        for key in ("generated_at", "negative_verdict", "target_green", "counted", "finding",
                    "identity_reasons", "positive_run_id", "negative_run_id",
                    "positive_rig_id", "negative_rig_id")
    }


def _supersedes(new: Mapping[str, Any], old: Mapping[str, Any]) -> bool:
    """Whether a rerun replaces an existing probe of the same (app, case, control).

    A counted probe is kept only when the rerun ran the same Recipe, Artifact,
    and Symbol-binding identity and is inconclusive or has no green baseline:
    such a rerun proves nothing either way.  A rerun that counts, whose
    negative Run passed (toothless), or whose known Recipe, Operation, Artifact
    content digest, or symbol ``binding_sha256`` differs from the old probe's
    replaces it, so stale evidence never outlives a newer attempt.
    """
    if old.get("counted") is not True:
        return True
    for key in ("recipe_identity", "recipe_steps_digest", "operation_identity"):
        if new.get(key) is not None and new.get(key) != old.get(key):
            return True
    new_digest = probe_artifact_content_digest(new)
    old_digest = probe_artifact_content_digest(old)
    if new_digest is not None and new_digest != old_digest:
        return True
    new_binding = probe_binding_sha256(new)
    old_binding = probe_binding_sha256(old)
    if new_binding is not None and new_binding != old_binding:
        return True
    if old_binding is not None and new_binding is None and new.get("recipe_identity") is not None:
        return True
    return bool(
        new.get("target_green") is True
        and new.get("negative_verdict") in {VERDICT_COUNTED, VERDICT_PASSED}
    )


def merge_probes(existing: Iterable[Any], probe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge one probe; every probe with another key is kept unchanged.

    For the same (app, case, control) the new probe replaces the old one, except
    that an inconclusive or not-green rerun of the same Recipe identity never
    overwrites a counted probe: the counted probe stays and records the rerun
    under ``latest_attempt``.
    """
    key = _probe_key(probe)
    rows = [dict(row) for row in existing if isinstance(row, dict)]
    same = [row for row in rows if key is not None and _probe_key(row) == key]
    kept = [row for row in rows if key is None or _probe_key(row) != key]
    previous = same[-1] if same else None
    if previous is not None and not _supersedes(probe, previous):
        previous["latest_attempt"] = _attempt_summary(probe)
        return [*kept, previous]
    return [*kept, dict(probe)]


def write_probe(negctl_root: Path, probe: Mapping[str, Any], *, generated_at: str) -> Path:
    """Merge one probe into ``<negctl_root>/<build_label>/artifact.json`` atomically."""
    from autophone.assets.store import path_write_lock
    from autophone.util import write_json_atomic

    build_label = str(probe.get("build_label") or "")
    if not build_label or "/" in build_label or build_label in {".", ".."}:
        raise NegativeControlError(f"probe build_label is not a directory name: {build_label!r}")
    path = Path(negctl_root) / build_label / ARTIFACT_NAME
    with path_write_lock(path):
        doc: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise NegativeControlError(f"{path}: existing artifact unreadable: {exc}") from exc
            if not isinstance(loaded, dict):
                raise NegativeControlError(f"{path}: existing artifact is not a mapping")
            doc = loaded
        if doc.get("build_label") not in (None, build_label):
            raise NegativeControlError(
                f"{path}: artifact build_label {doc.get('build_label')!r} != {build_label!r}"
            )
        doc.setdefault("experiment", "negative-control discrimination")
        doc["build_label"] = build_label
        doc.setdefault("adr", "docs/adr/0001-discrimination-gate-and-postcondition-witnesses.md")
        doc["generated_at"] = generated_at
        doc["probes"] = merge_probes(doc.get("probes") or [], probe)
        write_json_atomic(path, doc)
    return path


__all__ = [
    "ALL_EXPECT_PLANES",
    "ARTIFACT_NAME",
    "DEFAULT_API_FAIL_KINDS",
    "DEFAULT_FAIL_KINDS",
    "DEFAULT_JOINT_FAIL_KINDS",
    "DEFAULT_READBACK_FAIL_KINDS",
    "INCONCLUSIVE_VERDICTS",
    "JOINT_EXPECT_PLANES",
    "NEGATIVE_CONTROLS_FILE",
    "NEGATIVE_CONTROLS_SCHEMA",
    "NEGATIVE_VERDICTS",
    "NON_DISCRIMINATING_JOINT_FAIL_KINDS",
    "NegativeControlError",
    "PROBE_SCHEMA",
    "ResolvedControl",
    "VERDICT_COUNTED",
    "VERDICT_PASSED",
    "binding_sha256_of",
    "build_identity_of",
    "canonical_artifact_content_digest",
    "declaration_sha256",
    "describe_locator",
    "expect_plane_of",
    "find_negative_control",
    "is_joint_plane_expect",
    "judge_negative_control",
    "load_negative_controls",
    "locate_step",
    "merge_probes",
    "negative_controls_path",
    "normalize_sha256",
    "probe_artifact_content_digest",
    "probe_binding_sha256",
    "proof_matches_draft_identity",
    "resolve_negative_control",
    "rn_candidate_content_digest",
    "rn_candidate_digests_from",
    "validate_negative_control_request",
    "verified_recipe_identity",
    "write_probe",
]
