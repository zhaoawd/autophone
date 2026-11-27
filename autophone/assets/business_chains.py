"""Business Chain declarations (BL-038, Case Family proposal §4).

``apps/<app>/business_chains.yaml`` (schema ``autophone.business-chains/1``)
declares one App's classification hierarchy: Business Chain → Stage → Case
Family.  The declaration holds IDs, titles, business rule text and the Asset
Lifecycle status of each entry; it holds no steps, Operation, Screen, Fixture
or Oracle.  Case membership is not declared here: a Case references one Family
through its ``family_id``, and the Case Registry is the only source of that
relation.  The order of ``stages`` is the business narrative order, not an
execution order.

``load_business_chains`` returns a ``BusinessChainsDeclaration`` in one of
three states:

* ``absent`` (V7): the App has no declaration file.  Callers keep their
  previous behaviour; ``family_id`` is not checked against anything.
* ``loaded``: the file satisfies the Schema (V1 shape), every Chain, Stage and
  Family ID is unique within the App, ``app`` equals the App directory (V1),
  and the lifecycle hierarchy is consistent (V2).
* ``invalid`` (V8): any of the above fails.  ``errors`` lists every finding
  with its location (``path`` inside the document, plus ``line`` and
  ``column`` when the YAML text gives one).  Callers treat the whole
  declaration as unusable (fail-closed); no entry is resolvable.

The loader never infers a Family from Case text and never writes.

Case-side rules (proposal §4.5) live here too so every write entry and the
Case lint share one judgement: ``family_reference_issue`` (V3, V4, and V8 for
writes), ``lint_case_families`` (V3–V6, V9, plus the V7 notice) and
``declaration_change_issues`` (V5 for a declaration change).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, SequenceNode
from yaml.resolver import BaseResolver

from autophone.contracts import load_schema

BUSINESS_CHAINS_SCHEMA = "autophone.business-chains/1"
BUSINESS_CHAINS_FILE = "business_chains.yaml"
SCHEMA_NAME = "business-chains.v1.json"

#: Declaration states.
STATE_ABSENT = "absent"
STATE_LOADED = "loaded"
STATE_INVALID = "invalid"

#: Asset Lifecycle values; declaration entries use no other status.
ACTIVE = "active"
DRAFT = "draft"
RETIRED_STATUSES = frozenset({"deprecated", "archived"})

#: Entry kinds, also the YAML list keys that hold them.
KIND_CHAIN = "chain"
KIND_STAGE = "stage"
KIND_FAMILY = "family"


@dataclass(frozen=True)
class ChainEntry:
    id: str
    status: str
    title: str
    summary: str | None
    path: str
    stage_ids: tuple[str, ...]


@dataclass(frozen=True)
class StageEntry:
    id: str
    status: str
    title: str
    chain_id: str
    path: str
    family_ids: tuple[str, ...]


@dataclass(frozen=True)
class FamilyEntry:
    id: str
    status: str
    title: str
    rule: str | None
    stage_id: str
    chain_id: str
    path: str


@dataclass(frozen=True)
class BusinessChainsDeclaration:
    """One App's parsed declaration; entries are empty unless ``state`` is ``loaded``."""

    app: str
    path: Path | None
    state: str
    sha256: str | None = None
    chains: tuple[ChainEntry, ...] = ()
    stages: dict[str, StageEntry] = field(default_factory=dict)
    families: dict[str, FamilyEntry] = field(default_factory=dict)
    errors: tuple[dict[str, Any], ...] = ()

    @property
    def loaded(self) -> bool:
        return self.state == STATE_LOADED

    def family(self, family_id: str) -> FamilyEntry | None:
        return self.families.get(family_id) if self.loaded else None

    def stage(self, stage_id: str) -> StageEntry | None:
        return self.stages.get(stage_id) if self.loaded else None

    def chain(self, chain_id: str) -> ChainEntry | None:
        if not self.loaded:
            return None
        return next((chain for chain in self.chains if chain.id == chain_id), None)


# --------------------------------------------------------------------------- #
# YAML reading with duplicate-key rejection and node positions
# --------------------------------------------------------------------------- #


class _DuplicateKeyError(ConstructorError):
    def __init__(self, key: Any, first_mark: Any, duplicate_mark: Any) -> None:
        super().__init__(
            "while constructing a mapping", first_mark,
            f"found duplicate key {key!r}", duplicate_mark,
        )
        self.key = key
        self.first_mark = first_mark
        self.duplicate_mark = duplicate_mark


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a key written twice in the same mapping."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False) -> dict:
    seen: dict[Any, Any] = {}
    for key_node, _value in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=False)
        if isinstance(key, Hashable):
            if key in seen:
                raise _DuplicateKeyError(key, seen[key], key_node.start_mark)
            seen[key] = key_node.start_mark
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _node_at(root: Node | None, parts: list[Any]) -> Node | None:
    """Deepest node reachable along ``parts``; the parent when a key is missing."""
    node = root
    for part in parts:
        if isinstance(node, MappingNode):
            child = next(
                (value for key, value in node.value if getattr(key, "value", None) == str(part)),
                None,
            )
        elif isinstance(node, SequenceNode) and isinstance(part, int) and 0 <= part < len(node.value):
            child = node.value[part]
        else:
            child = None
        if child is None:
            break
        node = child
    return node


def _pointer(parts: list[Any]) -> str:
    return "/".join(str(part) for part in parts) if parts else "<root>"


def _finding(code: str, parts: list[Any], message: str, root: Node | None, **details: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "severity": "error",
        "code": code,
        "path": _pointer(parts),
        "message": message,
    }
    node = _node_at(root, parts)
    if node is not None:
        finding["line"] = int(node.start_mark.line) + 1
        finding["column"] = int(node.start_mark.column) + 1
    finding.update(details)
    return finding


def _schema_code(error: ValidationError) -> str:
    leaf = str(error.absolute_path[-1]) if error.absolute_path else ""
    if error.validator == "required":
        return "missing_required_field"
    if error.validator == "additionalProperties":
        return "unknown_field"
    if leaf == "schema" and error.validator == "const":
        return "invalid_schema_id"
    if leaf == "status" and error.validator == "enum":
        return "invalid_status"
    if leaf == "id" and error.validator == "pattern":
        return "invalid_entry_id"
    return "invalid_field_shape"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def business_chains_path(profile: Any) -> Path | None:
    """Declaration file of one App Profile; ``None`` when the profile has no directory."""
    profile_dir = getattr(profile, "profile_dir", None)
    if profile_dir is None:
        return None
    return Path(profile_dir) / BUSINESS_CHAINS_FILE


def load_business_chains(profile: Any) -> BusinessChainsDeclaration:
    """Load ``apps/<app>/business_chains.yaml`` for one App Profile, fail-closed."""
    app = str(getattr(profile, "app", "") or "")
    path = business_chains_path(profile)
    if path is None:
        return BusinessChainsDeclaration(app=app, path=None, state=STATE_ABSENT)
    return load_business_chains_file(path, app=app)


def load_business_chains_file(path: Path, *, app: str) -> BusinessChainsDeclaration:
    """Load one declaration file; ``app`` is the App name of its directory."""
    if not path.is_file():
        if path.exists():
            return _invalid(app, path, [_finding(
                "declaration_not_a_file", [], f"{path} 不是文件", None)])
        return BusinessChainsDeclaration(app=app, path=path, state=STATE_ABSENT)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _invalid(app, path, [_finding(
            "file_read_failed", [], f"读取 {path} 失败：{exc}", None)])
    return parse_business_chains_text(text, app=app, path=path)


def parse_business_chains_text(
    text: str, *, app: str, path: Path | None = None,
) -> BusinessChainsDeclaration:
    """Validate declaration text (V1, V2) and build the entry index."""
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        line = int(exc.duplicate_mark.line) + 1
        column = int(exc.duplicate_mark.column) + 1
        return _invalid(app, path, [{
            "severity": "error",
            "code": "duplicate_yaml_key",
            "path": "<root>",
            "message": (
                f"YAML mapping key {exc.key!r} 重复；首次位于第 {int(exc.first_mark.line) + 1} 行，"
                f"重复位于第 {line} 行第 {column} 列"
            ),
            "line": line,
            "column": column,
            "key": str(exc.key),
        }])
    except yaml.YAMLError as exc:
        finding: dict[str, Any] = {
            "severity": "error", "code": "yaml_parse_failed", "path": "<root>",
            "message": f"解析声明失败：{exc}",
        }
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            finding["line"] = int(mark.line) + 1
            finding["column"] = int(mark.column) + 1
        return _invalid(app, path, [finding])
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:  # pragma: no cover - load succeeded on the same text
        root = None
    return validate_business_chains_document(document, app=app, path=path, root=root)


def validate_business_chains_document(
    document: Any,
    *,
    app: str,
    path: Path | None = None,
    root: Node | None = None,
) -> BusinessChainsDeclaration:
    """Apply V1 (Schema, unique IDs, App name) and V2 (lifecycle hierarchy)."""
    validator = Draft202012Validator(load_schema(SCHEMA_NAME))
    errors = [
        _finding(_schema_code(error), list(error.absolute_path), error.message, root)
        for error in sorted(
            validator.iter_errors(document),
            key=lambda error: ([str(part) for part in error.absolute_path], error.message),
        )
    ]
    if errors:
        # Shape is not trustworthy; ID and hierarchy rules need the Schema to hold.
        return _invalid(app, path, errors)

    if str(document["app"]) != app:
        errors.append(_finding(
            "app_mismatch", ["app"],
            f"app={document['app']!r} 与所在 App 目录 {app!r} 不一致", root,
            expected=app, actual=document["app"],
        ))

    chains: list[ChainEntry] = []
    stages: dict[str, StageEntry] = {}
    families: dict[str, FamilyEntry] = {}
    first_seen: dict[str, tuple[str, list[Any]]] = {}

    def claim(entry_id: str, kind: str, parts: list[Any]) -> None:
        if entry_id in first_seen:
            first_kind, first_parts = first_seen[entry_id]
            errors.append(_finding(
                "duplicate_entry_id", [*parts, "id"],
                f"ID {entry_id!r} 重复：首次为 {first_kind}（{_pointer(first_parts)}），"
                f"再次为 {kind}（{_pointer(parts)}）；Chain、Stage、Family 的 ID 在 App 内共同唯一",
                root, entry_id=entry_id, first_path=_pointer(first_parts),
            ))
            return
        first_seen[entry_id] = (kind, parts)

    for chain_index, chain in enumerate(document["chains"]):
        chain_parts: list[Any] = ["chains", chain_index]
        claim(chain["id"], KIND_CHAIN, chain_parts)
        stage_ids: list[str] = []
        for stage_index, stage in enumerate(chain["stages"]):
            stage_parts = [*chain_parts, "stages", stage_index]
            claim(stage["id"], KIND_STAGE, stage_parts)
            stage_ids.append(stage["id"])
            family_ids: list[str] = []
            for family_index, family in enumerate(stage["families"]):
                family_parts = [*stage_parts, "families", family_index]
                claim(family["id"], KIND_FAMILY, family_parts)
                family_ids.append(family["id"])
                families.setdefault(family["id"], FamilyEntry(
                    id=family["id"], status=family["status"], title=family["title"],
                    rule=family.get("rule"), stage_id=stage["id"], chain_id=chain["id"],
                    path=_pointer(family_parts),
                ))
                _check_hierarchy(errors, root, family_parts, family, stage, chain)
            stages.setdefault(stage["id"], StageEntry(
                id=stage["id"], status=stage["status"], title=stage["title"],
                chain_id=chain["id"], path=_pointer(stage_parts), family_ids=tuple(family_ids),
            ))
            if chain["status"] in RETIRED_STATUSES and stage["status"] == ACTIVE:
                errors.append(_finding(
                    "active_entry_under_retired_parent", [*stage_parts, "status"],
                    f"Stage {stage['id']} 为 active，但上级 Chain {chain['id']} 为 {chain['status']}",
                    root, entry_id=stage["id"], parent_id=chain["id"],
                ))
        chains.append(ChainEntry(
            id=chain["id"], status=chain["status"], title=chain["title"],
            summary=chain.get("summary"), path=_pointer(chain_parts), stage_ids=tuple(stage_ids),
        ))

    if errors:
        return _invalid(app, path, errors)
    return BusinessChainsDeclaration(
        app=app,
        path=path,
        state=STATE_LOADED,
        sha256=declaration_sha256(document),
        chains=tuple(chains),
        stages=stages,
        families=families,
    )


def _check_hierarchy(
    errors: list[dict[str, Any]],
    root: Node | None,
    family_parts: list[Any],
    family: dict[str, Any],
    stage: dict[str, Any],
    chain: dict[str, Any],
) -> None:
    """V2 for one Family: an active Family needs an active Stage and Chain."""
    if family["status"] != ACTIVE:
        return
    for kind, parent in ((KIND_STAGE, stage), (KIND_CHAIN, chain)):
        if parent["status"] == ACTIVE:
            continue
        code = (
            "active_entry_under_retired_parent"
            if parent["status"] in RETIRED_STATUSES
            else "active_family_under_inactive_parent"
        )
        errors.append(_finding(
            code, [*family_parts, "status"],
            f"Family {family['id']} 为 active，但上级 {kind.capitalize()} {parent['id']} 为 {parent['status']}",
            root, entry_id=family["id"], parent_id=parent["id"],
        ))


def family_status_allowed(case_status: Any, family_status: str) -> bool:
    """V4: whether a Case in ``case_status`` may reference a Family in ``family_status``.

    An active Case needs an active Family; a draft Case may reference a draft
    or active Family.  Deprecated and archived Cases are not constrained.
    """
    if case_status == ACTIVE:
        return family_status == ACTIVE
    if case_status == DRAFT:
        return family_status in (DRAFT, ACTIVE)
    return True


# --------------------------------------------------------------------------- #
# Case references (V3–V6, V9)
# --------------------------------------------------------------------------- #

#: Write rejection codes (HTTP 422 ``detail.code``).
FAMILY_REFERENCE_UNDECLARED = "family_reference_undeclared"  # V3
FAMILY_REFERENCE_INACTIVE = "family_reference_inactive"      # V4
FAMILY_DECLARATION_INVALID = "family_declaration_invalid"    # V8

#: Case lifecycles whose ``family_id`` must resolve (V3, V4).
_MAINTAINED_CASE_STATUSES = frozenset({DRAFT, ACTIVE})
_LINT_CASE_ID_LIMIT = 20


def case_family_id(case: dict[str, Any]) -> str:
    """The Case's ``family_id`` with surrounding blanks removed; ``""`` when empty."""
    value = case.get("family_id")
    return value.strip() if isinstance(value, str) else ""


def family_reference_issue(
    declaration: BusinessChainsDeclaration,
    case: dict[str, Any],
) -> dict[str, Any] | None:
    """V3/V4/V8 for one prospective Case state; ``None`` when the write may proceed.

    * No declaration (V7): nothing is checked.
    * Empty ``family_id`` or a Case that is neither draft nor active: nothing
      is checked (V9 reports empty Family separately; V6 covers retired Cases).
    * Invalid declaration (V8): a non-empty ``family_id`` cannot be resolved,
      so the write is rejected (fail-closed).
    * V3: the ID must name a declared Family (not a Chain or Stage).
    * V4: an active Case needs an active Family; a draft Case needs a draft
      or active Family.
    """
    family_id = case_family_id(case)
    status = case.get("status")
    if declaration.state == STATE_ABSENT or not family_id or status not in _MAINTAINED_CASE_STATUSES:
        return None
    detail: dict[str, Any] = {
        "case_id": case.get("id"),
        "case_status": status,
        "family_id": family_id,
        "declaration_path": str(declaration.path) if declaration.path else None,
    }
    if declaration.state != STATE_LOADED:
        return {
            "code": FAMILY_DECLARATION_INVALID,
            "message": (
                f"business_chains.yaml 不合法（{len(declaration.errors)} 处错误），"
                f"不能写入或激活 family_id={family_id!r}；先修正声明文件"
            ),
            **detail,
            "declaration_errors": list(declaration.errors),
        }
    family = declaration.family(family_id)
    if family is None:
        return {
            "code": FAMILY_REFERENCE_UNDECLARED,
            "message": f"family_id={family_id!r} 未在 business_chains.yaml 中声明为 Case Family",
            **detail,
        }
    if not family_status_allowed(status, family.status):
        allowed = "active" if status == ACTIVE else "draft 或 active"
        return {
            "code": FAMILY_REFERENCE_INACTIVE,
            "message": (
                f"{status} Case 只能引用 {allowed} Family；"
                f"family_id={family_id!r} 当前为 {family.status}"
            ),
            **detail,
            "family_status": family.status,
        }
    return None


def lint_case_families(
    cases: list[Any],
    declaration: BusinessChainsDeclaration,
) -> dict[str, list[dict[str, Any]]]:
    """Case lint rules V3–V9 for one App's Case Registry rows.

    Errors: declaration findings (V1, V2, V8), V3, V4 and V5.  Warnings: V6,
    V7 and V9.  Without a declaration only V7 applies, and only when a draft or
    active Case carries a ``family_id`` that therefore goes unchecked.
    ``cases`` are raw Registry rows in file order.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rows = [(index, case) for index, case in enumerate(cases) if isinstance(case, dict)]
    declaration_path = str(declaration.path) if declaration.path else None

    if declaration.state == STATE_ABSENT:
        unchecked = [
            str(case.get("id")) for _, case in rows
            if case_family_id(case) and case.get("status") in _MAINTAINED_CASE_STATUSES
        ]
        if unchecked:
            warnings.append(_lint_finding(
                "warning", "family_declaration_absent", "cases",
                f"{len(unchecked)} 条 draft/active Case 填写了 family_id，但 App 没有 "
                "business_chains.yaml，引用未校验",
                case_ids=unchecked,
            ))
        return {"errors": errors, "warnings": warnings}

    if declaration.state != STATE_LOADED:
        for finding in declaration.errors:
            errors.append({
                **finding,
                "path": f"business_chains/{finding['path']}",
                "source": "business_chains",
                "declaration_path": declaration_path,
            })
        return {"errors": errors, "warnings": warnings}

    retired_undeclared: list[str] = []
    missing_family: list[str] = []
    for index, case in rows:
        status = case.get("status")
        case_id = case.get("id")
        family_id = case_family_id(case)
        if not family_id:
            if status == ACTIVE:
                missing_family.append(str(case_id))
            continue
        family = declaration.family(family_id)
        path = f"cases/{index}/family_id"
        if status not in _MAINTAINED_CASE_STATUSES:
            if family is None:
                retired_undeclared.append(str(case_id))  # V6
            continue
        if family is None:
            errors.append(_lint_finding(
                "error", FAMILY_REFERENCE_UNDECLARED, path,
                f"{status} Case {case_id} 的 family_id={family_id!r} 未在 business_chains.yaml 中声明",
                case_id=case_id, family_id=family_id,
            ))
        elif status == ACTIVE and family.status in RETIRED_STATUSES:
            # V5: a Family referenced by an active Case must not be deprecated or archived.
            errors.append(_lint_finding(
                "error", "family_retired_while_referenced", path,
                f"Family {family_id} 为 {family.status}，但仍被 active Case {case_id} 引用",
                case_id=case_id, family_id=family_id, family_status=family.status,
                family_path=family.path,
            ))
        elif not family_status_allowed(status, family.status):
            errors.append(_lint_finding(
                "error", FAMILY_REFERENCE_INACTIVE, path,
                f"{status} Case {case_id} 不能引用 {family.status} Family {family_id}",
                case_id=case_id, family_id=family_id, family_status=family.status,
            ))
    if retired_undeclared:
        warnings.append(_lint_finding(
            "warning", "retired_case_family_undeclared", "cases",
            f"{len(retired_undeclared)} 条 deprecated/archived Case 的 family_id 未在 "
            "business_chains.yaml 中声明",
            case_ids=retired_undeclared,
        ))
    if missing_family:
        warnings.append(_lint_finding(
            "warning", "active_cases_missing_family_id", "cases",
            f"{len(missing_family)} 条 active Case 的 family_id 为空",
            case_ids=missing_family,
        ))
    return {"errors": errors, "warnings": warnings}


def declaration_change_issues(
    previous: BusinessChainsDeclaration,
    current: BusinessChainsDeclaration,
    cases: list[Any],
) -> list[dict[str, Any]]:
    """V5 for a declaration change: a referenced Family must not disappear or retire.

    A Family referenced by any Case (any status) must still be declared in
    ``current``; a Family referenced by an active Case must not become
    deprecated or archived.  Removing a Chain or Stage removes its Families, so
    it is caught through them.  Both declarations must be ``loaded``.
    """
    if not (previous.loaded and current.loaded):
        raise ValueError("declaration_change_issues needs two loaded declarations")
    issues: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        family_id = case_family_id(case)
        if not family_id or previous.family(family_id) is None:
            continue
        after = current.family(family_id)
        if after is None:
            issues.append({
                "code": "family_entry_removed_while_referenced",
                "message": f"Family {family_id} 被 Case {case.get('id')} 引用，不能删除，只能废弃",
                "case_id": case.get("id"), "family_id": family_id,
            })
        elif case.get("status") == ACTIVE and after.status in RETIRED_STATUSES:
            issues.append({
                "code": "family_retired_while_referenced",
                "message": f"Family {family_id} 被 active Case {case.get('id')} 引用，不能改为 {after.status}",
                "case_id": case.get("id"), "family_id": family_id, "family_status": after.status,
            })
    return issues


def _lint_finding(severity: str, code: str, path: str, message: str, **details: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {"severity": severity, "code": code, "path": path, "message": message}
    case_ids = details.pop("case_ids", None)
    if case_ids is not None:
        finding.update({
            "count": len(case_ids),
            "case_ids": case_ids[:_LINT_CASE_ID_LIMIT],
            "truncated": len(case_ids) > _LINT_CASE_ID_LIMIT,
        })
    finding.update(details)
    return finding


def declaration_sha256(document: Any) -> str:
    """SHA-256 (hex) of the normalized declaration (sorted-key compact JSON)."""
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(app: str, path: Path | None, errors: list[dict[str, Any]]) -> BusinessChainsDeclaration:
    return BusinessChainsDeclaration(app=app, path=path, state=STATE_INVALID, errors=tuple(errors))


__all__ = [
    "ACTIVE",
    "BUSINESS_CHAINS_FILE",
    "BUSINESS_CHAINS_SCHEMA",
    "BusinessChainsDeclaration",
    "ChainEntry",
    "FAMILY_DECLARATION_INVALID",
    "FAMILY_REFERENCE_INACTIVE",
    "FAMILY_REFERENCE_UNDECLARED",
    "FamilyEntry",
    "SCHEMA_NAME",
    "STATE_ABSENT",
    "STATE_INVALID",
    "STATE_LOADED",
    "StageEntry",
    "business_chains_path",
    "case_family_id",
    "declaration_change_issues",
    "declaration_sha256",
    "family_reference_issue",
    "family_status_allowed",
    "lint_case_families",
    "load_business_chains",
    "load_business_chains_file",
    "parse_business_chains_text",
    "validate_business_chains_document",
]
