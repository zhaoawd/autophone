"""Atomic local checkpoints for ACK-committed managed location sources."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
import tempfile
from typing import Any, Mapping

from autophone.location.contracts import LocationSessionSpec


CHECKPOINT_SCHEMA = "autophone.location-checkpoints/v1"
DEFAULT_CHECKPOINT_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "location-checkpoints.local.json"
)


class LocationCheckpointError(RuntimeError):
    pass


class LocationCheckpointStore:
    """Persist only committed source progress; runtime handles never enter the file."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.environ.get("AUTOPHONE_LOCATION_CHECKPOINTS")
        self.path = Path(configured) if configured else DEFAULT_CHECKPOINT_PATH
        self._mu = RLock()

    def load(self, session: LocationSessionSpec) -> dict[str, Any] | None:
        with self._mu:
            document = self._read_locked()
            record = (document.get("sessions") or {}).get(session.session_id)
            if record is None:
                return None
            if not isinstance(record, Mapping):
                raise LocationCheckpointError(
                    f"checkpoint {session.session_id!r} must be an object"
                )
            if record.get("spec") != session.as_dict():
                raise LocationCheckpointError(
                    f"checkpoint {session.session_id!r} does not match session spec"
                )
            checkpoint = record.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise LocationCheckpointError(
                    f"checkpoint {session.session_id!r} has no source state"
                )
            return dict(checkpoint)

    def session_spec(self, session_id: str) -> LocationSessionSpec:
        with self._mu:
            document = self._read_locked()
            record = (document.get("sessions") or {}).get(str(session_id))
            if not isinstance(record, Mapping) or not isinstance(
                record.get("spec"), Mapping
            ):
                raise KeyError(str(session_id))
            try:
                return LocationSessionSpec.from_mapping(record["spec"])
            except ValueError as exc:
                raise LocationCheckpointError(
                    f"checkpoint {session_id!r} has invalid session spec: {exc}"
                ) from exc

    def save(
        self,
        session: LocationSessionSpec,
        checkpoint: Mapping[str, Any],
    ) -> None:
        if not isinstance(checkpoint, Mapping):
            raise LocationCheckpointError("checkpoint must be an object")
        with self._mu:
            document = self._read_locked()
            sessions = dict(document.get("sessions") or {})
            sessions[session.session_id] = {
                "spec": session.as_dict(),
                "checkpoint": dict(checkpoint),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_locked({"schema": CHECKPOINT_SCHEMA, "sessions": sessions})

    def remove(self, session_id: str) -> None:
        with self._mu:
            document = self._read_locked()
            sessions = dict(document.get("sessions") or {})
            if str(session_id) not in sessions:
                return
            sessions.pop(str(session_id), None)
            self._write_locked({"schema": CHECKPOINT_SCHEMA, "sessions": sessions})

    def summaries(self) -> tuple[dict[str, Any], ...]:
        with self._mu:
            document = self._read_locked()
            result = []
            for session_id, raw in sorted((document.get("sessions") or {}).items()):
                record = dict(raw) if isinstance(raw, Mapping) else {}
                spec = record.get("spec") if isinstance(record.get("spec"), Mapping) else {}
                source = spec.get("source") if isinstance(spec.get("source"), Mapping) else {}
                checkpoint = record.get("checkpoint") if isinstance(record.get("checkpoint"), Mapping) else {}
                result.append({
                    "session_id": session_id,
                    "owner": spec.get("owner"),
                    "device": spec.get("device"),
                    "source_kind": source.get("kind"),
                    "route_id": source.get("route_id"),
                    "route_distance_m": checkpoint.get("route_distance_m"),
                    "updated_at": record.get("updated_at"),
                })
            return tuple(result)

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": CHECKPOINT_SCHEMA, "sessions": {}}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocationCheckpointError(
                f"cannot read location checkpoint store {self.path}: {exc}"
            ) from exc
        if not isinstance(document, dict) or document.get("schema") != CHECKPOINT_SCHEMA:
            raise LocationCheckpointError(
                f"invalid location checkpoint store schema: {self.path}"
            )
        sessions = document.get("sessions")
        if not isinstance(sessions, dict):
            raise LocationCheckpointError(
                f"location checkpoint sessions must be an object: {self.path}"
            )
        return document

    def _write_locked(self, document: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".location-checkpoints-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            Path(temp_name).replace(self.path)
        except OSError as exc:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise LocationCheckpointError(
                f"cannot write location checkpoint store {self.path}: {exc}"
            ) from exc
