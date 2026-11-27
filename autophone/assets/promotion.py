from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autophone.assets.negative_control import (
    canonical_artifact_content_digest,
    normalize_sha256,
    probe_artifact_content_digest,
    probe_binding_sha256,
)

#: 通用 negctl 证据（``autophone.assets.negative_control.PROBE_SCHEMA``）。R47/R49 旧工具的
#: probe 不带 ``schema``，按原规则判定；按 D6 规则，所有探针均须携带精确制品内容摘要。
GENERIC_PROBE_SCHEMA = "autophone.negctl-probe/2"

_ARTIFACT_REF_KEYS = (
    "artifact_content_digest",
    "artifact_sha256",
    "rn_candidate_digests",
    "candidate_identity",
    "base_ipa_sha256",
    "main_jsbundle_sha256",
    "assets_tree_sha256",
)


def _mapping_declares_artifact_digest(mapping: Mapping[str, Any] | None) -> bool:
    if not isinstance(mapping, Mapping):
        return False
    if mapping.get("kind") == "rn_candidate":
        return True
    return any(mapping.get(key) is not None for key in _ARTIFACT_REF_KEYS)


def _resolve_expected_artifact_digest(
    *,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_build_identity: Mapping[str, Any] | None = None,
    current_build_identity_ref: Mapping[str, Any] | None = None,
) -> tuple[str | None, bool]:
    """Resolve the caller-supplied expected exact artifact content digest.

    Returns ``(digest, specified)``.  When ``specified`` is ``True`` and
    ``digest`` is ``None`` (malformed or contradictory caller inputs), callers
    must fail closed as ``discrimination_stale``.
    """
    candidates: list[str | None] = []
    if current_artifact_content_digest is not None:
        candidates.append(normalize_sha256(current_artifact_content_digest))
    if current_artifact_sha256 is not None:
        candidates.append(normalize_sha256(current_artifact_sha256))
    if current_build_identity is not None:
        candidates.append(canonical_artifact_content_digest(current_build_identity))
    if current_build_identity_ref is not None and _mapping_declares_artifact_digest(
        current_build_identity_ref
    ):
        candidates.append(canonical_artifact_content_digest(current_build_identity_ref))
    if not candidates:
        return None, False
    if any(digest is None for digest in candidates):
        return None, True
    unique = {digest for digest in candidates if digest is not None}
    if len(unique) != 1:
        return None, True
    return next(iter(unique)), True


def discrimination_gaps(
    proof: dict[str, Any] | None,
    *,
    current_build_label: str | None = None,
    current_build_identity_ref: dict[str, Any] | None = None,
    current_build_identity: dict[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool = False,
    current_recipe_identity: dict[str, Any] | None = None,
    current_operation_identity: dict[str, Any] | None = None,
) -> list[str]:
    """一份区分力证据（一条 negctl probe 结果）距「有牙且新鲜」差哪些 gap（ADR-0001 / D6）。空=过关。

    None/缺失 → `discrimination_unproven`。否则要求：① 有绿基线（target_green is True）；
    ② 负控下判定面翻红（discriminator_flipped is True）且声明牙在哪层（teeth_at=recipe|operation|joint）；
    ③ 若是 operation-witness 探针，必须翻成**预测对偶 witness**（witness_as_predicted is True）
    ——翻成兜底 fail（如 indeterminate）或缺字段都算弱证据、不算牙；
    ④ 全局精确制品新鲜度（D6）：无论是否使用符号绑定，探针必须携带精确 `artifact_content_digest`
    （`ipa_build` 为 64 位小写 `artifact_sha256`，`rn_candidate` 为三元组规范摘要），缺失精确摘要的
    历史旧证据或与当前制品摘要不一致（即便 `subject_build_key` 版本三元组或 `build_label` 相同）
    一律记 `discrimination_stale`；
    ⑤ 符号绑定新鲜度（D6）：当资产使用符号绑定（`uses_symbol_binding=True` 或传入
    `current_binding_sha256`）时，探针必须携带非空且与当前一致的 `binding_sha256`，否则记
    `discrimination_stale`；
    ⑥ 通用 negctl 证据（``schema=autophone.negctl-probe/2``）：反向 Run 不可判时记
    `discrimination_inconclusive`；传入 `current_recipe_identity` / `current_operation_identity`
    时身份不一致记 `discrimination_identity_mismatch`。
    """
    if not proof:
        return ["discrimination_unproven"]
    gaps: list[str] = []
    teeth_at = proof.get("teeth_at")
    generic = proof.get("schema") == GENERIC_PROBE_SCHEMA
    if proof.get("target_green") is not True:
        gaps.append("discrimination_no_green_baseline")
    inconclusive = generic and proof.get("negative_verdict") not in {
        "failed_on_expected_step", "passed",
    }
    if inconclusive:
        gaps.append("discrimination_inconclusive")
    elif proof.get("discriminator_flipped") is not True or teeth_at not in {"recipe", "operation", "joint"}:
        gaps.append("discrimination_toothless")
    if generic and proof.get("counted") is not True and not gaps:
        gaps.append("discrimination_inconclusive")
    if generic and current_recipe_identity is not None:
        from autophone.assets.negative_control import proof_matches_draft_identity

        if not proof_matches_draft_identity(proof, current_recipe_identity):
            gaps.append("discrimination_identity_mismatch")
    if generic and current_operation_identity is not None:
        if not current_operation_identity or proof.get("operation_identity") != current_operation_identity:
            gaps.append("discrimination_identity_mismatch")
    if teeth_at == "operation" and proof.get("witness_as_predicted") is not True:
        gaps.append("discrimination_witness_imprecise")

    stale = False
    proof_digest = probe_artifact_content_digest(proof)
    if proof_digest is None:
        stale = True

    expected_digest, expected_digest_specified = _resolve_expected_artifact_digest(
        current_artifact_content_digest=current_artifact_content_digest,
        current_artifact_sha256=current_artifact_sha256,
        current_build_identity=current_build_identity,
        current_build_identity_ref=current_build_identity_ref,
    )
    if expected_digest_specified and (expected_digest is None or proof_digest != expected_digest):
        stale = True

    if current_build_identity_ref is not None:
        if "subject_build_key" in current_build_identity_ref:
            expected_key = current_build_identity_ref.get("subject_build_key")
            proof_key = (
                (proof.get("build_identity_ref") or {}).get("subject_build_key")
                or (proof.get("build_identity") or {}).get("subject_build_key")
            )
            if not expected_key or proof_key != expected_key:
                stale = True
        elif not expected_digest_specified:
            stale = True
    elif current_build_label is not None and proof.get("build_label") != current_build_label:
        stale = True

    require_binding = bool(uses_symbol_binding) or (current_binding_sha256 is not None)
    if require_binding:
        expected_binding = normalize_sha256(current_binding_sha256)
        proof_binding = probe_binding_sha256(proof)
        if expected_binding is None or proof_binding is None or proof_binding != expected_binding:
            stale = True

    if stale:
        gaps.append("discrimination_stale")
    return gaps


def _evaluate_discrimination(
    proof: dict[str, Any] | None,
    current_build_label: str | None = None,
    current_recipe_identity: dict[str, Any] | None = None,
    current_operation_identity: dict[str, Any] | None = None,
    *,
    current_build_identity_ref: dict[str, Any] | None = None,
    current_build_identity: dict[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool = False,
) -> tuple[bool, list[str]]:
    gaps = discrimination_gaps(
        proof,
        current_build_label=current_build_label,
        current_build_identity_ref=current_build_identity_ref,
        current_build_identity=current_build_identity,
        current_artifact_content_digest=current_artifact_content_digest,
        current_artifact_sha256=current_artifact_sha256,
        current_binding_sha256=current_binding_sha256,
        uses_symbol_binding=uses_symbol_binding,
        current_recipe_identity=current_recipe_identity,
        current_operation_identity=current_operation_identity,
    )
    return (not gaps), gaps


def case_requires_discrimination(case: dict[str, Any]) -> bool:
    """该用例转正是否须过区分力闸：只对**可执行、产机器判定**的用例（automation=ui_ready）要求；
    manual_checkpoint/blocked_by_infra/needs_fixture 等没有机器 discriminator → 不卡（不可能 toothless）。"""
    return str(case.get("automation") or "") == "ui_ready"


def operation_requires_discrimination(op: dict[str, Any]) -> bool:
    """该操作转正是否须过区分力闸：只对**声明了 postcondition witness** 的操作要求——witness 是
    操作层唯一可机器校验的 discriminator（ADR-0001）。无 witness 的操作（造单/上下岗等纯 fixture）
    没有判定面、不可能 toothless → 不卡。对称 [[case_requires_discrimination]]（用例侧用 automation=ui_ready）。"""
    return bool(op.get("witness"))


def discrimination_status(
    *,
    required: bool,
    proof: dict[str, Any] | None,
    current_build_label: str | None = None,
    current_build_identity_ref: dict[str, Any] | None = None,
    current_build_identity: dict[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool = False,
) -> dict[str, Any]:
    """派生区分力态（供 **active 用例 soft-hold 展示**，不改 status；ADR-0003）。

    返回 `{state, ...}`，state ∈ not_required / proven / stale / toothless / witness_imprecise /
    no_green_baseline / inconclusive / unproven。active 用例 state≠proven 时**照常跑回归**（保留回归价值），只在
    面板标「该 build 区分力未复验」，而非把它退役或（致命地）信到变红为止。
    """
    if not required:
        return {"state": "not_required"}
    gaps = discrimination_gaps(
        proof,
        current_build_label=current_build_label,
        current_build_identity_ref=current_build_identity_ref,
        current_build_identity=current_build_identity,
        current_artifact_content_digest=current_artifact_content_digest,
        current_artifact_sha256=current_artifact_sha256,
        current_binding_sha256=current_binding_sha256,
        uses_symbol_binding=uses_symbol_binding,
    )
    if not gaps:
        return {"state": "proven", "build_label": (proof or {}).get("build_label"),
                "teeth_at": (proof or {}).get("teeth_at")}
    # 单一主因（展示用，优先级：stale > toothless > imprecise > no_green > inconclusive > unproven）。
    for primary in ("discrimination_stale", "discrimination_toothless",
                    "discrimination_witness_imprecise", "discrimination_no_green_baseline",
                    "discrimination_inconclusive", "discrimination_unproven"):
        if primary in gaps:
            state = primary.removeprefix("discrimination_")
            break
    else:  # pragma: no cover
        state = "unproven"
    return {"state": state, "gaps": gaps, "build_label": (proof or {}).get("build_label")}


def evaluate_promotion_gate(
    runs: list[dict[str, Any]],
    *,
    draft_id: str,
    required_passes: int = 3,
    discrimination: dict[str, Any] | None = None,
    require_discrimination: bool = False,
    current_build_label: str | None = None,
    current_build_identity_ref: dict[str, Any] | None = None,
    current_build_identity: dict[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool = False,
    current_recipe_identity: dict[str, Any] | None = None,
    current_operation_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate whether a verification draft can be promoted to regression.

    Promotion requires human approval and N consecutive passed verification runs.
    This deliberately returns a decision object; it never edits the regression
    case library by itself.

    区分力闸（ADR-0001 / D6，opt-in）：`require_discrimination=True` 时，draft 还必须附一份
    证明判定面有牙且精确制品/符号绑定新鲜的 negative-control 证据（`discrimination`）才 `ready`；
    缺证据/无绿基线/toothless/witness 不精确/制品或符号绑定过期各记一条 gap。
    """
    relevant = [run for run in runs if run.get("draft_id") == draft_id]
    relevant.sort(key=lambda run: str(run.get("ended_at") or run.get("run_id") or ""))

    approved = any(run.get("review_status") == "approved" for run in relevant)
    consecutive = 0
    streak: list[str] = []
    for run in reversed(relevant):
        if run.get("status") == "passed":
            consecutive += 1
            streak.append(str(run.get("run_id") or ""))
            continue
        break

    if require_discrimination:
        disc_ok, disc_gaps = _evaluate_discrimination(
            discrimination,
            current_build_label,
            current_recipe_identity,
            current_operation_identity,
            current_build_identity_ref=current_build_identity_ref,
            current_build_identity=current_build_identity,
            current_artifact_content_digest=current_artifact_content_digest,
            current_artifact_sha256=current_artifact_sha256,
            current_binding_sha256=current_binding_sha256,
            uses_symbol_binding=uses_symbol_binding,
        )
    else:
        disc_ok, disc_gaps = True, []

    ready = approved and consecutive >= required_passes and disc_ok
    gaps: list[str] = []
    if not approved:
        gaps.append("human_review_not_approved")
    if consecutive < required_passes:
        gaps.append(f"stable_passes_{consecutive}_lt_{required_passes}")
    gaps.extend(disc_gaps)
    return {
        "draft_id": draft_id,
        "ready": ready,
        "required_passes": required_passes,
        "consecutive_passes": consecutive,
        "streak": list(reversed(streak)),
        "gaps": gaps,
        "discrimination_required": require_discrimination,
        "discrimination": discrimination if require_discrimination else None,
    }


def load_discrimination_proof(
    negctl_root: Path | str,
    *,
    case_id: str | None = None,
    draft_id: str | None = None,
    operation: str | None = None,
    build_label: str | None = None,
    preferred_build_label: str | None = None,
    app: str | None = None,
    current_recipe_identity: dict[str, Any] | None = None,
    current_operation_identity: dict[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_artifact_sha256: str | None = None,
    current_build_identity: dict[str, Any] | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool = False,
) -> dict[str, Any] | None:
    """从 negctl 实验产物（`runs/negctl/<build_label>/artifact.json`）里取一条匹配某 draft 的
    区分力证据，喂给 `evaluate_promotion_gate(discrimination=...)`。

    匹配：按 `case_id`、`draft_id`，或按 `operation`（probe 的 perturbation.target == operation，且 teeth_at=operation）。
    同一 draft 可能有多条 probe（如 R49 的 ablate=toothless + flip=有牙）——优先选**有牙**那条
    （target green、翻红、teeth_at 有效，且 operation witness 精确）；都没有则取最后一条（让闸门照实报 gap）。`build_label`
    锁定某次 build 目录；`preferred_build_label` 优先读取当前 Build，缺少匹配时仍按目录名回退。
    两者均未指定时取名字最大的那次有匹配的。无产物/无匹配 → None。
    传 `app` 时跳过标了其他 App 的 probe；不带 `app` 字段的旧 probe 不受影响。
    通用 negctl probe 的身份字段（Recipe、Build、精确制品摘要、符号 `binding_sha256`、正反 Run id、反向结论）原样带出。
    传 `current_recipe_identity` / `current_operation_identity` / `current_artifact_content_digest` /
    `current_binding_sha256` 时，有牙的 probe 中优先取身份与制品/符号摘要一致的探针，使闸门报出的缺口对应实际原因。
    """
    root = Path(negctl_root)
    if not root.exists():
        return None
    build_dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda d: (d.name == preferred_build_label, d.name),
    )
    if build_label is not None:
        build_dirs = [d for d in build_dirs if d.name == build_label]

    expected_digest, expected_digest_specified = _resolve_expected_artifact_digest(
        current_artifact_content_digest=current_artifact_content_digest,
        current_artifact_sha256=current_artifact_sha256,
        current_build_identity=current_build_identity,
    )
    require_binding = bool(uses_symbol_binding) or (current_binding_sha256 is not None)
    expected_binding = normalize_sha256(current_binding_sha256) if require_binding else None

    for d in reversed(build_dirs):  # 最新 build 优先
        art = d / "artifact.json"
        if not art.exists():
            continue
        try:
            doc = json.loads(art.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        matches = [
            pr for pr in (doc.get("probes") or [])
            if isinstance(pr, dict)
            and (app is None or pr.get("app") in (None, app))
            and ((case_id is not None and pr.get("case_id") == case_id)
            or (draft_id is not None and pr.get("draft_id") == draft_id)
            or (operation is not None
                and (pr.get("perturbation") or {}).get("target") == operation
                and (pr.get("teeth_at") == "operation" or (
                    pr.get("schema") == GENERIC_PROBE_SCHEMA
                    and (pr.get("perturbation") or {}).get("kind") == "flip_witness"
                    and (pr.get("operation_identity") or {}).get("name") == operation))))
        ]
        if not matches:
            continue
        teeth = [
            m for m in matches
            if m.get("target_green") is True
            and m.get("discriminator_flipped") is True
            and m.get("teeth_at") in {"recipe", "operation", "joint"}
            and (m.get("teeth_at") != "operation" or m.get("witness_as_predicted") is True)
        ]
        if teeth:
            from autophone.assets.negative_control import proof_matches_draft_identity

            def rank(probe: dict[str, Any]) -> tuple[int, int, int]:
                if current_recipe_identity is not None or current_operation_identity is not None:
                    if probe.get("schema") != GENERIC_PROBE_SCHEMA:
                        id_rank = 1
                    else:
                        id_matches = (
                            (current_recipe_identity is None
                             or proof_matches_draft_identity(probe, current_recipe_identity))
                            and (current_operation_identity is None
                                 or probe.get("operation_identity") == current_operation_identity)
                        )
                        id_rank = 2 if id_matches else 0
                else:
                    id_rank = 0
                digest = probe_artifact_content_digest(probe)
                if expected_digest_specified:
                    art_rank = (
                        2 if (expected_digest is not None and digest == expected_digest)
                        else (1 if digest is not None else 0)
                    )
                else:
                    art_rank = 1 if digest is not None else 0
                if require_binding:
                    binding = probe_binding_sha256(probe)
                    bind_rank = 1 if (expected_binding is not None and binding == expected_binding) else 0
                else:
                    bind_rank = 0
                return (id_rank, art_rank, bind_rank)

            best = max(rank(m) for m in teeth)
            teeth = [m for m in teeth if rank(m) == best]
        chosen = teeth[-1] if teeth else matches[-1]
        is_generic = chosen.get("schema") == GENERIC_PROBE_SCHEMA
        out: dict[str, Any] = {
            "build_label": doc.get("build_label") or d.name,
            "case_id": chosen.get("case_id"),
            "draft_id": chosen.get("draft_id"),
            "teeth_at": chosen.get("teeth_at"),
            "target_green": chosen.get("target_green"),
            "discriminator_flipped": chosen.get("discriminator_flipped"),
            "witness_as_predicted": chosen.get("witness_as_predicted"),
            "perturbation": chosen.get("perturbation"),
            "finding": chosen.get("finding"),
        }
        if is_generic:
            for key in _GENERIC_PROOF_FIELDS:
                out[key] = chosen.get(key)
        else:
            for key in _LEGACY_IDENTITY_PROOF_FIELDS:
                if key in chosen:
                    out[key] = chosen.get(key)
        return out
    return None


_LEGACY_IDENTITY_PROOF_FIELDS = (
    "build_identity",
    "build_identity_ref",
    "artifact_content_digest",
    "artifact_sha256",
    "rn_candidate_digests",
    "binding_sha256",
)

_GENERIC_PROOF_FIELDS = (
    "schema", "app", "control_id", "declaration_sha256", "negative_verdict", "counted",
    "identity_reasons", "recipe_identity", "recipe_steps_digest", "build_identity",
    "build_identity_ref", "artifact_content_digest", "binding_sha256",
    "operation_identity", "expect_stage", "witness_stage", "positive_witness", "negative_witness",
    "positive_run_id", "negative_run_id", "positive_rig_id", "negative_rig_id", "latest_attempt",
)
