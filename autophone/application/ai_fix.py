from __future__ import annotations

import json
from pathlib import Path
from secrets import token_hex
from typing import Any

from autophone.assets import cases as cases_store
from autophone.assets.business_chains import family_reference_issue, load_business_chains
from autophone.assets.policy import assert_assets_writable
from autophone.application.ai_convert import write_recipe_draft
from autophone.application.ai_draft import land_operation_draft
from autophone.assets.apps import load_app_profile
from autophone.contracts import validate_or_raise, write_json
from autophone.util import now_iso, read_json

PROPOSAL_NAME = "ai-fix-proposal.json"


class AiFixError(ValueError):
    pass


def _text(diagnosis: dict[str, Any]) -> str:
    parts = [
        diagnosis.get("likely_root_cause") or "",
        diagnosis.get("failed_action") or "",
        " ".join(str(v) for v in diagnosis.get("next_steps") or []),
    ]
    return " ".join(parts).lower()


def route_fix(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Pure classifier from AI diagnosis to repair lane."""
    text = _text(diagnosis)
    no_fix_tokens = (
        "environment", "env", "rig", "device", "offline", "frida", "vphone",
        "网络", "环境", "设备", "离线", "真机", "登录态", "后端不可用",
    )
    if any(token in text for token in no_fix_tokens):
        return {"kind": "no_fix", "reason": "diagnosis points at environment/rig/device/backend, not a regression asset patch"}
    if any(token in text for token in ("缺操作", "operation", "precondition", "setup", "前置", "复用操作")):
        return {"kind": "operation_draft", "reason": "diagnosis asks for a reusable operation or missing precondition"}
    if any(token in text for token in ("case", "用例", "预期", "文本", "描述", "actual behavior")):
        return {"kind": "case_edit", "reason": "diagnosis suggests the case text or expectation is stale"}
    return {"kind": "recipe_patch", "reason": "diagnosis points at execution recipe timing/OCR/region drift"}


def resolve_run_dir(run_root: Path, run_id: str, *, run_dir: str | None = None) -> Path:
    if run_dir:
        target = (run_root / run_dir).resolve() if not Path(run_dir).is_absolute() else Path(run_dir).resolve()
        try:
            target.relative_to(run_root.resolve())
        except ValueError as exc:
            raise AiFixError("run_dir must be under run root") from exc
        if target.name != run_id:
            raise AiFixError("run_dir does not match run_id")
        if not target.exists():
            raise FileNotFoundError(f"run not found: {run_dir}")
        return target
    matches = sorted(run_root.glob(f"*/{run_id}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError(f"run not found: {run_id}")
    if len(matches) > 1:
        raise AiFixError(json.dumps({
            "error": "ambiguous_run_id",
            "candidates": [m.relative_to(run_root).as_posix() for m in matches],
        }, ensure_ascii=False))
    return matches[0]


def _proposal_path(run_dir: Path) -> Path:
    return run_dir / PROPOSAL_NAME


def _load_diagnosis(run_dir: Path) -> dict[str, Any]:
    diagnosis = read_json(run_dir / "ai-diagnosis.json")
    if not isinstance(diagnosis, dict):
        raise AiFixError(f"ai-diagnosis.json not found: {run_dir}")
    validate_or_raise("ai-diagnosis.v1.json", diagnosis)
    return diagnosis


def _run_app(run_dir: Path) -> str | None:
    result = read_json(run_dir / "platform-run-result.json") or {}
    return result.get("app")


def propose_fix(
    run_dir: Path,
    *,
    patch: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnosis = _load_diagnosis(run_dir)
    route = route_fix(diagnosis)
    kind = str(route["kind"])
    now = now_iso()
    case_id = diagnosis.get("failed_case")
    proposal = {
        "schema_version": "v1",
        "proposal_id": f"fix-{token_hex(5)}",
        "run_id": str(diagnosis["run_id"]),
        "run_dir": str(run_dir),
        "app": _run_app(run_dir),
        "kind": kind,
        "status": "no_fix" if kind == "no_fix" else "proposed",
        "target": target or {"case_id": case_id, "asset": f"case:{case_id}" if case_id else None},
        "patch": patch or _default_patch(kind, diagnosis),
        "route": route,
        "provenance": {
            "source": "ai-diagnosis",
            "provider": (diagnosis.get("provenance") or {}).get("provider"),
            "model": (diagnosis.get("provenance") or {}).get("model"),
        },
        "created_at": now,
        "updated_at": now,
    }
    validate_or_raise("ai-fix-proposal.v1.json", proposal)
    write_json(_proposal_path(run_dir), proposal)
    return {"ok": kind != "no_fix", "proposal": proposal, "path": str(_proposal_path(run_dir))}


def _default_patch(kind: str, diagnosis: dict[str, Any]) -> dict[str, Any]:
    if kind == "case_edit":
        return {
            "changes": {
                "status": "draft",
                "remark": f"AI fix proposal: {diagnosis.get('likely_root_cause')}",
            }
        }
    if kind == "operation_draft":
        return {
            "requirement": diagnosis.get("likely_root_cause"),
            "source_text": "\n".join(str(v) for v in diagnosis.get("next_steps") or []),
        }
    if kind == "recipe_patch":
        return {
            "instructions": diagnosis.get("likely_root_cause"),
            "steps": [],
        }
    return {"reason": diagnosis.get("likely_root_cause")}


def load_proposal(run_dir: Path) -> dict[str, Any]:
    proposal = read_json(_proposal_path(run_dir))
    if not isinstance(proposal, dict):
        raise AiFixError(f"{PROPOSAL_NAME} not found: {run_dir}")
    validate_or_raise("ai-fix-proposal.v1.json", proposal)
    return proposal


def save_proposal(run_dir: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    proposal["updated_at"] = now_iso()
    validate_or_raise("ai-fix-proposal.v1.json", proposal)
    write_json(_proposal_path(run_dir), proposal)
    return proposal


def apply_proposal(run_dir: Path, *, proposal: dict[str, Any] | None = None) -> dict[str, Any]:
    assert_assets_writable("apply AI asset fix")
    proposal = proposal or load_proposal(run_dir)
    if proposal.get("status") == "applied" and proposal.get("applied"):
        return {"ok": True, "proposal": proposal, "applied": proposal["applied"], "idempotent": True}
    if proposal.get("status") == "rejected":
        raise AiFixError("proposal is rejected")
    kind = proposal.get("kind")
    if kind == "no_fix":
        raise AiFixError("proposal kind no_fix cannot be applied")
    app = proposal.get("app")
    if not app:
        raise AiFixError("proposal has no app")
    if kind == "recipe_patch":
        applied = _apply_recipe_patch(str(app), proposal)
    elif kind == "case_edit":
        applied = _apply_case_edit(str(app), proposal)
    elif kind == "operation_draft":
        applied = _apply_operation_draft(str(app), proposal)
    else:
        raise AiFixError(f"unsupported proposal kind: {kind}")
    proposal["status"] = "applied"
    proposal["applied"] = applied
    save_proposal(run_dir, proposal)
    return {"ok": True, "proposal": proposal, "applied": applied}


def reject_proposal(run_dir: Path, *, actor: str = "web", reason: str = "") -> dict[str, Any]:
    proposal = load_proposal(run_dir)
    proposal["status"] = "rejected"
    proposal["rejection"] = {"actor": actor, "reason": reason, "at": now_iso()}
    save_proposal(run_dir, proposal)
    return {"ok": True, "proposal": proposal}


def _apply_recipe_patch(app: str, proposal: dict[str, Any]) -> dict[str, Any]:
    case_id = (proposal.get("target") or {}).get("case_id")
    steps = (proposal.get("patch") or {}).get("steps")
    if not case_id:
        raise AiFixError("recipe_patch target.case_id is required")
    if not isinstance(steps, list) or not steps:
        raise AiFixError("recipe_patch requires non-empty patch.steps")
    out = write_recipe_draft(
        app,
        str(case_id),
        steps,
        provenance={
            "source": "ai-fix",
            "from": f"run:{proposal.get('run_id')}",
            "proposal_id": proposal.get("proposal_id"),
            "note": (proposal.get("patch") or {}).get("instructions") or "",
        },
        overwrite=False,
    )
    if not out.get("ok") and out.get("path"):
        return {"kind": "recipe_patch", "status": "already_exists", "path": out["path"], "draft_id": str(case_id).upper()}
    return {"kind": "recipe_patch", "status": "draft", "path": out["path"], "draft_id": str(case_id).upper()}


def _apply_case_edit(app: str, proposal: dict[str, Any]) -> dict[str, Any]:
    case_id = (proposal.get("target") or {}).get("case_id")
    if not case_id:
        raise AiFixError("case_edit target.case_id is required")
    profile = load_app_profile(app)
    changes = dict((proposal.get("patch") or {}).get("changes") or {})
    changes["status"] = "draft"
    cases_path = profile.cases_path()
    with cases_store.registry_write_lock(cases_path):
        # 与 CaseLifecycleApplication.edit 同一判定（V3/V4/V8）：本入口总是改 status，
        # 按写后状态校验 Family 引用，声明不合法时 fail-closed（BL-038）。
        current = _registry_case(cases_path, str(case_id))
        issue = family_reference_issue(load_business_chains(profile), {**current, **changes})
        if issue is not None:
            raise AiFixError(f"case_family: {issue['code']} — {issue['message']}")
        updated = cases_store.update_case(cases_path, str(case_id), changes)
    return {"kind": "case_edit", "status": "draft", "case_id": updated["id"], "path": str(cases_path)}


def _registry_case(cases_path: Path, case_id: str) -> dict[str, Any]:
    import yaml

    document = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    rows = document.get("cases") if isinstance(document, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("id")) == case_id:
            return row
    # 与此前直接调用 update_case 时相同：条目不存在抛 KeyError。
    raise KeyError(case_id)


def _apply_operation_draft(app: str, proposal: dict[str, Any]) -> dict[str, Any]:
    patch = proposal.get("patch") or {}
    fields = patch.get("fields")
    if not isinstance(fields, dict):
        raise AiFixError("operation_draft apply requires patch.fields; use /api/ai/draft-operation for free-form requirements")
    provenance = {
        "source": "ai-fix",
        "from": f"run:{proposal.get('run_id')}",
        "proposal_id": proposal.get("proposal_id"),
    }
    return land_operation_draft(app, fields, provenance=provenance)
