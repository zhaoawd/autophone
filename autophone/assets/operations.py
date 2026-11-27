"""operations.py — 把用例/验证单的 precondition 绑定到操作库（「引用」解析层）。

这是 platform-roadmap §八.2「用例 = 操作 + 断言」合流的接缝：precondition 不再
内联硬编码造数/改岗逻辑，而是按 name 引用操作库（apps/<app>/operations/）里的操作。
本模块负责解析 + fail-loud 校验那条引用——操作必须存在且 active、传入参数必须满足
操作声明的 param 契约（未知参数 / 缺必填 / 枚举越界都报错），并回填默认值。

执行本身（真去造单/点同意）属于后续的操作执行器（type=operation 的 run）；这里只做
「绑定 + 校验 + 解析出确定性入参」，使一条坏引用在加载期就 fail-loud，而非跑起来才炸。

precondition 里的引用形态：
    precondition:
      setup:
        - op: create_order
          with: { order_type: resident_enterprise, mode: execute }
        - op: passive_break_off
          with: { action: 离岗 }
旧形态（NL 字符串列表 / {fixture: ...}）原样放行，本模块只认 `setup` 下的 op 引用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from autophone.assets.apps import AppProfile

# Case 级 Operation 引用角色，按 Runner 派发顺序：setup 在 Recipe 主体前，teardown 在其后，
# post_case_cleanup 在 Run 边界。`autophone.execution.effects` 从这里导入（kernel 不得依赖 domain）。
CASE_OPERATION_ROLES: tuple[str, ...] = ("setup", "teardown", "post_case_cleanup")


class OperationError(ValueError):
    """操作引用解析失败。继承 ValueError，API 层沿用既有 422 映射。"""


@dataclass(frozen=True)
class ResolvedOperation:
    """一条解析后的操作引用：稳定 id/name/revision + 回填默认值后的入参。"""

    name: str
    id: str
    revision: int
    args: dict[str, Any]
    summary: str = ""


def load_operations(profile: AppProfile) -> dict[str, dict[str, Any]]:
    """加载某 App 的操作库，按 name 建索引。无操作库 → 空 dict；active 操作名重复 → fail-loud。

    索引包含所有状态的操作（draft/active/deprecated 等），以便通过 name 查找。但只对 active 状态做
    重名检查：draft 允许同名共存（试验阶段的多次迭代），active 要求唯一（被用例引用时需确定性绑定）。
    同名冲突时：多个 active 同名 → fail-loud；active + draft 同名 → active 优先覆盖（draft 不入索引）。
    """
    path = profile.operations_path()
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return index_operations(doc.get("operations") or [], source=path)


def active_name_conflicts(profile: AppProfile, operation: dict[str, Any]) -> list[str]:
    """Return active owners of this name other than the candidate itself."""
    owner = load_operations(profile).get(operation.get("name"))
    if owner and owner.get("status") == "active" and owner.get("id") != operation.get("id"):
        return [str(owner["id"])]
    return []


def index_operations(rows: Any, *, source: Any = "operations") -> dict[str, dict[str, Any]]:
    """按 ``load_operations`` 的规则为一组操作条目建 name 索引（active 优先，active 重名 fail-loud）。

    ``load_operations`` 读文件后调用本函数；降级闸（BL-038）用它评估「改写 status 之后」的
    假设操作库，判定规则与读文件时一致。``source`` 只用于错误消息定位。
    """
    path = source
    index: dict[str, dict[str, Any]] = {}
    active_names: set[str] = set()

    for op in rows or []:
        name = op.get("name")
        if not name:
            raise OperationError(f"操作缺少 name：id={op.get('id')}（{path}）")

        status = op.get("status")
        if status == "active":
            # active 操作：检查重名 + 优先入索引（覆盖同名 draft）
            if name in active_names:
                raise OperationError(f"active 操作 name 重复：{name}（{path}）")
            active_names.add(name)
            index[name] = op
        elif name not in index:
            # 非 active 操作（draft/deprecated 等）：仅当该 name 尚未被占用时才入索引
            # 这样 active 优先，draft 只在没有同名 active 时才可被查到
            index[name] = op

    return index


def _validate_args(op: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """按操作声明的 params 契约校验入参，回填默认值，返回解析后的入参。"""
    params = {p["name"]: p for p in (op.get("params") or [])}
    unknown = set(args) - set(params)
    if unknown:
        raise OperationError(f"操作 {op['name']} 不接受参数：{sorted(unknown)}（可用：{sorted(params)}）")

    resolved: dict[str, Any] = {}
    for name, p in params.items():
        if name in args:
            value = args[name]
        elif "default" in p:
            value = p["default"]
        elif p.get("required"):
            raise OperationError(f"操作 {op['name']} 缺少必填参数：{name}")
        else:
            continue  # 可选且无默认且未传 → 不带入
        if p.get("type") == "enum":
            values = p.get("values") or []
            if value not in values:
                raise OperationError(
                    f"操作 {op['name']} 参数 {name}={value!r} 不在取值域 {values}"
                )
        resolved[name] = value
    return resolved


def resolve_operation(
    profile: AppProfile,
    name: str,
    args: dict[str, Any] | None = None,
    *,
    allow_statuses: tuple[str, ...] = ("active",),
) -> ResolvedOperation:
    """解析单条操作引用。操作不存在 / 状态不在 allow_statuses / 参数不合契约 → fail-loud。

    默认只认 active（setup/teardown 引用、用例绑定走这条铁律）。**draft 回放转正**例外：
    `operation run --draft-id OPx` 显式回放一条 draft 时传 `allow_statuses=("active","draft")`，
    否则 draft 永远跑不起来、攒不出连续 passed、过不了冻结门（M2 死锁）。
    """
    index = load_operations(profile)
    op = index.get(name)
    if op is None:
        raise OperationError(f"操作未找到：{name}（app={profile.app}，可用：{sorted(index)}）")
    if op.get("status") not in allow_statuses:
        raise OperationError(
            f"操作 {name} 状态为 {op.get('status')!r}，仅 {'/'.join(allow_statuses)} 可被引用"
        )
    resolved_args = _validate_args(op, args or {})
    return ResolvedOperation(
        name=name, id=str(op["id"]), revision=int(op["revision"]),
        args=resolved_args, summary=str(op.get("summary") or ""),
    )


def resolve_refs(profile: AppProfile, refs: Any, *, label: str = "ref") -> list[ResolvedOperation]:
    """解析一串操作引用 `[{op: <name>, with: {...}}, ...]`。非列表 / 空 → []。

    每条引用经 `resolve_operation` 做 fail-loud 契约校验（op 不存在/非 active/参数不合）。
    候选条目里可能带 `reason` 等额外键（AI 推荐用），本函数只认 `op`/`with`，其余忽略。
    `label` 仅用于错误消息定位（"setup" / "teardown" / "suggested_setup"…）。
    """
    out: list[ResolvedOperation] = []
    for i, ref in enumerate(refs or []):
        if not isinstance(ref, dict) or "op" not in ref:
            raise OperationError(f"{label}[{i}] 形态非法，应为 {{op: <name>, with: {{...}}}}：{ref!r}")
        with_args = ref.get("with")
        if with_args is not None and not isinstance(with_args, dict):
            # 不先卡形态的话，下游 _validate_args 的 set(args) 会对非 dict 抛 TypeError（API 层
            # 只接 OperationError → 漏成 500）。这里 fail-loud 成契约错（review 续审 P1）。
            raise OperationError(f"{label}[{i}] 的 with 必须是对象：{with_args!r}")
        out.append(resolve_operation(profile, str(ref["op"]), with_args or {}))
    return out


def resolve_setup(profile: AppProfile, container: Any) -> list[ResolvedOperation]:
    """解析某 mapping 里 `setup` 下的操作引用列表（前置）。无 setup → []。

    container 是承载 setup 的映射：验证单传 precondition（dict），回归用例传 case
    本身（NL precondition 仍作描述，机器可读引用落在 case 级 `setup`）。旧形态
    precondition（NL 列表 / {fixture}）不是 dict 或没有 setup → 返回 []，向后兼容。
    """
    setup = container.get("setup") if isinstance(container, dict) else None
    return resolve_refs(profile, setup, label="setup")


def resolve_teardown(profile: AppProfile, container: Any) -> list[ResolvedOperation]:
    """解析 case 级 `teardown` 下的操作引用列表（后置动作）。无 teardown → []。

    与 `setup` 完全对称：用例跑完后要复位/收尾的操作（退出登录、复位状态、取消订单…）
    都引用操作库里的同一批操作。旧用例无 teardown → []，向后兼容。
    """
    teardown = container.get("teardown") if isinstance(container, dict) else None
    return resolve_refs(profile, teardown, label="teardown")


def inactive_operation_refs(
    profile: Any,
    *,
    steps: Any = None,
    case: dict[str, Any] | None = None,
    library: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """列出 Recipe 步骤与 Case Operation 引用中引用的非 active 操作库条目（BL-038）。

    规则「active Recipe 只能引用 active Operation」的判定面：

    * Recipe 步骤 ``{operation: <name>}`` 与 Case ``setup``/``teardown``/``post_case_cleanup``
      （``effects.CASE_OPERATION_ROLES``，与 Runner 派发的角色一致）的 ``{op: <name>}``
      按 name 对应操作库（``apps/<app>/operations/operations.yaml``）条目；
    * 只判操作库中存在的条目。仅在代码中 ``register_operation`` 注册、未登记操作库的执行器
      没有生命周期状态，不在本规则范围内（其引用合法性由 Recipe 静态契约负责）；
    * 同名 active 与 draft 共存时按 ``load_operations`` 的 active 优先索引判定。

    profile 没有操作库（无 ``operations_path`` 或文件不存在）时返回空列表。操作库本身
    非法（如 active 重名）时 ``load_operations`` 抛 ``OperationError``，由调用方 fail-closed。

    ``library`` 给出时按该索引判定，不读文件；降级闸用它评估改写 status 后的假设操作库。
    """
    if library is None:
        if not callable(getattr(profile, "operations_path", None)):
            return []
        library = load_operations(profile)
    if not library:
        return []
    refs: list[tuple[str, int, Any]] = []
    for index, step in enumerate(steps or []):
        if isinstance(step, dict) and "operation" in step:
            refs.append(("recipe", index, step.get("operation")))
    for source in CASE_OPERATION_ROLES:
        rows = (case or {}).get(source)
        for index, ref in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(ref, dict) and "op" in ref:
                refs.append((source, index, ref.get("op")))
    findings: list[dict[str, Any]] = []
    for source, index, name in refs:
        op = library.get(str(name))
        if op is None or op.get("status") == "active":
            continue
        findings.append({
            "source": source,
            "index": index,
            "name": str(name),
            "id": str(op.get("id") or ""),
            "status": op.get("status"),
        })
    return findings


#: 操作库条目上声明「断言型 Operation」的字段（operation.v1 ``case_assertion``，BL-210）。
CASE_ASSERTION_FIELD = "case_assertion"


def declares_case_assertion(op: Any) -> bool:
    """操作库条目是否声明为 Case Assertion：只认 ``case_assertion: true``，缺省或其他值都不算。"""
    return isinstance(op, dict) and op.get(CASE_ASSERTION_FIELD) is True


def case_assertion_operation_names(library: dict[str, dict[str, Any]]) -> list[str]:
    """按 ``load_operations`` 索引列出声明为 Case Assertion 的操作名（排序）。"""
    return sorted(name for name, op in (library or {}).items() if declares_case_assertion(op))


def is_case_assertion_step(step: Any, library: dict[str, dict[str, Any]]) -> bool:
    """一个 typed-IR Recipe 步骤是否是 Case Assertion。

    * ``{action: <verb>}``：verb 以 ``assert`` 开头（平台白名单的 ``assert_*`` 动词）；
    * ``{operation: <name>}``：name 按 ``load_operations`` 的规则（active 优先）在该 App 操作库中
      解析到条目，且该条目声明 ``case_assertion: true``。解析不到或未声明的都不算，不按名字前缀推断。

    不检查条目生命周期：草稿 Recipe 可引用非 active Operation（§6.2），「active Recipe 只能引用
    active Operation」由 live 写入与转正发布时的 ``inactive_operation_refs`` 负责。
    """
    if not isinstance(step, dict):
        return False
    if "action" in step:
        return str(step.get("action") or "").startswith("assert")
    if "operation" in step:
        return declares_case_assertion((library or {}).get(str(step.get("operation"))))
    return False


def describe_operation_refs(findings: list[dict[str, Any]]) -> str:
    """把 ``inactive_operation_refs`` 的结果写成一行说明，供错误消息与 lint 使用。"""
    return "；".join(
        f"{row['source']}[{row['index']}] {row['name']}（{row['id'] or '无 id'}，"
        f"status={row['status']!r}）"
        for row in findings
    )
