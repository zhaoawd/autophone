"""Case-level business entity binding contract & static validation (Stage 2).

Distinct from class-level ``field-binding.v1.json`` (which intentionally forbids
instance IDs), ``entity-binding.v1.json`` binds a concrete expected business
entity instance (``entity_type``, ``entity_id``, ``attributes``) onto a Case so
UI assertions, window-scoped API capture assertions (``api_window``), and
post-action data-model readbacks (``readback``) can be arbitrated together
fail-closed.
"""
from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from autophone.assets.api_assertions import (
    ALLOWED_OPS,
    CROSS_ELEMENT_OPS,
    _spec_op,
    _static_check_cross_op,
    static_check_assert_key,
)
from autophone.assets.effect_markers import static_step_effect
from autophone.assets.operations import OperationError, _validate_args, load_operations
from autophone.contracts import _validator

ENTITY_BINDING_SCHEMA_NAME = "entity-binding.v1.json"

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\}")
_HOUSEKEEPING_STEPS = frozenset({"body_end", "post_case_cleanup", "entity_readback"})


@dataclass(frozen=True)
class ResolvedEntity:
    """Resolved concrete business entity identity and template variables for one Case."""

    entity_type: str
    entity_id: str | None
    attributes: dict[str, Any]
    template_vars: dict[str, Any]
    unresolved: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "attributes": dict(self.attributes),
            "template_vars": dict(self.template_vars),
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True)
class EntityBindingIssue:
    """One static validation violation on a Case's ``entity_binding`` declaration."""

    code: str
    message: str
    field: str = ""


def case_has_entity_binding(case: Mapping[str, Any]) -> bool:
    """Return True when ``case`` declares a non-null ``entity_binding`` section."""
    return isinstance(case, Mapping) and case.get("entity_binding") is not None


def policy_from_entity_binding(binding: Mapping[str, Any] | None) -> dict[str, bool]:
    """Derive ``{require_ui, require_api, require_readback}`` from a binding mapping."""
    if not isinstance(binding, Mapping):
        return {
            "require_ui": True,
            "require_api": False,
            "require_readback": False,
        }
    jv = binding.get("joint_verdict")
    jv_map = jv if isinstance(jv, Mapping) else {}
    require_ui = bool(jv_map.get("require_ui", True))
    if "require_api" in jv_map:
        require_api = bool(jv_map["require_api"])
    else:
        require_api = bool(binding.get("api_window"))
    if "require_readback" in jv_map:
        require_readback = bool(jv_map["require_readback"])
    else:
        require_readback = bool(binding.get("readback"))
    return {
        "require_ui": require_ui,
        "require_api": require_api,
        "require_readback": require_readback,
    }


def entity_binding_policy(case: Mapping[str, Any]) -> dict[str, bool]:
    """Derive joint-verdict plane requirements for ``case``."""
    if not case_has_entity_binding(case):
        return {
            "require_ui": True,
            "require_api": False,
            "require_readback": False,
        }
    binding = case.get("entity_binding")
    return policy_from_entity_binding(binding if isinstance(binding, Mapping) else None)


def case_requires_api_capture(case: Mapping[str, Any]) -> bool:
    """Return True when ``case`` requires API capture for joint arbitration."""
    if not case_has_entity_binding(case):
        return False
    return entity_binding_policy(case)["require_api"]


def case_requires_entity_readback(case: Mapping[str, Any]) -> bool:
    """Return True when ``case`` requires post-action entity readback."""
    if not case_has_entity_binding(case):
        return False
    return entity_binding_policy(case)["require_readback"]


def _is_ref_dict(value: Any, ref_key: str) -> str | None:
    if not isinstance(value, Mapping) or len(value) != 1 or ref_key not in value:
        return None
    raw = value.get(ref_key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _resolve_binding_value(
    spec: Any,
    *,
    field_label: str,
    fixture_vars: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    unresolved: list[str],
) -> tuple[bool, Any]:
    if isinstance(spec, Mapping):
        fixture_key = _is_ref_dict(spec, "from_fixture")
        if fixture_key is not None:
            if fixture_key in fixture_vars and fixture_vars[fixture_key] is not None:
                return True, fixture_vars[fixture_key]
            unresolved.append(f"{field_label}:from_fixture:{fixture_key}")
            return False, None
        context_key = _is_ref_dict(spec, "from_context")
        if context_key is not None:
            if context_key in runtime_context and runtime_context[context_key] is not None:
                return True, runtime_context[context_key]
            unresolved.append(f"{field_label}:from_context:{context_key}")
            return False, None
        unresolved.append(f"{field_label}:invalid_ref")
        return False, None
    return True, spec


def resolve_case_entity(
    case: Mapping[str, Any],
    *,
    fixture_vars: Mapping[str, Any] | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> ResolvedEntity | None:
    """Resolve a Case's ``entity_binding`` into concrete entity fields & template vars.

    Never raises on missing fixture or runtime context references; instead records
    every missing reference in ``ResolvedEntity.unresolved`` so fail-closed joint
    arbitration reports ``entity_binding_unresolved``.
    """
    if not case_has_entity_binding(case):
        return None
    fixtures = dict(fixture_vars or {})
    context = dict(runtime_context or {})
    binding = case.get("entity_binding")
    if not isinstance(binding, Mapping):
        return ResolvedEntity(
            entity_type="",
            entity_id=None,
            attributes={},
            template_vars={str(k): v for k, v in fixtures.items() if v is not None},
            unresolved=("entity_binding:invalid_type",),
        )

    unresolved: list[str] = []
    raw_type = binding.get("entity_type")
    entity_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else ""
    if not entity_type:
        unresolved.append("entity_type:missing")

    entity_id: str | None = None
    if "entity_id" not in binding or binding.get("entity_id") is None:
        unresolved.append("entity_id:missing")
    else:
        ok, resolved_id = _resolve_binding_value(
            binding["entity_id"],
            field_label="entity_id",
            fixture_vars=fixtures,
            runtime_context=context,
            unresolved=unresolved,
        )
        if ok:
            if isinstance(resolved_id, bool) or resolved_id is None or not str(resolved_id).strip():
                unresolved.append("entity_id:empty")
            else:
                entity_id = str(resolved_id)

    attributes: dict[str, Any] = {}
    raw_attrs = binding.get("attributes")
    if isinstance(raw_attrs, Mapping):
        for attr_key, attr_spec in raw_attrs.items():
            key_str = str(attr_key)
            ok, resolved_val = _resolve_binding_value(
                attr_spec,
                field_label=f"attributes.{key_str}",
                fixture_vars=fixtures,
                runtime_context=context,
                unresolved=unresolved,
            )
            if ok:
                attributes[key_str] = resolved_val

    template_vars: dict[str, Any] = {
        str(k): v for k, v in fixtures.items() if v is not None
    }
    if entity_type:
        template_vars["entity.type"] = entity_type
    if entity_id is not None:
        template_vars["entity.id"] = entity_id
    for attr_key, attr_val in attributes.items():
        if attr_val is not None:
            template_vars[f"entity.{attr_key}"] = attr_val

    return ResolvedEntity(
        entity_type=entity_type,
        entity_id=entity_id,
        attributes=attributes,
        template_vars=template_vars,
        unresolved=tuple(unresolved),
    )


def _iter_template_vars(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(_VAR_RE.findall(obj))
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            found.update(_iter_template_vars(k))
            found.update(_iter_template_vars(v))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_iter_template_vars(item))
    return found


def _check_window_scope(
    window: Any,
    *,
    field: str,
    issues: list[EntityBindingIssue],
) -> None:
    if not isinstance(window, Mapping):
        return
    for key in ("step", "window_tag"):
        val = window.get(key)
        if isinstance(val, str) and val in _HOUSEKEEPING_STEPS:
            issues.append(EntityBindingIssue(
                code="entity_binding_invalid_window_scope",
                message=(
                    f"window.{key}={val!r} targets a post-body housekeeping mark; "
                    "interaction windows must scope to case body actions/operations"
                ),
                field=f"{field}.{key}",
            ))


def _check_url_regex(
    pattern: Any,
    *,
    field: str,
    issues: list[EntityBindingIssue],
) -> None:
    if not isinstance(pattern, str):
        return
    sanitized = _VAR_RE.sub("PLACEHOLDER", pattern)
    try:
        re.compile(sanitized)
    except re.error as exc:
        issues.append(EntityBindingIssue(
            code="entity_binding_invalid_url_regex",
            message=f"invalid url_regex {pattern!r}: {exc}",
            field=field,
        ))


def _check_template_refs(
    obj: Any,
    *,
    field: str,
    known_entity_vars: set[str],
    fixture_vars: Mapping[str, Any] | None,
    issues: list[EntityBindingIssue],
) -> None:
    for var_name in sorted(_iter_template_vars(obj)):
        if var_name.startswith("entity."):
            if var_name not in known_entity_vars:
                issues.append(EntityBindingIssue(
                    code="entity_binding_unknown_entity_var",
                    message=(
                        f"template variable ${{{var_name}}} is not declared on entity_binding "
                        f"(known: {sorted(known_entity_vars)})"
                    ),
                    field=field,
                ))
        elif fixture_vars is not None and (
            var_name not in fixture_vars or fixture_vars[var_name] is None
        ):
            issues.append(EntityBindingIssue(
                code="entity_binding_unresolved_fixture",
                message=f"template variable ${{{var_name}}} is missing from fixture_vars",
                field=field,
            ))


def _check_fixture_ref_value(
    value: Any,
    *,
    field: str,
    fixture_vars: Mapping[str, Any] | None,
    issues: list[EntityBindingIssue],
) -> None:
    if fixture_vars is None:
        return
    fixture_key = _is_ref_dict(value, "from_fixture")
    if fixture_key is not None and (
        fixture_key not in fixture_vars or fixture_vars[fixture_key] is None
    ):
        issues.append(EntityBindingIssue(
            code="entity_binding_unresolved_fixture",
            message=f"fixture reference {fixture_key!r} is missing from fixture_vars",
            field=field,
        ))


def _check_entity_ref_value(
    value: Any,
    *,
    field: str,
    known_entity_attrs: set[str],
    issues: list[EntityBindingIssue],
) -> None:
    entity_attr = _is_ref_dict(value, "from_entity")
    if entity_attr is not None and entity_attr not in known_entity_attrs:
        issues.append(EntityBindingIssue(
            code="entity_binding_unknown_entity_attr",
            message=(
                f"from_entity reference {entity_attr!r} is not declared on entity_binding "
                f"(known: {sorted(known_entity_attrs)})"
            ),
            field=field,
        ))


def _placeholder_for_op_param(param_spec: Mapping[str, Any] | None) -> Any:
    if not isinstance(param_spec, Mapping):
        return ""
    ptype = param_spec.get("type")
    if ptype == "enum":
        values = param_spec.get("values") or []
        return values[0] if values else ""
    if ptype == "int":
        return 0
    if ptype == "float":
        return 0.0
    if ptype == "bool":
        return False
    return ""


@dataclass(frozen=True)
class ReadbackStepEffect:
    """Kernel-level effect verdict for one readback step or Python executor."""

    irreversible: bool
    effect: str
    reason: str


@dataclass(frozen=True)
class ReadbackExecutorInfo:
    """Domain-supplied Python executor lookup result for ``readback.op``."""

    executor_fn: Any | None = None
    effect: ReadbackStepEffect | None = None


ReadbackExecutorInspector = Callable[[Any, str], ReadbackExecutorInfo]
ReadbackStepClassifier = Callable[[Mapping[str, Any]], ReadbackStepEffect]

_REGISTERED_EXECUTOR_INSPECTOR: ReadbackExecutorInspector | None = None
_REGISTERED_STEP_CLASSIFIER: ReadbackStepClassifier | None = None


def register_readback_runtime_provider(
    *,
    inspect_executor: ReadbackExecutorInspector | None = None,
    classify_step: ReadbackStepClassifier | None = None,
) -> None:
    """Register domain execution hooks for Python readback executors and step effects.

    ``autophone-domain`` installs these hooks when execution registries are loaded,
    keeping ``autophone-kernel`` strictly independent of ``autophone.execution.*``.
    """
    global _REGISTERED_EXECUTOR_INSPECTOR, _REGISTERED_STEP_CLASSIFIER
    if inspect_executor is not None:
        _REGISTERED_EXECUTOR_INSPECTOR = inspect_executor
    if classify_step is not None:
        _REGISTERED_STEP_CLASSIFIER = classify_step


def _default_classify_step(step: Mapping[str, Any]) -> ReadbackStepEffect:
    irreversible, effect, reason = static_step_effect(step)
    return ReadbackStepEffect(irreversible=irreversible, effect=effect, reason=reason)


def _validate_readback_operation(
    profile: Any,
    case: Mapping[str, Any],
    readback: Mapping[str, Any],
    issues: list[EntityBindingIssue],
    *,
    inspect_executor: ReadbackExecutorInspector | None = None,
    classify_step: ReadbackStepClassifier | None = None,
) -> None:
    op_name = str(readback.get("op") or "").strip()
    if not op_name:
        issues.append(EntityBindingIssue(
            code="entity_binding_readback_op_missing",
            message="readback.op must be a non-empty operation name",
            field="readback.op",
        ))
        return

    ops_library: dict[str, dict[str, Any]] = {}
    if callable(getattr(profile, "operations_path", None)):
        try:
            if profile.operations_path().is_file():
                ops_library = load_operations(profile)
        except (OperationError, yaml.YAMLError, OSError) as exc:
            issues.append(EntityBindingIssue(
                code="entity_binding_readback_op_invalid",
                message=f"failed to load operations library: {exc}",
                field="readback.op",
            ))
            return

    resolved_inspector = inspect_executor or _REGISTERED_EXECUTOR_INSPECTOR
    resolved_classifier = classify_step or _REGISTERED_STEP_CLASSIFIER or _default_classify_step

    executor_info = (
        resolved_inspector(profile, op_name)
        if resolved_inspector is not None
        else ReadbackExecutorInfo()
    )

    op_def = ops_library.get(op_name)
    executor_fn = executor_info.executor_fn
    if op_def is None and executor_fn is None:
        issues.append(EntityBindingIssue(
            code="entity_binding_readback_op_missing",
            message=f"readback operation {op_name!r} is not defined or registered",
            field="readback.op",
        ))
        return

    raw_with = readback.get("with")
    with_map = dict(raw_with) if isinstance(raw_with, Mapping) else {}

    if op_def is not None:
        if case.get("status") == "active" and op_def.get("status") not in ("active", None):
            issues.append(EntityBindingIssue(
                code="entity_binding_readback_op_inactive",
                message=(
                    f"readback operation {op_name!r} has status={op_def.get('status')!r}; "
                    "active cases require active readback operations"
                ),
                field="readback.op",
            ))
        nav = op_def.get("navigation")
        if isinstance(nav, Mapping):
            if nav.get("safety") == "mutating" or bool(nav.get("effects")):
                issues.append(EntityBindingIssue(
                    code="entity_binding_readback_op_irreversible",
                    message=(
                        f"readback operation {op_name!r} declares mutating navigation/effects "
                        "and must be read-only"
                    ),
                    field="readback.op",
                ))
        for idx, step in enumerate(op_def.get("steps") or []):
            if not isinstance(step, Mapping):
                continue
            decision = resolved_classifier(dict(step))
            if decision.irreversible:
                issues.append(EntityBindingIssue(
                    code="entity_binding_readback_op_irreversible",
                    message=(
                        f"readback operation {op_name!r} step[{idx}] is irreversible "
                        f"(effect={decision.effect}, reason={decision.reason})"
                    ),
                    field="readback.op",
                ))
                break
        op_params = {
            str(param.get("name")): param
            for param in (op_def.get("params") or [])
            if isinstance(param, Mapping) and param.get("name")
        }
        placeholder_args: dict[str, Any] = {}
        for param_name, val in with_map.items():
            if isinstance(val, Mapping):
                placeholder_args[str(param_name)] = _placeholder_for_op_param(
                    op_params.get(str(param_name))
                )
            else:
                placeholder_args[str(param_name)] = val
        try:
            _validate_args(dict(op_def), placeholder_args)
        except OperationError as exc:
            issues.append(EntityBindingIssue(
                code="entity_binding_readback_args_invalid",
                message=str(exc),
                field="readback.with",
            ))

    if executor_fn is not None:
        decision = executor_info.effect or ReadbackStepEffect(
            irreversible=True,
            effect="irreversible",
            reason="executor_missing_effect_declaration",
        )
        if decision.irreversible:
            if not any(i.code == "entity_binding_readback_op_irreversible" for i in issues):
                issues.append(EntityBindingIssue(
                    code="entity_binding_readback_op_irreversible",
                    message=(
                        f"readback operation {op_name!r} is irreversible "
                        f"(effect={decision.effect}, reason={decision.reason})"
                    ),
                    field="readback.op",
                ))
        if op_def is None:
            sig = inspect.signature(executor_fn)
            params = list(sig.parameters.values())[1:]
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            accepted = {
                p.name
                for p in params
                if p.kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            }
            required = {
                p.name
                for p in params
                if p.kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
                and p.default is inspect.Parameter.empty
            }
            unknown = sorted(set(with_map) - accepted) if not accepts_var_kw else []
            missing = sorted(required - set(with_map))
            if unknown:
                issues.append(EntityBindingIssue(
                    code="entity_binding_readback_args_invalid",
                    message=f"readback operation {op_name!r} rejects unknown arg(s): {unknown}",
                    field="readback.with",
                ))
            if missing:
                issues.append(EntityBindingIssue(
                    code="entity_binding_readback_args_invalid",
                    message=f"readback operation {op_name!r} missing required arg(s): {missing}",
                    field="readback.with",
                ))


def validate_case_entity_binding(
    profile: Any,
    case: Mapping[str, Any],
    *,
    fixture_vars: Mapping[str, Any] | None = None,
    inspect_executor: ReadbackExecutorInspector | None = None,
    classify_step: ReadbackStepClassifier | None = None,
) -> list[EntityBindingIssue]:
    """Validate a Case's ``entity_binding`` contract statically (fail-closed).

    Checks:
    - JSON Schema conformance against ``entity-binding.v1.json``;
    - policy consistency (``require_api`` requires ``api_window``; ``require_readback``
      requires ``readback``);
    - ``api_window`` assertion key/op/cross-op syntax, regex compilation, non-housekeeping
      window scope, and declared ``${entity.*}`` references;
    - ``readback`` ``{from_entity}`` references, predicate regex syntax, and (when
      ``profile`` is provided) that ``readback.op`` exists, is read-only / non-irreversible,
      and accepts ``readback.with`` arguments;
    - no unresolved ``{from_fixture}`` or ``${var}`` references when ``fixture_vars`` is
      provided.
    """
    if not case_has_entity_binding(case):
        return []

    binding = case.get("entity_binding")
    issues: list[EntityBindingIssue] = []

    validator = _validator(ENTITY_BINDING_SCHEMA_NAME)
    schema_errors = sorted(
        validator.iter_errors(binding),
        key=lambda err: (tuple(str(part) for part in err.absolute_path), err.message),
    )
    for err in schema_errors:
        field_path = ".".join(str(part) for part in err.absolute_path)
        issues.append(EntityBindingIssue(
            code="entity_binding_schema_invalid",
            message=err.message,
            field=field_path,
        ))

    if not isinstance(binding, Mapping):
        return issues

    policy = policy_from_entity_binding(binding)
    if policy["require_api"] and not binding.get("api_window"):
        issues.append(EntityBindingIssue(
            code="entity_binding_api_window_missing",
            message="joint_verdict.require_api=true requires api_window to be declared",
            field="joint_verdict.require_api",
        ))
    if policy["require_readback"] and not binding.get("readback"):
        issues.append(EntityBindingIssue(
            code="entity_binding_readback_missing",
            message="joint_verdict.require_readback=true requires readback to be declared",
            field="joint_verdict.require_readback",
        ))

    attrs = binding.get("attributes")
    attr_keys = {str(k) for k in attrs} if isinstance(attrs, Mapping) else set()
    known_entity_attrs = {"id", "type"} | attr_keys
    known_entity_vars = {f"entity.{attr}" for attr in known_entity_attrs}

    _check_fixture_ref_value(
        binding.get("entity_id"),
        field="entity_id",
        fixture_vars=fixture_vars,
        issues=issues,
    )
    if isinstance(attrs, Mapping):
        for attr_key, attr_val in attrs.items():
            _check_fixture_ref_value(
                attr_val,
                field=f"attributes.{attr_key}",
                fixture_vars=fixture_vars,
                issues=issues,
            )

    api_window = binding.get("api_window")
    if isinstance(api_window, Mapping):
        _check_window_scope(api_window.get("window"), field="api_window.window", issues=issues)
        _check_template_refs(
            api_window.get("window"),
            field="api_window.window",
            known_entity_vars=known_entity_vars,
            fixture_vars=fixture_vars,
            issues=issues,
        )
        for idx, rule in enumerate(api_window.get("expect") or []):
            if not isinstance(rule, Mapping):
                continue
            rule_field = f"api_window.expect[{idx}]"
            _check_window_scope(rule.get("window"), field=f"{rule_field}.window", issues=issues)
            _check_url_regex(rule.get("url_regex"), field=f"{rule_field}.url_regex", issues=issues)
            _check_template_refs(
                rule,
                field=rule_field,
                known_entity_vars=known_entity_vars,
                fixture_vars=fixture_vars,
                issues=issues,
            )
            assertions = rule.get("assert")
            if isinstance(assertions, Mapping):
                for key, spec in assertions.items():
                    key_str = str(key)
                    assert_field = f"{rule_field}.assert.{key_str}"
                    try:
                        static_check_assert_key(key_str)
                    except ValueError as exc:
                        issues.append(EntityBindingIssue(
                            code="entity_binding_invalid_assert_key",
                            message=str(exc),
                            field=assert_field,
                        ))
                        continue
                    op = _spec_op(spec)
                    if op not in ALLOWED_OPS:
                        issues.append(EntityBindingIssue(
                            code="entity_binding_invalid_assert_op",
                            message=f"unsupported api assertion op {op!r} (allowed: {sorted(ALLOWED_OPS)})",
                            field=assert_field,
                        ))
                        continue
                    if op in CROSS_ELEMENT_OPS and isinstance(spec, Mapping):
                        try:
                            _static_check_cross_op(assert_field, op, dict(spec))
                        except ValueError as exc:
                            issues.append(EntityBindingIssue(
                                code="entity_binding_invalid_assert_spec",
                                message=str(exc),
                                field=assert_field,
                            ))
        for idx, rule in enumerate(api_window.get("forbid") or []):
            if not isinstance(rule, Mapping):
                continue
            rule_field = f"api_window.forbid[{idx}]"
            _check_window_scope(rule.get("window"), field=f"{rule_field}.window", issues=issues)
            _check_url_regex(rule.get("url_regex"), field=f"{rule_field}.url_regex", issues=issues)
            _check_template_refs(
                rule,
                field=rule_field,
                known_entity_vars=known_entity_vars,
                fixture_vars=fixture_vars,
                issues=issues,
            )

    readback = binding.get("readback")
    if isinstance(readback, Mapping):
        with_map = readback.get("with")
        if isinstance(with_map, Mapping):
            for param, val in with_map.items():
                field_path = f"readback.with.{param}"
                _check_entity_ref_value(
                    val,
                    field=field_path,
                    known_entity_attrs=known_entity_attrs,
                    issues=issues,
                )
                _check_fixture_ref_value(
                    val,
                    field=field_path,
                    fixture_vars=fixture_vars,
                    issues=issues,
                )
                _check_template_refs(
                    val,
                    field=field_path,
                    known_entity_vars=known_entity_vars,
                    fixture_vars=fixture_vars,
                    issues=issues,
                )
        expect_ctx = readback.get("expect_context")
        if isinstance(expect_ctx, Mapping):
            for ctx_key, expected_spec in expect_ctx.items():
                field_path = f"readback.expect_context.{ctx_key}"
                _check_entity_ref_value(
                    expected_spec,
                    field=field_path,
                    known_entity_attrs=known_entity_attrs,
                    issues=issues,
                )
                _check_fixture_ref_value(
                    expected_spec,
                    field=field_path,
                    fixture_vars=fixture_vars,
                    issues=issues,
                )
                _check_template_refs(
                    expected_spec,
                    field=field_path,
                    known_entity_vars=known_entity_vars,
                    fixture_vars=fixture_vars,
                    issues=issues,
                )
                if isinstance(expected_spec, Mapping) and "op" in expected_spec:
                    pred_val = expected_spec.get("value")
                    _check_entity_ref_value(
                        pred_val,
                        field=f"{field_path}.value",
                        known_entity_attrs=known_entity_attrs,
                        issues=issues,
                    )
                    _check_fixture_ref_value(
                        pred_val,
                        field=f"{field_path}.value",
                        fixture_vars=fixture_vars,
                        issues=issues,
                    )
                    if expected_spec.get("op") == "regex" and isinstance(pred_val, str):
                        sanitized = _VAR_RE.sub("PLACEHOLDER", pred_val)
                        try:
                            re.compile(sanitized)
                        except re.error as exc:
                            issues.append(EntityBindingIssue(
                                code="entity_binding_invalid_readback_regex",
                                message=f"invalid readback regex {pred_val!r}: {exc}",
                                field=f"{field_path}.value",
                            ))
        if profile is not None:
            _validate_readback_operation(
                profile,
                case,
                readback,
                issues,
                inspect_executor=inspect_executor,
                classify_step=classify_step,
            )

    return issues


__all__ = [
    "ENTITY_BINDING_SCHEMA_NAME",
    "EntityBindingIssue",
    "ReadbackExecutorInfo",
    "ReadbackExecutorInspector",
    "ReadbackStepClassifier",
    "ReadbackStepEffect",
    "ResolvedEntity",
    "case_has_entity_binding",
    "case_requires_api_capture",
    "case_requires_entity_readback",
    "entity_binding_policy",
    "policy_from_entity_binding",
    "register_readback_runtime_provider",
    "resolve_case_entity",
    "validate_case_entity_binding",
]
