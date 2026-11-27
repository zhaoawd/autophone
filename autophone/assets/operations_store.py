"""operations_store.py — 操作库 YAML 的生命周期写回（编辑 / 废弃 / 追加）。

与 cases_store 并列，是 app-agnostic 的 `asset_store` 的另一处薄特化：声明「操作」
这一资产类型的 `AssetSpec`（schema_version 1、可编辑标量集、id 前缀 OP），复用
同一套 surgical 写回 + stable key/revision/status 核心（见 platform-roadmap §八.3）。

操作无 expects（断言属于用例）；可编辑标量限于 name/summary。steps 允许通过
专门 API 编辑，走整条目块重排并在 API 层先过 typed-IR 静态校验。断言型 Operation 的
`case_assertion` 声明经 `declare_case_assertion` 写入（BL-210），不进入通用编辑白名单。
"""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from functools import wraps
from pathlib import Path
from typing import Any

from autophone.assets import store as asset_store
from autophone.assets.apps import AppProfile
from autophone.assets.store import LIFECYCLE, VALID_STATUS  # 重导出

# 可被 UI/API 编辑的扁平标量字段。params/steps/provenance 等嵌套结构不在此列。
EDITABLE_SCALAR = {"name", "summary"}
EDITABLE_STRUCT = {"steps"}

__all__ = [
    "EDITABLE_SCALAR",
    "EDITABLE_STRUCT",
    "LIFECYCLE",
    "VALID_STATUS",
    "operation_registry_write_lock",
    "update_operation",
    "deprecate_operation",
    "detach_operation_source",
    "declare_case_assertion",
    "append_operation",
]

OPERATIONS_SPEC = asset_store.AssetSpec(
    list_key="operations",
    schema_version=1,
    editable_scalars=frozenset(EDITABLE_SCALAR),
    editable_structs=frozenset(EDITABLE_STRUCT),
    id_prefix="OP",
)


def operation_registry_write_lock(profile: AppProfile) -> AbstractContextManager[None]:
    """Serialize reference checks and publication against Operation lifecycle writes.

    Acquire before Case Registry, draft Recipe, live Recipe and expectation locks.
    Profiles with only code-registered operations have no registry to lock.
    """
    getter = getattr(profile, "operations_path", None)
    return asset_store.path_write_lock(getter()) if callable(getter) else nullcontext()


def _locked_mutation(function):
    @wraps(function)
    def locked(path: Path, *args, **kwargs):
        with asset_store.path_write_lock(path):
            return function(path, *args, **kwargs)
    return locked


@_locked_mutation
def update_operation(path: Path, op_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """编辑一条操作的标量字段。内容字段变更 → revision++；纯 status 变更不动 revision。"""
    return asset_store.update_item(OPERATIONS_SPEC, path, op_id, changes)


@_locked_mutation
def deprecate_operation(path: Path, op_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """软删一条操作：status=deprecated（+ 可选 deprecate_reason），不动 revision。"""
    return asset_store.deprecate_item(OPERATIONS_SPEC, path, op_id, reason=reason)


@_locked_mutation
def detach_operation_source(
    path: Path, op_id: str, *, reason: str | None = None,
) -> dict[str, Any]:
    """来源录制被删：标 `provenance.detached`，draft 顺带软删。

    删掉录制目录就等于删掉这条操作的全部证据——trace、逐步截图、人手节奏、重提升的可能性
    都没了。以前这条 draft 什么都不知道地留着，照样能被回放、被统计进转正门，面板上也看不出
    它的来源已经不在了。这里把这件事写进资产本身：`detached` 供面板标注，draft 直接废弃
    （试验品失去证据就不该再往转正走）。`deprecated` 的只补标记。
    """
    doc = asset_store._read_doc(OPERATIONS_SPEC, path)
    item = asset_store._item_dict(OPERATIONS_SPEC, doc, op_id)
    provenance = dict(item.get("provenance") or {})
    provenance["detached"] = True
    changes: dict[str, Any] = {"provenance": provenance}
    if str(item.get("status") or "") == "draft":
        changes["status"] = "deprecated"
        if reason:
            changes["deprecate_reason"] = reason
    return asset_store.rewrite_item_fields(OPERATIONS_SPEC, path, op_id, changes)


@_locked_mutation
def declare_case_assertion(path: Path, op_id: str, declared: bool = True) -> dict[str, Any]:
    """写入一条操作的 ``case_assertion`` 声明（BL-210），返回写回后的条目。

    声明只改变创建期检查对该操作的认定（「用例 → Recipe 草稿」非空断言检查），不改变步骤、
    执行器或副作用，按分类元数据处理：不动 revision、不动 status，任何生命周期都可写。
    写入持操作库写锁；写后整库须贴 ``operation.v1``，不贴则恢复原文并抛 ``ValueError``。
    条目不存在抛 ``KeyError``。
    """
    if not isinstance(declared, bool):
        raise ValueError(f"case_assertion 须为 bool，got {declared!r}")
    from autophone.contracts import ContractError, validate_or_raise

    original = path.read_text(encoding="utf-8")
    asset_store._item_dict(OPERATIONS_SPEC, asset_store._read_doc(OPERATIONS_SPEC, path), op_id)
    result = asset_store.rewrite_item_fields(
        OPERATIONS_SPEC, path, op_id, {"case_assertion": declared}, bump_revision=False,
    )
    try:
        validate_or_raise("operation.v1.json", asset_store._read_doc(OPERATIONS_SPEC, path))
    except ContractError as exc:
        asset_store.atomic_write_text(path, original)
        raise ValueError(f"写入 case_assertion 后操作库不贴 operation.v1（已恢复原文）：{exc}") from exc
    return result


@_locked_mutation
def append_operation(path: Path, fields: dict[str, Any], *, status: str = "active") -> dict[str, Any]:
    """在 `operations:` 列表尾追加一条新操作，id 从 meta.next_seq 分配。

    用于收录入口：fields 给业务字段（name/summary/params/steps/provenance…），
    id/status/revision/next_seq 由本函数管理。收录默认走 draft 经人审再转 active。
    """
    return asset_store.append_item(OPERATIONS_SPEC, path, fields, status=status)
