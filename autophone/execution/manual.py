from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from autophone.util import now_iso as _now, read_json as _read_json, write_json_atomic as _write_json


class ManualCheckpointError(RuntimeError):
    pass


class ManualCheckpointTimeout(ManualCheckpointError):
    pass


class ManualCheckpointAborted(ManualCheckpointError):
    pass


def _manual_dir(run_dir: Path) -> Path:
    return run_dir / "manual"


def checkpoint_path(run_dir: Path) -> Path:
    return _manual_dir(run_dir) / "checkpoint.json"


def resume_path(run_dir: Path) -> Path:
    return _manual_dir(run_dir) / "resume.json"


def abort_path(run_dir: Path) -> Path:
    return _manual_dir(run_dir) / "abort.json"


def notify_path(run_dir: Path) -> Path:
    return _manual_dir(run_dir) / "notify.json"


def request_manual_checkpoint(
    run_dir: Path,
    *,
    run_id: str,
    case_id: str,
    prompt: str,
    timeout_s: float,
    shot_path: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "v1",
        "run_id": run_id,
        "case_id": case_id,
        "prompt": prompt,
        "status": "waiting",
        "requested_at": _now(),
        "timeout_s": timeout_s,
    }
    if shot_path:
        payload["shot_path"] = shot_path
    resume_path(run_dir).unlink(missing_ok=True)
    abort_path(run_dir).unlink(missing_ok=True)
    _write_json(checkpoint_path(run_dir), payload)
    _notify_manual_checkpoint(run_dir, payload)
    return payload


def load_manual_state(run_dir: Path) -> dict[str, Any] | None:
    checkpoint = _read_json(checkpoint_path(run_dir))
    if checkpoint is None:
        return None
    return {
        "checkpoint": checkpoint,
        "resume": _read_json(resume_path(run_dir)),
        "abort": _read_json(abort_path(run_dir)),
        "notify": _read_json(notify_path(run_dir)),
    }


def _update_checkpoint(run_dir: Path, **updates: Any) -> dict[str, Any]:
    checkpoint = _read_json(checkpoint_path(run_dir))
    if checkpoint is None:
        raise FileNotFoundError(f"manual checkpoint not found under {run_dir}")
    checkpoint.update(updates)
    _write_json(checkpoint_path(run_dir), checkpoint)
    return checkpoint


def resume_manual_checkpoint(run_dir: Path, *, actor: str = "web", note: str = "") -> dict[str, Any]:
    checkpoint = _read_json(checkpoint_path(run_dir))
    if checkpoint is None:
        raise FileNotFoundError(f"manual checkpoint not found under {run_dir}")
    if checkpoint.get("status") not in {"waiting", "resumed"}:
        raise ManualCheckpointError(f"manual checkpoint is not waiting: {checkpoint.get('status')}")
    resume = {"actor": actor, "note": note, "resumed_at": _now()}
    _write_json(resume_path(run_dir), resume)
    checkpoint = _update_checkpoint(run_dir, status="resumed", resumed_at=resume["resumed_at"], resumed_by=actor)
    return {"checkpoint": checkpoint, "resume": resume}


def abort_manual_checkpoint(run_dir: Path, *, actor: str = "web", reason: str = "") -> dict[str, Any]:
    checkpoint = _read_json(checkpoint_path(run_dir))
    if checkpoint is None:
        raise FileNotFoundError(f"manual checkpoint not found under {run_dir}")
    abort = {"actor": actor, "reason": reason, "aborted_at": _now()}
    _write_json(abort_path(run_dir), abort)
    checkpoint = _update_checkpoint(run_dir, status="aborted", aborted_at=abort["aborted_at"], aborted_by=actor)
    return {"checkpoint": checkpoint, "abort": abort}


def wait_for_manual_resume(run_dir: Path, *, timeout_s: float, poll_ms: int = 500) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resume = _read_json(resume_path(run_dir))
        if resume is not None:
            _update_checkpoint(run_dir, status="resumed", resumed_at=resume["resumed_at"], resumed_by=resume["actor"])
            return resume
        abort = _read_json(abort_path(run_dir))
        if abort is not None:
            _update_checkpoint(run_dir, status="aborted", aborted_at=abort["aborted_at"], aborted_by=abort["actor"])
            raise ManualCheckpointAborted(abort.get("reason") or "manual checkpoint aborted")
        time.sleep(max(poll_ms, 50) / 1000)

    _update_checkpoint(run_dir, status="timeout", timed_out_at=_now())
    raise ManualCheckpointTimeout(f"manual checkpoint timed out after {timeout_s:.0f}s")


def _manual_payload(checkpoint: dict[str, Any], *, base_url: str | None) -> dict[str, Any]:
    run_id = str(checkpoint["run_id"])
    link = f"/?run={run_id}" if not base_url else base_url.rstrip("/") + f"/?run={run_id}"
    lines = [
        "autophone manual step waiting",
        f"run: {run_id}",
        f"case: {checkpoint.get('case_id') or '-'}",
        f"prompt: {checkpoint.get('prompt') or ''}",
        f"detail: {link}",
    ]
    return {"text": "\n".join(lines)}


def _notify_manual_checkpoint(run_dir: Path, checkpoint: dict[str, Any]) -> None:
    base_url = os.environ.get("AUTOPHONE_PUBLIC_BASE_URL")
    payload = _manual_payload(checkpoint, base_url=base_url)
    _write_json(notify_path(run_dir), {
        "status": "skipped",
        "reason": "external notification transport not configured",
        "payload": payload,
    })
