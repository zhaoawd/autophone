"""Versioned, app-scoped templates for generic API interception scenarios.

The store owns validation, append-only rule versions, snapshot integrity and atomic
YAML writes.  HTTP callers never hand a mutable template directly to ``start``;
``build_scenario`` revalidates the selected immutable version first.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from autophone.assets.apps import AppProfile
from autophone.assets.policy import assert_assets_writable
from autophone.assets.store import path_write_lock
from autophone.scenario.rules import validate_or_raise


class ScenarioPresetError(ValueError):
    """A generic scenario preset request or registry is invalid."""


class ScenarioPresetNotFound(ScenarioPresetError):
    """The requested preset or immutable version does not exist."""


class ScenarioPresetConflict(ScenarioPresetError):
    """The requested mutation conflicts with the preset lifecycle."""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_STATUS = frozenset({"draft", "active", "deprecated"})
_SOURCES = frozenset({"manual", "capture", "sample", "conversation"})


class ScenarioPresetStore:
    """Generic API scenario registry for one app.

    A preset fixes its environment.  Rule changes append an immutable version and
    each version stores the hash of the exact compiled rules sent to the agent.
    """

    def __init__(self, profile: AppProfile, *, path: str | Path | None = None) -> None:
        self.profile = profile
        self.path = Path(path) if path is not None else profile.profile_dir / "scenario_presets.yaml"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())

    def list(self, *, env: str | None = None, include_deprecated: bool = False) -> list[dict[str, Any]]:
        wanted_env = str(env or "").strip().lower() or None
        with self._lock:
            result = []
            for preset in self._load()["presets"]:
                if wanted_env is not None and preset["env"] != wanted_env:
                    continue
                if not include_deprecated and preset["status"] == "deprecated":
                    continue
                result.append(self._summary(preset))
            return result

    def get(self, preset_id: str, *, version: int | None = None) -> dict[str, Any]:
        with self._lock:
            return self._detail(self._find(self._load(), preset_id), version=version)

    def create(
        self, *, name: Any, selected_env: Any, rules: Any,
        source: Any = "manual", source_ref: Any = None, note: Any = None,
        status: Any = "draft",
    ) -> dict[str, Any]:
        assert_assets_writable("create scenario preset", path=self.path)
        clean_name = _name(name)
        clean_env = _env(selected_env)
        clean_status = _status(status, allow_deprecated=False)
        version = self._compile_version(
            clean_env, rules, version=1, source=_source(source),
            source_ref=source_ref, note=note,
        )
        now = _now()
        with self._lock, path_write_lock(self.path):
            registry = self._load()
            seq = int(registry["meta"]["next_seq"])
            preset_id = f"ASP{seq:04d}"
            if any(item["id"] == preset_id for item in registry["presets"]):
                raise ScenarioPresetConflict(f"scenario preset next_seq 冲突: {preset_id}")
            preset = {
                "id": preset_id, "name": clean_name, "env": clean_env,
                "status": clean_status, "latest_version": 1,
                "created_at": now, "updated_at": now, "versions": [version],
            }
            registry["presets"].append(preset)
            registry["meta"]["next_seq"] = seq + 1
            self._write(registry)
            return self._detail(preset, version=1)

    def update(self, preset_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        assert_assets_writable("update scenario preset", path=self.path)
        if not isinstance(changes, dict) or not changes:
            raise ScenarioPresetError("changes 至少包含一个字段")
        allowed = {"name", "status", "rules", "source", "source_ref", "note", "expected_revision"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ScenarioPresetError(f"scenario preset 不支持修改字段: {', '.join(unknown)}")
        has_rules = "rules" in changes
        if not has_rules and set(changes) & {"source", "source_ref", "note"}:
            raise ScenarioPresetError("source/source_ref/note 只能随新 rules 版本写入")
        with self._lock, path_write_lock(self.path):
            registry = self._load()
            preset = self._find(registry, preset_id)
            self._check_revision(preset, changes.get("expected_revision"))
            if preset["status"] == "deprecated":
                raise ScenarioPresetConflict(f"scenario preset 已废弃: {preset_id}")
            changed = False
            if "name" in changes:
                value = _name(changes["name"])
                if value != preset["name"]:
                    preset["name"] = value
                    changed = True
            if "status" in changes:
                value = _status(changes["status"], allow_deprecated=False)
                if value != preset["status"]:
                    preset["status"] = value
                    changed = True
            if has_rules:
                next_version = int(preset["latest_version"]) + 1
                record = self._compile_version(
                    preset["env"], changes["rules"], version=next_version,
                    source=_source(changes.get("source", "manual")),
                    source_ref=changes.get("source_ref"), note=changes.get("note"),
                )
                if _version_content(record) != _version_content(preset["versions"][-1]):
                    preset["versions"].append(record)
                    preset["latest_version"] = next_version
                    changed = True
            if changed:
                preset["updated_at"] = _now()
                self._write(registry)
            return self._detail(preset)

    def deprecate(self, preset_id: str, *, reason: Any = None, expected_revision: str | None = None) -> dict[str, Any]:
        assert_assets_writable("deprecate scenario preset", path=self.path)
        with self._lock, path_write_lock(self.path):
            registry = self._load()
            preset = self._find(registry, preset_id)
            self._check_revision(preset, expected_revision)
            if preset["status"] != "deprecated":
                preset["status"] = "deprecated"
                preset["updated_at"] = _now()
                clean_reason = _text(reason, "reason", 500)
                if clean_reason is not None:
                    preset["deprecate_reason"] = clean_reason
                self._write(registry)
            return self._detail(preset)

    def build_scenario(
        self, preset_id: str, *, version: int | None = None,
        scenario_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            preset = copy.deepcopy(self._find(self._load(), preset_id))
        if preset["status"] == "deprecated":
            raise ScenarioPresetConflict(f"scenario preset 已废弃，不能启动: {preset_id}")
        record = self._version(preset, version)
        scenario = {
            "id": scenario_id or f"api-scenario-{preset_id.lower()}-v{record['version']}",
            "app": self.profile.app,
            "env": preset["env"],
            "rules": copy.deepcopy(record["rules"]),
        }
        compiled = validate_or_raise(scenario)
        if compiled.snapshot_hash != record["rule_snapshot_hash"]:
            raise ScenarioPresetConflict(
                f"scenario preset 规则快照不一致: {preset_id} v{record['version']}"
            )
        binding = {
            "preset_id": preset["id"], "preset_name": preset["name"],
            "version": record["version"], "env": preset["env"],
            "rule_snapshot_hash": record["rule_snapshot_hash"],
        }
        return scenario, binding

    def validate_rules(self, *, selected_env: Any, rules: Any) -> dict[str, Any]:
        """Validate authoring input without reading a registry or contacting a device."""
        record = self._compile_version(
            _env(selected_env), rules, version=1, source="manual", source_ref=None, note=None,
        )
        return {"valid": True, "rule_count": len(rules),
                "rule_snapshot_hash": record["rule_snapshot_hash"]}

    @staticmethod
    def _check_revision(preset: dict[str, Any], expected: Any) -> None:
        if expected is not None and expected != _revision(preset):
            raise ScenarioPresetConflict("模板已被其他编辑更新，请读取最新版本后重试")

    def _compile_version(
        self, env: str, rules: Any, *, version: int, source: str,
        source_ref: Any, note: Any,
    ) -> dict[str, Any]:
        _validate_editor_rules(rules)
        scenario = {"id": f"api-scenario-preset-v{version}", "app": self.profile.app,
                    "env": env, "rules": copy.deepcopy(rules)}
        try:
            compiled = validate_or_raise(scenario)
        except ValueError as exc:
            raise ScenarioPresetError(str(exc)) from exc
        record: dict[str, Any] = {
            "version": version, "source": source, "created_at": _now(),
            "rules": copy.deepcopy(rules), "rule_snapshot_hash": compiled.snapshot_hash,
        }
        clean_ref = _text(source_ref, "source_ref", 500)
        clean_note = _text(note, "note", 1000)
        if clean_ref is not None:
            record["source_ref"] = clean_ref
        if clean_note is not None:
            record["note"] = clean_note
        return record

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "v1", "app": self.profile.app,
                    "meta": {"next_seq": 1}, "presets": []}
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ScenarioPresetError(f"scenario preset YAML 非法: {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != "v1" or raw.get("app") != self.profile.app:
            raise ScenarioPresetError(f"scenario preset registry 头非法: {self.path}")
        meta, presets = raw.get("meta"), raw.get("presets")
        if not isinstance(meta, dict) or type(meta.get("next_seq")) is not int or meta["next_seq"] < 1:
            raise ScenarioPresetError("scenario preset meta.next_seq 非法")
        if not isinstance(presets, list):
            raise ScenarioPresetError("scenario preset presets 必须是列表")
        seen: set[str] = set()
        for preset in presets:
            self._validate_record(preset, seen)
        if any(f"ASP{meta['next_seq']:04d}" == p["id"] for p in presets):
            raise ScenarioPresetError("scenario preset meta.next_seq 已被占用")
        return raw

    def _validate_record(self, preset: Any, seen: set[str]) -> None:
        if not isinstance(preset, dict):
            raise ScenarioPresetError("scenario preset 条目必须是对象")
        pid = str(preset.get("id") or "")
        if not pid or pid in seen:
            raise ScenarioPresetError(f"scenario preset id 缺失或重复: {pid!r}")
        seen.add(pid)
        if preset.get("status") not in _STATUS or not str(preset.get("name") or "").strip():
            raise ScenarioPresetError(f"scenario preset 元数据非法: {pid}")
        _env(preset.get("env"))
        versions, latest = preset.get("versions"), preset.get("latest_version")
        if not isinstance(versions, list) or not versions or type(latest) is not int or latest != len(versions):
            raise ScenarioPresetError(f"scenario preset versions 非法: {pid}")
        if any(not isinstance(v, dict) or type(v.get("version")) is not int for v in versions) or [v.get("version") for v in versions if isinstance(v, dict)] != list(range(1, len(versions) + 1)):
            raise ScenarioPresetError(f"scenario preset version 序列非法: {pid}")
        for record in versions:
            if record.get("source") not in _SOURCES or not isinstance(record.get("rules"), list):
                raise ScenarioPresetError(f"scenario preset version 内容非法: {pid}")
            scenario = {"id": "registry-check", "app": self.profile.app,
                        "env": preset["env"], "rules": copy.deepcopy(record["rules"])}
            try:
                compiled = validate_or_raise(scenario)
            except ValueError as exc:
                raise ScenarioPresetError(f"scenario preset 规则非法: {pid}: {exc}") from exc
            if compiled.snapshot_hash != record.get("rule_snapshot_hash"):
                raise ScenarioPresetError(f"scenario preset 规则 hash 非法: {pid}")

    def _write(self, registry: dict[str, Any]) -> None:
        assert_assets_writable("write scenario preset registry", path=self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(registry, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _find(registry: dict[str, Any], preset_id: str) -> dict[str, Any]:
        wanted = str(preset_id or "").strip()
        for preset in registry["presets"]:
            if preset["id"] == wanted:
                return preset
        raise ScenarioPresetNotFound(f"scenario preset 不存在: {wanted}")

    @staticmethod
    def _version(preset: dict[str, Any], version: int | None) -> dict[str, Any]:
        selected = preset["latest_version"] if version is None else version
        if not isinstance(selected, int) or isinstance(selected, bool) or selected < 1:
            raise ScenarioPresetError("scenario preset version 必须是正整数")
        for record in preset["versions"]:
            if record["version"] == selected:
                return record
        raise ScenarioPresetNotFound(f"scenario preset version 不存在: {preset['id']} v{selected}")

    @classmethod
    def _detail(cls, preset: dict[str, Any], *, version: int | None = None) -> dict[str, Any]:
        out = copy.deepcopy(preset)
        out["revision"] = _revision(preset)
        out["selected_version"] = copy.deepcopy(cls._version(preset, version))
        out.pop("versions", None)
        return out

    @staticmethod
    def _summary(preset: dict[str, Any]) -> dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in preset.items() if k != "versions"}
        out["revision"] = _revision(preset)
        out["rule_snapshot_hash"] = preset["versions"][-1]["rule_snapshot_hash"]
        out["rule_count"] = len(preset["versions"][-1]["rules"])
        return out


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 120:
        raise ScenarioPresetError("scenario preset name 必填且最长 120 字符")
    return name


def _env(value: Any) -> str:
    env = str(value or "").strip().lower()
    if not env or len(env) > 80:
        raise ScenarioPresetError("scenario preset env 必填且最长 80 字符")
    return env


def _status(value: Any, *, allow_deprecated: bool) -> str:
    status = str(value or "").strip().lower()
    allowed = _STATUS if allow_deprecated else _STATUS - {"deprecated"}
    if status not in allowed:
        raise ScenarioPresetError(f"scenario preset status 非法: {status!r}")
    return status


def _source(value: Any) -> str:
    source = str(value or "").strip().lower()
    if source not in _SOURCES:
        raise ScenarioPresetError(f"scenario preset source 非法: {source!r}")
    return source


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise ScenarioPresetError(f"{field} 最长 {limit} 字符")
    return text or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _revision(preset: dict[str, Any]) -> str:
    # Includes metadata and all immutable versions; no migration of v1 YAML required.
    data = json.dumps(preset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _version_content(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in {"version", "created_at"}}


def _validate_editor_rules(rules: Any) -> None:
    """Reject ignored fields/types at the authoring boundary; keep legacy reads compatible."""
    try:
        json.dumps(rules, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ScenarioPresetError("rules 必须是有限值 JSON") from exc
    if not isinstance(rules, list):
        raise ScenarioPresetError("rules 必须是列表")
    allowed = {
        "rule": {"rule_id", "name", "match", "action", "times", "assert_hit"},
        "match": {"method", "host", "path", "path_prefix", "path_suffix", "query"},
        "action": {"type", "status", "headers", "body"},
        "times": {"mode", "n"},
    }
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ScenarioPresetError(f"rules[{i}] 必须是对象")
        for key, fields in allowed.items():
            obj = rule if key == "rule" else rule.get(key, {})
            if not isinstance(obj, dict):
                raise ScenarioPresetError(f"rules[{i}].{key} 必须是对象")
            unknown = set(obj) - fields
            if unknown:
                raise ScenarioPresetError(f"rules[{i}].{key} 不支持字段: {', '.join(sorted(unknown))}")
        body = rule.get("action", {}).get("body")
        if isinstance(body, str) and rule.get("action", {}).get("type") == "fixed_json":
            try:
                json.dumps(json.loads(body), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ScenarioPresetError(f"rules[{i}].action.body 必须是有限值 JSON") from exc
        if "assert_hit" in rule and not isinstance(rule["assert_hit"], bool):
            raise ScenarioPresetError(f"rules[{i}].assert_hit 必须是布尔值")
        for obj, fields in ((rule, ("rule_id", "name")),
                            (rule.get("match", {}), ("method", "host", "path", "path_prefix", "path_suffix"))):
            for key in fields:
                if key in obj and not isinstance(obj[key], str):
                    raise ScenarioPresetError(f"rules[{i}].{key} 必须是字符串")
