"""Case Registry v2 的结构与跨记录一致性校验。

JSON Schema 负责单文档/单记录形状；本模块补充 JSON Schema 不便表达的整库不变量：
``meta.total``、``meta.next_seq``、稳定 id 与 ``raw.external_id`` 唯一性。历史兼容
形态只产生 warning，不与阻断性 error 混在一起。
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from autophone.assets.case_ids import (
    id_reservations,
    reservation_issues,
    retired_case_ids,
)
from autophone.assets.entity_binding import (
    case_has_entity_binding,
    validate_case_entity_binding,
)
from autophone.contracts import _validator

SCHEMA_NAME = "case-registry.v2.json"
_CASE_ID_RE = re.compile(r"^R([1-9][0-9]*)$")
_WARNING_CASE_ID_LIMIT = 20


class CaseRegistryError(ValueError):
    """Case Registry 存在一个或多个阻断性错误。"""


class _DuplicateYamlKeyError(ConstructorError):
    """YAML 同一 mapping 内出现重复键，保留首次与重复位置。"""

    def __init__(self, key: Any, first_mark: Any, duplicate_mark: Any) -> None:
        super().__init__(
            "while constructing a mapping",
            first_mark,
            f"found duplicate key {key!r}",
            duplicate_mark,
        )
        self.key = key
        self.first_mark = first_mark
        self.duplicate_mark = duplicate_mark


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader 兼容形状，但禁止同一 mapping 内的显式重复键。"""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )
    seen: dict[Any, Any] = {}
    for key_node, _value_node in node.value:
        # YAML merge key 由 SafeLoader 按标准语义展开；这里只拒绝当前
        # mapping 中重复写出的显式键，不把合法 merge override 误报为重复。
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, Hashable):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            )
        if key in seen:
            raise _DuplicateYamlKeyError(key, seen[key], key_node.start_mark)
        seen[key] = key_node.start_mark
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _path(parts: Any) -> str:
    values = list(parts)
    return "/".join(str(part) for part in values) if values else "<root>"


def _schema_error_code(error: ValidationError) -> str:
    path = list(error.absolute_path)
    leaf = str(path[-1]) if path else ""
    if error.validator == "required":
        return "missing_required_field"
    if error.validator == "additionalProperties":
        return "unknown_field"
    if leaf == "status" and error.validator == "enum":
        return "invalid_status"
    if leaf == "id" and error.validator == "pattern":
        return "invalid_case_id"
    return "invalid_field_shape"


def _finding(
    severity: str,
    code: str,
    path: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }
    result.update(details)
    return result


def _has_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and any(_has_nonempty_text(item) for item in value)


def lint_case_registry_document(
    document: Any,
    *,
    source: str = "<memory>",
    profile: Any = None,
    fixture_vars: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """校验已解析的 Case Registry，返回稳定的 errors/warnings 结构。

    ``errors`` 可作为 CI 阻断条件；``warnings`` 仅报告历史兼容债务。函数不修改传入文档。
    """
    validator = _validator(SCHEMA_NAME)
    schema_errors = sorted(
        validator.iter_errors(document),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    errors = [
        _finding(
            "error",
            _schema_error_code(error),
            _path(error.absolute_path),
            error.message,
        )
        for error in schema_errors
    ]
    warnings: list[dict[str, Any]] = []

    # 结构不成立时仍返回全部 schema findings；以下整库规则只在可安全遍历时执行。
    if not isinstance(document, dict):
        return _report(source, errors, warnings)
    meta = document.get("meta")
    cases = document.get("cases")
    if not isinstance(meta, dict) or not isinstance(cases, list):
        return _report(source, errors, warnings)

    total = meta.get("total")
    if isinstance(total, int) and not isinstance(total, bool) and total != len(cases):
        errors.append(_finding(
            "error",
            "meta_total_mismatch",
            "meta/total",
            f"meta.total={total}，cases 实际记录数={len(cases)}",
            expected=len(cases),
            actual=total,
        ))

    id_positions: dict[str, list[int]] = defaultdict(list)
    external_positions: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    source_ref_positions: dict[str, list[tuple[int, str]]] = defaultdict(list)
    max_id_seq = 0
    typed_ir_case_ids: list[str] = []
    legacy_automation_case_ids: list[str] = []
    missing_quality_case_ids: dict[str, list[str]] = {
        "case_type": [],
        "test_point": [],
        "owner": [],
        "draft_expects": [],
    }

    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = case.get("id")
        status = str(case.get("status") or "")
        if isinstance(case_id, str):
            id_positions[case_id].append(index)
            match = _CASE_ID_RE.fullmatch(case_id)
            if match:
                max_id_seq = max(max_id_seq, int(match.group(1)))

        raw = case.get("raw")
        if isinstance(raw, dict):
            external_id = raw.get("external_id")
            if isinstance(external_id, str) and external_id.strip():
                external_positions[external_id.strip()].append(
                    (index, str(case_id or ""), status)
                )
            source_ref = raw.get("source_ref")
            if isinstance(source_ref, str) and source_ref.strip():
                source_ref_positions[source_ref.strip()].append((index, str(case_id or "")))

        steps = case.get("steps")
        if isinstance(steps, list) and any(isinstance(step, dict) for step in steps):
            typed_ir_case_ids.append(str(case_id or f"index:{index}"))
        if case.get("automation") == "phase4_prefix_api":
            legacy_automation_case_ids.append(str(case_id or f"index:{index}"))

        display_case_id = str(case_id or f"index:{index}")
        if status in {"active", "draft"}:
            for field in ("case_type", "test_point", "owner"):
                if not _has_nonempty_text(case.get(field)):
                    missing_quality_case_ids[field].append(display_case_id)
            if status == "draft" and not _has_nonempty_string_list(case.get("expects")):
                missing_quality_case_ids["draft_expects"].append(display_case_id)

        if status == "active":
            expects = case.get("expects")
            if not _has_nonempty_string_list(expects):
                errors.append(_finding(
                    "error",
                    "active_case_missing_expects",
                    f"cases/{index}/expects",
                    f"active Case {case_id or index} 必须声明非空 expects",
                    case_id=case_id,
                ))
            automation = case.get("automation")
            if not isinstance(automation, str) or not automation:
                errors.append(_finding(
                    "error",
                    "active_case_missing_automation",
                    f"cases/{index}/automation",
                    f"active Case {case_id or index} 必须声明 automation",
                    case_id=case_id,
                ))

        if case_has_entity_binding(case):
            case_fixtures: Mapping[str, Any] | None = None
            if isinstance(fixture_vars, Mapping):
                per_case = fixture_vars.get(str(case_id or ""))
                case_fixtures = per_case if isinstance(per_case, Mapping) else fixture_vars
            for eb_issue in validate_case_entity_binding(
                profile,
                case,
                fixture_vars=case_fixtures,
            ):
                if eb_issue.code == "entity_binding_schema_invalid":
                    continue
                issue_path = (
                    f"cases/{index}/entity_binding/{eb_issue.field}"
                    if eb_issue.field
                    else f"cases/{index}/entity_binding"
                )
                errors.append(_finding(
                    "error",
                    eb_issue.code,
                    issue_path,
                    eb_issue.message,
                    case_id=case_id,
                ))

    for case_id, positions in sorted(id_positions.items()):
        if len(positions) > 1:
            errors.append(_finding(
                "error",
                "duplicate_case_id",
                "cases",
                f"Case id {case_id} 重复 {len(positions)} 次",
                case_id=case_id,
                indexes=positions,
            ))

    historical_external_duplicates: list[dict[str, Any]] = []
    for external_id, positions in sorted(external_positions.items()):
        maintained = [position for position in positions if position[2] in {"active", "draft"}]
        if len(maintained) > 1:
            errors.append(_finding(
                "error",
                "duplicate_external_id",
                "cases",
                f"raw.external_id {external_id!r} 被 {len(maintained)} 条 active/draft Case 共用",
                external_id=external_id,
                case_ids=[case_id for _, case_id, _ in maintained],
                indexes=[index for index, _, _ in maintained],
            ))
        elif len(positions) > 1:
            historical_external_duplicates.append({
                "external_id": external_id,
                "case_ids": [case_id for _, case_id, _ in positions],
                "statuses": [status for _, _, status in positions],
                "indexes": [index for index, _, _ in positions],
            })

    try:
        reserved_ids = id_reservations(document)
    except ValueError:
        reserved_ids = {}
    try:
        retired_ids = retired_case_ids(document)
    except ValueError:
        retired_ids = set()
    max_reserved_seq = max(
        (int(match.group(1)) for case_id in reserved_ids
         if (match := _CASE_ID_RE.fullmatch(case_id))),
        default=0,
    )
    max_retired_seq = max(
        (int(match.group(1)) for case_id in retired_ids
         if (match := _CASE_ID_RE.fullmatch(case_id))),
        default=0,
    )
    max_allocated_seq = max(max_id_seq, max_reserved_seq, max_retired_seq)
    next_seq = meta.get("next_seq")
    if (
        isinstance(next_seq, int)
        and not isinstance(next_seq, bool)
        and max_allocated_seq
        and next_seq <= max_allocated_seq
    ):
        errors.append(_finding(
            "error",
            "next_seq_not_ahead",
            "meta/next_seq",
            f"meta.next_seq={next_seq} 必须大于现存、保留或退休的最大 Case 序号 {max_allocated_seq}",
            actual=next_seq,
            minimum=max_allocated_seq + 1,
        ))

    for issue in reservation_issues(document):
        details = {
            key: value
            for key, value in issue.items()
            if key not in {"code", "path", "message"}
        }
        errors.append(_finding(
            "error",
            issue["code"],
            issue["path"],
            issue["message"],
            **details,
        ))

    if typed_ir_case_ids:
        warnings.append(_finding(
            "warning",
            "legacy_typed_ir_case_steps",
            "cases",
            f"{len(typed_ir_case_ids)} 条 Case 的 steps 含 typed IR object；Schema 仅作历史兼容，新执行步骤应进入 Recipe/Operation",
            count=len(typed_ir_case_ids),
            case_ids=typed_ir_case_ids[:_WARNING_CASE_ID_LIMIT],
            truncated=len(typed_ir_case_ids) > _WARNING_CASE_ID_LIMIT,
        ))
    if historical_external_duplicates:
        visible = historical_external_duplicates[:_WARNING_CASE_ID_LIMIT]
        warnings.append(_finding(
            "warning",
            "historical_duplicate_external_ids",
            "cases",
            (
                f"{len(historical_external_duplicates)} 组 raw.external_id 因包含 "
                "deprecated/archived 历史记录而重复；需复核重导关系"
            ),
            count=len(historical_external_duplicates),
            external_ids=[item["external_id"] for item in visible],
            duplicates=visible,
            truncated=len(historical_external_duplicates) > _WARNING_CASE_ID_LIMIT,
        ))
    for source_ref, positions in sorted(source_ref_positions.items()):
        if len(positions) > 1:
            warnings.append(_finding(
                "warning",
                "duplicate_source_ref",
                "cases",
                f"raw.source_ref {source_ref!r} 被 {len(positions)} 条 Case 共用；需复核是同一来源拆分还是重复候选",
                source_ref=source_ref,
                case_ids=[case_id for _, case_id in positions],
                indexes=[index for index, _ in positions],
            ))
    quality_warning_specs = (
        (
            "case_type",
            "maintained_cases_missing_case_type",
            "active/draft Case 缺少非空 case_type",
        ),
        (
            "test_point",
            "maintained_cases_missing_test_point",
            "active/draft Case 缺少非空 test_point",
        ),
        (
            "owner",
            "maintained_cases_missing_owner",
            "active/draft Case 缺少非空 owner",
        ),
        (
            "draft_expects",
            "draft_cases_missing_expects",
            "draft Case 缺少非空 expects",
        ),
    )
    for key, code, description in quality_warning_specs:
        case_ids = missing_quality_case_ids[key]
        if case_ids:
            warnings.append(_finding(
                "warning",
                code,
                "cases",
                f"{len(case_ids)} 条 {description}",
                count=len(case_ids),
                case_ids=case_ids[:_WARNING_CASE_ID_LIMIT],
                truncated=len(case_ids) > _WARNING_CASE_ID_LIMIT,
            ))
    if legacy_automation_case_ids:
        warnings.append(_finding(
            "warning",
            "legacy_automation_tag",
            "cases",
            "phase4_prefix_api 是既有专项兼容标记，不属于可扩展的标准 automation 分类",
            count=len(legacy_automation_case_ids),
            case_ids=legacy_automation_case_ids,
        ))

    return _report(source, errors, warnings)


def _report(
    source: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": source,
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
    }


def lint_case_registry_file(
    path: Path,
    *,
    profile: Any = None,
    fixture_vars: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """读取并 lint 一个 YAML Case Registry；解析/读取失败也归入结构化 error。"""
    source = str(path)
    try:
        document = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except FileNotFoundError:
        return _report(source, [
            _finding("error", "file_not_found", "<root>", f"Case Registry 不存在：{path}")
        ], [])
    except OSError as exc:
        return _report(source, [
            _finding("error", "file_read_failed", "<root>", f"读取 Case Registry 失败：{exc}")
        ], [])
    except _DuplicateYamlKeyError as exc:
        first_line = int(exc.first_mark.line) + 1
        first_column = int(exc.first_mark.column) + 1
        duplicate_line = int(exc.duplicate_mark.line) + 1
        duplicate_column = int(exc.duplicate_mark.column) + 1
        return _report(source, [
            _finding(
                "error",
                "duplicate_yaml_key",
                f"line/{duplicate_line}/column/{duplicate_column}",
                (
                    f"YAML mapping key {exc.key!r} 重复；"
                    f"首次位于第 {first_line} 行第 {first_column} 列，"
                    f"重复位于第 {duplicate_line} 行第 {duplicate_column} 列"
                ),
                key=str(exc.key),
                first_line=first_line,
                first_column=first_column,
                line=duplicate_line,
                column=duplicate_column,
            )
        ], [])
    except yaml.YAMLError as exc:
        return _report(source, [
            _finding("error", "yaml_parse_failed", "<root>", f"解析 Case Registry 失败：{exc}")
        ], [])
    return lint_case_registry_document(
        document,
        source=source,
        profile=profile,
        fixture_vars=fixture_vars,
    )


def validate_case_registry_or_raise(document: Any, *, source: str = "<memory>") -> None:
    """以与 CLI lint 相同的规则做阻断式校验；warning 不触发异常。"""
    report = lint_case_registry_document(document, source=source)
    if not report["errors"]:
        return
    detail = "\n".join(
        f"- [{finding['code']}] {finding['path']}: {finding['message']}"
        for finding in report["errors"]
    )
    raise CaseRegistryError(f"{source} Case Registry validation failed:\n{detail}")


__all__ = [
    "CaseRegistryError",
    "SCHEMA_NAME",
    "lint_case_registry_document",
    "lint_case_registry_file",
    "validate_case_registry_or_raise",
]
