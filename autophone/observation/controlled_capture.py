"""Fail-closed controlled capture using one owned spawn and private evidence."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from autophone.observation.impact.controlled_input import load_for_run
from autophone.observation.impact.controlled_evidence import verify_delivery

_FAILURE_CODE = re.compile(r"controlled_[a-z_]+\Z")


def configure(exports, loaded, pid):
    from autophone.scenario.session import apply_scenario

    payload = SimpleNamespace(
        payload=lambda mode: {
            **loaded.compiled.payload(mode),
            "controlled": True,
        }
    )
    health = apply_scenario(exports, payload, "spawn")
    required = dict(
        controlled=True,
        hash=loaded.compiled.snapshot_hash,
        pid=pid,
        mode="spawn",
        configured_rules=1,
        interception_ready=True,
        hook_installed=True,
        hook_mode="replace",
        nsurlsession_class=True,
        block_runtime=True,
        coverage_gap=False,
    )
    if (
        type(pid) is not int
        or pid <= 0
        or any(
            type(health.get(key)) is not type(value) or health.get(key) != value
            for key, value in required.items()
        )
    ):
        raise RuntimeError("controlled_health_invalid")


def execute(run_dir, spec, profile, shot_dir, operation, factory, executor, ready):
    """Keep the first execution outcome and never retry after any capture failure."""
    result = dict(operation)
    session = None
    errors = []
    delivery = None
    loaded = None
    try:
        loaded = load_for_run(run_dir, spec)
        if loaded is None:
            raise RuntimeError("controlled_input_required")
        session = factory(profile, spec, shot_dir)
        if session is None or session.spawn_gated is not True:
            raise RuntimeError("controlled_spawn_required")
        with session:
            if not ready(shot_dir, profile).get("ok"):
                raise RuntimeError("controlled_surface_unready")
            try:
                result = executor(capture_session=session)
            except Exception:
                errors.append("controlled_executor_failed")
                raise
    except Exception as exc:
        errors.append("controlled_execution_failed")
        # Keep only fixed failure codes; arbitrary exception text may carry source values.
        code = getattr(exc, "code", None) or str(exc)
        if isinstance(code, str) and _FAILURE_CODE.fullmatch(code):
            errors.append(code)
    errors.extend(getattr(session, "failure_codes", []))
    errors = list(dict.fromkeys(errors))
    # Preserve available events even when startup, execution, or closing failed.
    if session is not None:
        try:
            from autophone.observation.capture import write_capture_jsonl
            from autophone.observation.artifacts import Artifact, append_artifacts

            write_capture_jsonl(run_dir, list(session.merged()))
            path = run_dir / "planes/api/private/events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(
                "".join(
                    json.dumps(e, ensure_ascii=False) + "\n" for e in session.events
                )
            )
            path.chmod(0o600)
            append_artifacts(
                run_dir,
                [
                    Artifact(
                        rel,
                        "other",
                        sensitivity="sensitive",
                        redaction_status="raw",
                        retention="local_only",
                        share_policy="forbidden",
                        ai_policy="forbidden",
                    )
                    for rel in (
                        "planes/api/capture.jsonl",
                        "planes/api/private/events.jsonl",
                    )
                ],
            )
            if not errors:
                delivery = verify_delivery(
                    run_dir, spec, session.events, cleanup=session.cleanup
                )
        except Exception:
            errors.append("controlled_evidence_failed")
    if errors:
        result = {
            **result,
            "status": "error",
            "detail": ";".join(filter(None, (result.get("detail"), *errors))),
            "failure_stage": result.get("failure_stage", "operation_execution"),
        }
    api = {
        "mode": "controlled",
        "verdict": "fail" if errors else "pass",
        "detail": ";".join(errors) if errors else "controlled_delivery_verified",
    }
    api["scenario"] = spec["planes"]["api"]["scenario"]
    if loaded is not None:
        api["rule_snapshot_hash"] = loaded.compiled.snapshot_hash
    if delivery is not None:
        api["delivery"] = delivery
    else:
        api["delivery"] = dict(
            status="failed", reason=errors[0], hit_count=0, completed_count=0
        )
    return result, api


def build_session(profile, spec, shot_dir):
    from autophone.observation import capture
    from autophone.observation.frida import net_capture
    from autophone.observation.frida.capture_session import CaptureSession
    from pathlib import Path

    if shot_dir is None or capture._capture_mode(profile) != "spawn":
        raise RuntimeError("controlled_spawn_required")
    loaded = load_for_run(shot_dir.parent, spec)
    if loaded is None:
        raise RuntimeError("controlled_input_required")
    try:
        import frida  # noqa: F401 - reject missing SDK before execution
    except ImportError:
        raise RuntimeError("controlled_sdk_unavailable") from None
    if not profile.bundle_id or not Path(net_capture.AGENT_JS).is_file():
        raise RuntimeError("controlled_bundle_unavailable")
    profile_loaded = capture._capture_on_loaded(profile)

    def on_loaded(exports):
        if profile_loaded is not None:
            profile_loaded(exports)
        capture._install_early_lifecycle(exports)
        configure(exports, loaded, session.pid)

    session = CaptureSession(
        capture._frida_host(),
        str(profile.bundle_id),
        str(net_capture.AGENT_JS),
        warmup=capture._warmup(),
        on_loaded=on_loaded,
        identity_path=shot_dir.parent / "build-identity.json",
        run_id=spec["run_id"],
        app=spec["app"],
        controlled_identity={key: spec[key] for key in ("run_id", "app", "device")},
    )
    return session


def wait_ready(shot_dir, profile):
    """Read OCR readiness without creating a second Frida attachment."""
    from autophone.observation.capture import wait_for_spawn_ready

    return wait_for_spawn_ready(shot_dir, profile, allow_ax=False)
