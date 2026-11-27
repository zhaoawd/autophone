"""cases_store.py — 用例库 YAML 的生命周期写回（编辑 / 废弃 / 追加）。

平台接管真源后，用例的生命周期写操作都走这里。本模块现在是 app-agnostic 的
`asset_store` 的 Case 特化：surgical 写回 + stable key/revision/status 的通用核心位于
`autophone.assets.store`，这里只声明「用例」这一资产类型的 `AssetSpec`（schema_version 2、
可编辑标量集、id 前缀 R），并保留语义化的 update_case/deprecate_case/append_case
公开名（service.py / api.py 仍按这些名字调用）。操作库等其他资产复用同一核心，
见 platform-roadmap §八.3。

约束（fail-loud，由 asset_store 强制）：
  - 只支持标量字段编辑（automation / level / 文案等）；steps/expects 等嵌套结构
    UI 暂不可改，传入即报错。
  - 内容字段变更 → revision++；纯 status 变更（废弃 / 复活）不动 revision。
  - 分类元数据 `CLASSIFICATION_SCALAR`（business_family / family_id / pair_id /
    family_variant）写入不动 revision，仍经同一写锁并写 INFO 日志（Case Family 提案 D2）。
    CAS 覆盖范围：`expected_revision` 只能发现内容字段与其他会使 revision 加 1 的写入；
    分类写入不改 revision，两个持有同一 revision 的分类写入方彼此不可见。写分类字段的
    调用方若基于先前读取的值做决定，须同时传 `expected_classification`，在锁内比对被写
    分类字段的当前值。
  - 现存 YAML 必须是 schema_version 2（migrate_v2 之后）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from autophone.assets import store as asset_store
from autophone.assets.case_ids import allocate_case_ids
from autophone.assets.store import LIFECYCLE, VALID_STATUS  # 重导出，兼容既有引用
from autophone.assets.policy import assert_assets_writable
from autophone.assets.scheduling import validate_scheduling
from autophone.logging import get_logger

_T = TypeVar("_T")
RegistryTextMutation = Callable[[str | None], tuple[str | None, _T]]
registry_write_lock = asset_store.path_write_lock
log = get_logger(__name__)


class CaseRevisionConflict(ValueError):
    """The Case changed after its caller captured the expected revision."""

# 可被 UI 编辑的扁平标量字段。嵌套结构（suitable/raw）不在此列。
# case_type/test_point/owner 是通用用例模板（autophone/_data/test-cases-template.xlsx）引入的列。
EDITABLE_SCALAR = {
    "automation", "automation_reason", "level", "order_type",
    "item", "module", "desc", "remark",
    "case_type", "test_point", "owner", "login_precondition",
    "business_family", "family_id", "pair_id", "family_variant",
}

# 分类元数据：只表达业务归类（Case Family 与对照对），不改变测试意图与执行身份，写入不动
# revision（Case Family 提案 D2）。family_id 是 Case → Family 成员关系的唯一真源，
# 引用校验见 autophone.assets.business_chains。
CLASSIFICATION_SCALAR = frozenset({"business_family", "family_id", "pair_id", "family_variant"})

# login_precondition：该用例对「登录态」的前置要求。run 级登录态闸（service._login_gate_applies）
# 据此决定要不要拦本 run——缺省即 required。required=需已登录（订单/上下岗等绝大多数用例）；
# logged_out=需未登录/已登出（登录失败类，如 R48/R49）；any=不在乎起始态/自管理（recipe 自己
# 先 logout 再 login，如 R47）。logged_out/any 都不被登录态闸阻塞。
VALID_LOGIN_PRECONDITION = {"required", "logged_out", "any"}

CASE_AUTOMATION_VALUES = frozenset({
    "",
    "ui_ready",
    "needs_fixture",
    "blocked_by_infra",
    "manual_checkpoint",
    "phase4_prefix_api",
})


def normalize_case_automation(value: Any) -> str:
    """Normalize one Case automation value before Registry persistence."""
    if not isinstance(value, str):
        raise ValueError(
            f"automation 非法：须为字符串，收到 {type(value).__name__}"
        )
    normalized = value.strip()
    if normalized not in CASE_AUTOMATION_VALUES:
        raise ValueError(
            f"automation 非法：{normalized!r}"
            f"（应为 {sorted(CASE_AUTOMATION_VALUES)}）"
        )
    return normalized


def active_case_validation_error(case: dict[str, Any]) -> str | None:
    """Return the first Active Case Registry invariant violation."""
    if case.get("status") != "active":
        return None
    try:
        automation = normalize_case_automation(case.get("automation"))
    except ValueError as exc:
        return f"active_case_invalid_automation — {exc}"
    if not automation:
        return "active_case_missing_automation — Active Case 必须声明非空 automation"
    expects = case.get("expects")
    if not isinstance(expects, list) or not any(
        isinstance(item, str) and bool(item.strip()) for item in expects
    ):
        return "active_case_missing_expects — Active Case 必须声明至少一个非空 expects"
    return None

# 字符串列表字段：UI/API 可编辑，写回走整条目块重排（asset_store._rewrite_item）。
EDITABLE_LIST = {"precondition", "steps", "expects", "suites"}

# 结构化列表字段（list-of-dict 操作引用）：UI/API 可编辑，同走整条目块重排。
# setup=前置操作、teardown=后置操作（确认后的）；post_case_cleanup 是 run 级清理通道，
# 不复用 teardown 的结果降级语义；suggested_* 是 AI 推荐的待确认候选
# （条目可带 reason）。详见 platform.operations.resolve_setup/resolve_teardown 与
# autophone/platform/ai_suggest_ops.py。
EDITABLE_STRUCT = {"setup", "teardown", "post_case_cleanup", "suggested_setup", "suggested_teardown"}

# 批调度运行期的最小稳定契约。它是 object（不是操作引用列表），由 scheduling module
# 统一做 schema + setup 前缀语义校验；raw.hints 仍只承载 advisory 信息。
EDITABLE_OBJECT = {"scheduling"}

# 局部开放 raw.hints：M2 order-pool/AI advisory 需要写 hint，但仍不能编辑整个 raw。
EDITABLE_RAW_HINTS = {"raw.hints"}

__all__ = [
    "EDITABLE_SCALAR", "EDITABLE_LIST", "EDITABLE_STRUCT", "EDITABLE_OBJECT", "EDITABLE_RAW_HINTS",
    "CLASSIFICATION_SCALAR",
    "VALID_LOGIN_PRECONDITION", "CASE_AUTOMATION_VALUES",
    "normalize_case_automation", "active_case_validation_error",
    "LIFECYCLE", "VALID_STATUS",
    "CaseRevisionConflict",
    "update_case", "deprecate_case", "deprecate_cases", "activate_cases", "append_case",
    "mutate_registry_text", "registry_write_lock",
]

CASES_SPEC = asset_store.AssetSpec(
    list_key="cases",
    schema_version=2,
    editable_scalars=frozenset(EDITABLE_SCALAR),
    editable_lists=frozenset(EDITABLE_LIST),
    editable_structs=frozenset(EDITABLE_STRUCT),
    id_prefix="R",
    migrate_hint="写回前请先跑 `python3 -m autophone.assets.case_import.migrate_v2 --write`",
    classification_scalars=CLASSIFICATION_SCALAR,
)


def update_case(
    path: Path,
    case_id: str,
    changes: dict[str, Any],
    *,
    expected_revision: int | None = None,
    expected_classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """编辑一条用例的标量字段。内容字段变更 → revision++；纯 status 与分类元数据变更不动 revision。

    返回写回后的该用例 dict。未知/嵌套字段、非法 status 一律 fail-loud。
    ``expected_revision`` 在同一 Registry 写锁内做 CAS；不匹配时写前拒绝。它不覆盖分类字段：
    分类写入不改 revision。``expected_classification`` 给出调用方读到的分类字段值
    （键须属于 ``CLASSIFICATION_SCALAR``；空值与缺失都按空串比较），在同一把锁内与当前值
    比对，不一致时抛 ``CaseRevisionConflict``，写前拒绝。
    """
    assert_assets_writable("update case asset", path=path)
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision 必须是正整数")
    if expected_classification is not None:
        if not isinstance(expected_classification, dict):
            raise ValueError("expected_classification 必须是对象")
        stray = sorted(set(expected_classification) - CLASSIFICATION_SCALAR)
        if stray:
            raise ValueError(f"expected_classification 只能包含分类字段：{stray}")
    with registry_write_lock(path):
        current: dict[str, Any] | None = None
        if (
            expected_revision is not None
            or expected_classification is not None
            or "setup" in changes
        ):
            doc = asset_store._read_doc(CASES_SPEC, path)
            current = dict(asset_store._item_dict(CASES_SPEC, doc, case_id))
        if expected_revision is not None:
            assert current is not None
            actual_revision = current.get("revision")
            if (
                not isinstance(actual_revision, int)
                or isinstance(actual_revision, bool)
                or actual_revision < 1
            ):
                raise ValueError(f"Case {case_id} revision 非法：{actual_revision!r}")
            if actual_revision != expected_revision:
                raise CaseRevisionConflict(
                    f"Case {case_id} revision 冲突："
                    f"expected={expected_revision}, actual={actual_revision}"
                )
        if expected_classification is not None:
            assert current is not None
            for key, expected in sorted(expected_classification.items()):
                actual = _classification_value(current.get(key))
                if actual != _classification_value(expected):
                    raise CaseRevisionConflict(
                        f"Case {case_id} 分类字段 {key} 冲突："
                        f"expected={_classification_value(expected)!r}, actual={actual!r}"
                    )
        if "raw.hints" in changes or EDITABLE_OBJECT & set(changes):
            result = _update_case_with_objects(path, case_id, changes)
        else:
            if "setup" in changes:
                # setup 改动不能让既有 scheduling.shareable_prefix 漂成假前缀。
                assert current is not None
                prospective = {**current, **changes}
                if prospective.get("scheduling") is not None:
                    validate_scheduling(prospective["scheduling"], case=prospective)
            result = asset_store.update_item(CASES_SPEC, path, case_id, changes)
        classified = sorted(CLASSIFICATION_SCALAR & set(changes))
        if classified:
            log.info(
                "case classification written: path=%s case_id=%s fields=%s "
                "values=%s revision=%s",
                path, case_id, classified,
                {key: result.get(key) for key in classified}, result.get("revision"),
            )
        return result


def _classification_value(value: Any) -> str:
    """Comparable form of one classification value: ``None`` and blanks are ``""``."""
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _validate_case_changes(changes: dict[str, Any]) -> None:
    bad = (
        set(changes)
        - set(EDITABLE_SCALAR)
        - set(EDITABLE_LIST)
        - set(EDITABLE_STRUCT)
        - set(EDITABLE_OBJECT)
        - EDITABLE_RAW_HINTS
        - asset_store.LIFECYCLE
    )
    if bad:
        raise ValueError(f"不可编辑的字段：{sorted(bad)}（嵌套结构或未知字段）")
    if "status" in changes and changes["status"] not in VALID_STATUS:
        raise ValueError(f"非法 status：{changes['status']}（应为 {sorted(VALID_STATUS)}）")
    for key in EDITABLE_LIST & set(changes):
        value = changes[key]
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(f"字段 {key} 须为字符串列表（steps/expects/precondition 形态）")
    for key in EDITABLE_STRUCT & set(changes):
        if not isinstance(changes[key], list):
            raise ValueError(f"字段 {key} 须为列表（操作引用 [{{op, with}}] 形态）")
    for key in EDITABLE_OBJECT & set(changes):
        if not isinstance(changes[key], dict):
            raise ValueError(f"字段 {key} 须为对象")
    if "raw.hints" in changes and not isinstance(changes["raw.hints"], dict):
        raise ValueError("字段 raw.hints 须为对象")


def _update_case_with_objects(path: Path, case_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """整条目重排目标 case，只开放 raw.hints 与显式稳定 object 字段。"""
    assert_assets_writable("update case object fields", path=path)
    _validate_case_changes(changes)
    yaml = asset_store._require_yaml()
    doc = asset_store._read_doc(CASES_SPEC, path)
    item = dict(asset_store._item_dict(CASES_SPEC, doc, case_id))
    for key, value in changes.items():
        if key == "raw.hints":
            raw = dict(item.get("raw") or {})
            raw["hints"] = dict(value)
            item["raw"] = raw
        elif key == "scheduling":
            if value:
                item["scheduling"] = dict(value)
            else:
                item.pop("scheduling", None)
        else:
            item[key] = value

    if item.get("scheduling") is not None:
        item["scheduling"] = validate_scheduling(item["scheduling"], case=item)

    # raw.hints 与 object 字段不在 AssetSpec 的可编辑集合中，按内容变更计。
    content_changed = any(
        key != "status" and key not in CLASSIFICATION_SCALAR for key in changes
    )
    if content_changed:
        item["revision"] = int(item.get("revision") or 1) + 1

    block = yaml.safe_dump([item], allow_unicode=True, sort_keys=False, width=100)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = asset_store._find_block(lines, case_id)
    asset_store.atomic_write_text(
        path,
        "".join(lines[:start] + block.splitlines(keepends=True) + lines[end:]),
    )
    return asset_store._item_dict(CASES_SPEC, asset_store._read_doc(CASES_SPEC, path), case_id)


def deprecate_case(path: Path, case_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """软删一条用例：status=deprecated（+ 可选 deprecate_reason），不动 revision。"""
    assert_assets_writable("deprecate case asset", path=path)
    with registry_write_lock(path):
        return asset_store.deprecate_item(CASES_SPEC, path, case_id, reason=reason)


def deprecate_cases(path: Path, case_ids: list[str], *, reason: str | None = None) -> dict[str, Any]:
    """批量软删多条用例。逐条 surgical 写回、fail-soft 汇总，返回 {deprecated, failed}。"""
    assert_assets_writable("batch deprecate case assets", path=path)
    with registry_write_lock(path):
        return asset_store.deprecate_items(CASES_SPEC, path, case_ids, reason=reason)


def activate_cases(path: Path, case_ids: list[str]) -> dict[str, Any]:
    """批量转在用：status=active，逐条 surgical 写回、fail-soft 汇总。

    返回 {updated, failed}。纯 status 变更不动 revision。已 active 的条目幂等（照常计入
    updated）。草稿/归档转正用；deprecated 也可被复活，调用方按需筛选传入的 ids。
    """
    assert_assets_writable("activate case assets", path=path)
    with registry_write_lock(path):
        return asset_store.set_status_items(CASES_SPEC, path, case_ids, "active")


def append_case(path: Path, fields: dict[str, Any], *, status: str = "draft") -> dict[str, Any]:
    """在 `cases:` 列表尾追加一条新用例，id 从 meta.next_seq 分配。返回新用例 dict。

    新建默认落 draft；只有已通过转正门的专用入口才显式传 active。
    fields 给业务字段（module/item/automation/level/steps/expects/provenance…），
    id/status/revision/next_seq 由本函数管理。
    """
    assert_assets_writable("append case asset", path=path)
    with registry_write_lock(path):
        if fields.get("scheduling") is not None:
            fields = dict(fields)
            fields["scheduling"] = validate_scheduling(fields["scheduling"], case=fields)
        doc = asset_store._read_doc(CASES_SPEC, path)
        allocated_ids, next_seq_after = allocate_case_ids(doc, [fields])
        return asset_store.append_item(
            CASES_SPEC,
            path,
            fields,
            status=status,
            item_id=allocated_ids[0],
            allocated_next_seq=next_seq_after,
        )


def mutate_registry_text(path: Path, mutation: RegistryTextMutation[_T]) -> _T:
    """Apply one full-registry text mutation under the shared write boundary.

    The callback receives the current text, or ``None`` when the Registry does
    not exist.  Returning ``None`` as the replacement is a successful no-op.
    Any replacement is committed through a same-directory atomic rename.
    """
    assert_assets_writable("mutate case registry", path=path)
    with registry_write_lock(path):
        current = path.read_text(encoding="utf-8") if path.exists() else None
        replacement, result = mutation(current)
        if replacement is not None:
            asset_store.atomic_write_text(path, replacement)
        return result
