"""Scenario 临时会话 → 平台 explore run 证据。

Scenario 自己仍是短生命周期控制器；本模块只负责在关闭后把已确认命中的 merged API 调用、
规则摘要和 cleanup 结果固化成标准 run 目录，让既有 history/detail/api-capture 页面直接消费。
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autophone.observation.artifacts import Artifact, write_artifact_manifest
from autophone.contracts import validate_or_raise, write_json
from autophone.version import inherit_run_identity, stamp_run_spec


@dataclass(frozen=True)
class _EvidenceContext:
    run_id: str
    run_dir: Path
    app: str
    env: str
    device: str
    started_at: str
    scenario_id: str
    rule_snapshot_hash: str
    metadata: dict[str, Any]


class ScenarioEvidenceStore:
    """把活动 Scenario 会话登记并在终态原子化成平台可发现的 run。"""

    def __init__(
        self,
        *,
        run_root: str | Path,
        persist_result: Callable[[Path, dict[str, Any]], Path] | None = None,
    ) -> None:
        self.run_root = Path(run_root)
        self._persist_result = persist_result or self._write_result
        self._active: dict[str, _EvidenceContext] = {}
        self._lock = threading.RLock()

    def begin(
        self,
        *,
        session_id: str,
        app: str,
        env: str,
        device: str,
        scenario_id: str,
        rule_snapshot_hash: str,
        ttl_s: float,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = _now()
        run_id = session_id
        run_dir = self.run_root / datetime.now().strftime("%Y%m%d") / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        spec = {
            "schema_version": "v1",
            "run_id": run_id,
            "type": "explore",
            "app": app,
            "device": device,
            "planes": {
                "ui": {"mode": "disabled"},
                "function": {"mode": "disabled"},
                # 场景启动时只有 transport/delivery 观测；业务断言可能在会话结束时才得到。
                # 未配置业务断言时这是 advisory capture，不能预先承诺 required/pass。
                "api": {"mode": "advisory"},
                "telemetry": {"mode": "disabled"},
            },
            "requested_by": "web-scenario",
            "requested_at": started_at,
        }
        spec = stamp_run_spec(spec)
        validate_or_raise("run-spec.v1.json", spec)
        write_json(run_dir / "run-spec.json", spec)
        context = _EvidenceContext(
            run_id=run_id,
            run_dir=run_dir,
            app=app,
            env=env,
            device=device,
            started_at=started_at,
            scenario_id=scenario_id,
            rule_snapshot_hash=rule_snapshot_hash,
            metadata=copy.deepcopy(metadata or {}),
        )
        with self._lock:
            self._active[session_id] = context
        write_json(run_dir / "scenario-evidence.json", {
            "schema_version": "v1",
            "run_id": run_id,
            "session_id": session_id,
            "status": "active",
            "app": app,
            "env": env,
            "device": device,
            "scenario_id": scenario_id,
            "rule_snapshot_hash": rule_snapshot_hash,
            "ttl_s": ttl_s,
            "started_at": started_at,
            "metadata": context.metadata,
        })
        return {"run_id": run_id, "run_dir": str(run_dir)}

    def finish(self, session_id: str, terminal: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            context = self._active.pop(session_id, None)
        if context is None:
            raise KeyError(f"scenario evidence context 不存在: {session_id}")
        close = terminal.get("close") or {}
        merged = list(close.get("merged") or [])
        capture_path = context.run_dir / "planes" / "api" / "capture.jsonl"
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(
            "".join(json.dumps(rec, ensure_ascii=False, default=str) + "\n" for rec in merged),
            encoding="utf-8",
        )
        ended_at = _now()
        effective = close.get("verdict") == "effective"
        cleanup_verified = bool(close.get("cleanup_verified"))
        delivery_verdict = "delivered" if effective else "not_delivered"
        business_verdict = str(terminal.get("business_verdict") or "unverified")
        if business_verdict not in {"verified", "unverified", "failed"}:
            raise ValueError(f"business_verdict 非法: {business_verdict!r}")
        business_evidence = terminal.get("business_evidence")
        observations = terminal.get("observations") or {}
        upstream = observations.get("upstream") if isinstance(observations, dict) else {}
        upstream = upstream if isinstance(upstream, dict) else {}
        zero_touch_required = (
            context.metadata.get("upstream_policy")
            == "local_short_circuit_requires_observation"
        )
        zero_touch_verified = upstream.get("verified_zero_touch") is True
        upstream_violation = zero_touch_required and upstream.get("status") == "requests_observed"
        cleanup = close.get("cleanup") or {}
        restoration = cleanup.get("restoration") if isinstance(cleanup, dict) else {}
        restoration = restoration if isinstance(restoration, dict) else {}
        recovery_required = (
            context.metadata.get("recovery_policy")
            == "attach_requires_post_detach_observation"
        )
        recovery_verified = restoration.get("verified") is True
        recovery_failed = recovery_required and restoration.get("status") in {
            "failed", "error", "invalid", "direct_request_failed",
        }
        if business_verdict == "verified" and not isinstance(business_evidence, dict):
            raise ValueError("business_verdict=verified 必须附 business_evidence")
        transport_ok = effective and cleanup_verified
        if (
            not transport_ok or business_verdict == "failed"
            or upstream_violation or recovery_failed
        ):
            run_status = "failed"
            api_mode = "required"
            api_verdict = "fail"
        elif business_verdict == "verified" and (
            not zero_touch_required or zero_touch_verified
        ) and (
            not recovery_required or recovery_verified
        ):
            run_status = "passed"
            api_mode = "required"
            api_verdict = "pass"
        else:
            run_status = "partial"
            api_mode = "advisory"
            api_verdict = "captured"
        evidence = {
            "schema_version": "v1",
            "run_id": context.run_id,
            "session_id": session_id,
            "status": "closed",
            "app": context.app,
            "env": context.env,
            "device": context.device,
            "scenario_id": context.scenario_id,
            "rule_snapshot_hash": context.rule_snapshot_hash,
            "started_at": context.started_at,
            "ended_at": ended_at,
            "close_reason": terminal.get("close_reason"),
            "verdict": close.get("verdict"),
            "delivery_verdict": delivery_verdict,
            "business_verdict": business_verdict,
            "business_evidence": business_evidence if isinstance(business_evidence, dict) else None,
            "upstream_observation": copy.deepcopy(upstream),
            "zero_touch_required": zero_touch_required,
            "recovery_required": recovery_required,
            "restoration_verified": recovery_verified,
            "hits": int(close.get("hits") or 0),
            "hit_rules": list(close.get("hit_rules") or []),
            "missed_rules": list(close.get("missed_rules") or []),
            "unapplied_matches": int(close.get("unapplied_matches") or 0),
            "cleanup_verified": cleanup_verified,
            "cleanup": cleanup,
            "scenario_hits": list(close.get("scenario_hits") or []),
            "capture": {"path": "planes/api/capture.jsonl", "calls": len(merged)},
            "metadata": context.metadata,
        }
        write_json(context.run_dir / "scenario-evidence.json", evidence)
        detail = (
            f"scenario={context.scenario_id}; hits={evidence['hits']}; "
            f"missed={','.join(evidence['missed_rules']) or '-'}; "
            f"delivery={delivery_verdict}; business={business_verdict}; "
            f"upstream={upstream.get('status', 'unconfigured')}; "
            f"cleanup_verified={str(cleanup_verified).lower()}"
        )
        result = {
            "schema_version": "v1",
            "run_id": context.run_id,
            "type": "explore",
            "app": context.app,
            "env": context.env,
            "device": context.device,
            "status": run_status,
            "planes": {
                "ui": {"mode": "disabled", "verdict": "not_configured"},
                "function": {"mode": "disabled", "verdict": "not_configured"},
                "api": {
                    "mode": api_mode,
                    "verdict": api_verdict,
                    "detail": detail,
                },
                "telemetry": {"mode": "disabled", "verdict": "not_configured"},
            },
            "artifacts": "artifact-manifest.json",
            "lock_phase": {
                "execute": {
                    "status": "ok" if cleanup_verified else "failed",
                    "started_at": context.started_at,
                    "ended_at": ended_at,
                },
                "collect": {
                    "status": "ok",
                    "started_at": ended_at,
                    "ended_at": ended_at,
                },
            },
            "timestamps": {
                "queued_at": context.started_at,
                "execute_started_at": context.started_at,
                "execute_ended_at": ended_at,
                "collect_ended_at": ended_at,
            },
        }
        self._persist_result(context.run_dir, result)
        manifest = write_artifact_manifest(context.run_dir, context.run_id, [
            Artifact("run-spec.json", "other", share_policy="forbidden"),
            Artifact("platform-run-result.json", "report"),
            Artifact("scenario-evidence.json", "other", sensitivity="sensitive",
                     redaction_status="summary_only", retention="local_only", share_policy="forbidden"),
            Artifact("planes/api/capture.jsonl", "api_capture", sensitivity="sensitive",
                     redaction_status="raw", retention="local_only", share_policy="forbidden"),
        ])
        return {
            "run_id": context.run_id,
            "run_dir": str(context.run_dir),
            "artifact_manifest": str(manifest),
            "status": result["status"],
        }

    def _write_result(self, run_dir: Path, result: dict[str, Any]) -> Path:
        try:
            spec = json.loads((run_dir / "run-spec.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            spec = None
        result = inherit_run_identity(result, spec)
        validate_or_raise("run-result.v1.json", result)
        path = write_json(run_dir / "platform-run-result.json", result)
        from autophone.application import run_index

        run_index.ingest_run(run_dir, run_root=self.run_root)
        return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
