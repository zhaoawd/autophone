"""受控的 telemetry 事件字典匹配器。

字典只解释本地 YAML 中的声明式条件，不执行表达式，也不把条件转发给 dataTrace。
公共接口只有 :func:`compile_event_dictionary` 返回的 ``EventDictionary``；调用方可按
事件名匹配，或给事件补充稳定的中文名/事件种类。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,7}$")
_MISSING = object()
_OPS = frozenset({"eq", "in", "contains", "exists"})


class TelemetryDictionaryError(ValueError):
    """字典结构或受控条件非法。"""


@dataclass(frozen=True)
class _Condition:
    field: str
    op: Literal["eq", "in", "contains", "exists"]
    value: Any = None


@dataclass(frozen=True)
class _Rule:
    name: str
    label: str
    kind: str | None
    relation: Literal["all", "any"]
    conditions: tuple[_Condition, ...]
    required_fields: tuple[str, ...]


def _path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _same(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, (dict, list)) or isinstance(wanted, (dict, list)):
        return actual == wanted
    return str(actual) == str(wanted)


def _condition_matches(event: dict[str, Any], condition: _Condition) -> bool:
    actual = _path(event, condition.field)
    if condition.op == "exists":
        return actual is not _MISSING and actual not in (None, "")
    if actual is _MISSING:
        return False
    if condition.op == "eq":
        return _same(actual, condition.value)
    if condition.op == "in":
        return any(_same(actual, item) for item in condition.value)
    if condition.op == "contains":
        if isinstance(actual, str):
            return str(condition.value) in actual
        if isinstance(actual, list):
            return any(_same(item, condition.value) for item in actual)
        if isinstance(actual, dict):
            return str(condition.value) in actual
        return False
    raise AssertionError("unreachable")


def _validated_path(value: Any, *, context: str) -> str:
    path = str(value or "").strip()
    if not _PATH_RE.fullmatch(path) or path == "raw" or path.startswith("raw."):
        raise TelemetryDictionaryError(f"{context} has an invalid field path")
    return path


def _condition(raw: Any, *, context: str) -> _Condition:
    if not isinstance(raw, dict):
        raise TelemetryDictionaryError(f"{context} must be an object")
    unknown = set(raw) - {"field", "op", "value"}
    if unknown:
        raise TelemetryDictionaryError(f"{context} has unknown keys")
    field = _validated_path(raw.get("field"), context=context)
    op = str(raw.get("op") or "").strip()
    if op not in _OPS:
        raise TelemetryDictionaryError(f"{context} has an unsupported operator")
    has_value = "value" in raw
    value = raw.get("value")
    if op == "exists":
        if has_value:
            raise TelemetryDictionaryError(f"{context} exists does not accept value")
    elif op == "in":
        if not isinstance(value, list) or not value:
            raise TelemetryDictionaryError(f"{context} in requires a non-empty list")
    elif not has_value:
        raise TelemetryDictionaryError(f"{context} {op} requires value")
    return _Condition(field=field, op=op, value=value)


def _rule(name: str, raw: Any) -> _Rule:
    if not isinstance(raw, dict):
        raise TelemetryDictionaryError(f"event rule {name!r} must be an object")
    unknown = set(raw) - {"label", "kind", "match", "required_fields", "source"}
    if unknown:
        raise TelemetryDictionaryError(f"event rule {name!r} has unknown keys")
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        raise TelemetryDictionaryError(f"event rule {name!r} requires match")

    structured = set(match) & {"all", "any"}
    if structured:
        if len(structured) != 1 or set(match) != structured:
            raise TelemetryDictionaryError(
                f"event rule {name!r} match must contain exactly one of all/any"
            )
        relation = next(iter(structured))
        raw_conditions = match[relation]
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise TelemetryDictionaryError(
                f"event rule {name!r} match.{relation} must be a non-empty list"
            )
        conditions = tuple(
            _condition(item, context=f"event rule {name!r} condition")
            for item in raw_conditions
        )
    else:
        # v1 兼容：``match: {category: 202, event: click}`` 等价于 all + eq。
        relation = "all"
        conditions = tuple(
            _Condition(
                field=_validated_path(field, context=f"event rule {name!r}"),
                op="eq",
                value=value,
            )
            for field, value in match.items()
        )

    required = raw.get("required_fields") or []
    if not isinstance(required, list):
        raise TelemetryDictionaryError(
            f"event rule {name!r} required_fields must be a list"
        )
    required_fields = tuple(
        _validated_path(field, context=f"event rule {name!r} required_fields")
        for field in required
    )
    label = str(raw.get("label") or name).strip() or name
    kind = str(raw.get("kind") or "").strip() or None
    return _Rule(
        name=name,
        label=label,
        kind=kind,
        relation=relation,  # type: ignore[arg-type]
        conditions=conditions,
        required_fields=required_fields,
    )


@dataclass(frozen=True)
class EventDictionary:
    """已编译字典；匹配知识集中在这里，调用方不解释 YAML 结构。"""

    _rules: tuple[_Rule, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self._rules)

    def has(self, name: str) -> bool:
        return any(rule.name == name for rule in self._rules)

    def matches(self, name: str, event: dict[str, Any]) -> bool:
        rule = next((item for item in self._rules if item.name == name), None)
        if rule is None:
            return False
        results = [_condition_matches(event, item) for item in rule.conditions]
        return all(results) if rule.relation == "all" else any(results)

    def missing_required(self, name: str, events: list[dict[str, Any]]) -> list[str]:
        rule = next((item for item in self._rules if item.name == name), None)
        if rule is None:
            return []
        return [
            field
            for field in rule.required_fields
            if not any(
                (value := _path(event, field)) is not _MISSING
                and value not in (None, "")
                for event in events
            )
        ]

    def annotate(self, event: dict[str, Any]) -> dict[str, Any]:
        annotated = dict(event)
        for rule in self._rules:
            if not self.matches(rule.name, event):
                continue
            annotated["dictionary_event"] = rule.name
            annotated["name"] = rule.label
            if rule.kind:
                annotated["kind"] = rule.kind
            break
        return annotated


def compile_event_dictionary(document: Any) -> EventDictionary:
    """校验并编译 YAML 解析结果；非法字典 fail-closed。"""

    if document in (None, {}):
        return EventDictionary(())
    if not isinstance(document, dict) or set(document) - {"version", "events"}:
        raise TelemetryDictionaryError("telemetry dictionary root is invalid")
    events = document.get("events") or {}
    if not isinstance(events, dict):
        raise TelemetryDictionaryError("telemetry dictionary events must be an object")
    rules: list[_Rule] = []
    for raw_name, raw_rule in events.items():
        name = str(raw_name or "").strip()
        if not name:
            raise TelemetryDictionaryError("telemetry dictionary event name is required")
        rules.append(_rule(name, raw_rule))
    return EventDictionary(tuple(rules))
