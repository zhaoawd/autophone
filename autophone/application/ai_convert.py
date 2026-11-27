"""ai_convert.py — AI-5 用例→recipe 转换器（platform-ai-language-to-execution P1 转换器脊）。

把一条**自然语言回归用例**（item/precondition/steps/expects）交给本地 agent，转成一条**可执行的
typed-IR recipe 草稿**：用已注册的 operation/action 把设备驱动到用例场景，并把用例的【预期】逐条钉成
`assert_*` 步骤。这是「语言→执行」资产侧的脊——把人写的用例自动变成可回归的执行资产。

与 AI-4（`ai_draft.draft_operation`，起草无断言的 operation）对偶：本模块产出**带断言的 recipe**，
是用例层；operation 是它组合的积木。两者共用同一套接缝（`_capability_brief` 白名单 + `validate_recipe`
静态闸 + fail-closed 形态），只是 schema / 系统提示不同。

三条铁律：
1. **产物恒 draft**，落 `apps/<app>/recipes/drafts/<id>.yaml`（`status: draft`）——**绝不**落
   `recipes/*.yaml`：那目录被 app 的 recipes 包 `glob("*.yaml")` 全量自动注册进 `RECIPES`，落进去就
   等于让回归 runner 直接拾取 AI 草稿，破「AI 产物不自行入回归库」不变量。`drafts/` 子目录非递归 glob
   不碰，转正（P2：挣 teeth + 人审后）才搬到 `recipes/<id>.yaml`。
2. **缺操作不臆造**：用例的某个前置态若无任何已注册 operation 能达成，模型须返回
   `{needs_extraction, reason}` 而非编一串 action 去凑——这正是 ADR-0004 抽取需求的 demand 信号，
   P3 闭环 auto-pull 时直接消费；P1 先把它结构化 fail-closed 报出来。
3. **拒写脏**：模型回包先过内容 schema，再过 `validate_recipe` 静态闸（瞎编 verb / 名对值错 / 引用未
   注册 operation 全拦），再查**非空断言**（无断言的 recipe 测不了任何东西，vacuous 拒收）。断言步是
   `assert_*` action，或引用该 App 操作库中声明 `case_assertion: true` 的断言型 Operation（BL-210，
   判据由操作库声明，不按名字前缀推断）。
   任一不过 → ok=False，不落任何文件。
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

import yaml

from autophone.paths import CODE_ROOT
from autophone.assets import store as asset_store
from autophone.assets.policy import assert_assets_writable
from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, invoke
from autophone.assets.apps import load_app_profile
from autophone.assets.operations import (
    OperationError,
    case_assertion_operation_names,
    is_case_assertion_step,
    index_operations,
    load_operations,
)
from autophone.execution.app_recipes import import_app_recipes
# 复用 AI-4 的白名单渲染 + typed-IR step 形态，避免两份漂移。
from autophone.application.ai_draft import _ACTION_STEP, _OPERATION_STEP, _capability_brief
from autophone.application.case_intake_progress import stage_event

ProgressFn = Callable[[dict[str, Any]], None]

# 内容 schema：模型恰好回一支——
#   recipe 分支：{steps:[…typed IR…]}（id 由 case_id 装配，不让模型回）
#   缺料分支：  {needs_extraction:<前置态关键词>, reason?}
# additionalProperties:false + 互斥 not，挡「两支混填」。
_RECIPE_BRANCH: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "not": {"required": ["needs_extraction"]},
    "properties": {
        "steps": {"type": "array", "minItems": 1,
                  "items": {"oneOf": [_ACTION_STEP, _OPERATION_STEP]}},
    },
}
_NEEDS_BRANCH: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_extraction"],
    "properties": {
        "needs_extraction": {"type": "string", "minLength": 1,
                             "description": "缺的前置态关键词（ADR-0004 抽取需求 demand token）"},
        "reason": {"type": "string"},
    },
}
_CONVERT_SCHEMA: dict[str, Any] = {"oneOf": [_RECIPE_BRANCH, _NEEDS_BRANCH]}

# 传输 schema：发给 provider 的形态。Codex 结构化输出要求根为严格对象、无 oneOf/not、所有字段必填、
# 不接受开放对象（action step 的 kwarg 键名不固定），所以内容 schema 不能直接发送。传输形态：
#   根：steps / needs_extraction / reason 三键恒在，不用的一支写 null；
#   step：action / operation 二选一（另一个写 null），参数统一放进 args_json（JSON 对象字符串）。
# 回包经 _content_from_transport 还原为内容形态后，仍由 _CONVERT_SCHEMA 校验（两支互斥、无杂键），
# 再过 validate_recipe 静态闸；传输形态不放宽任何本地校验。
_TRANSPORT_STEP: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "operation", "args_json"],
    "properties": {
        "action": {"type": ["string", "null"],
                   "description": "白名单 verb；本步引用 operation 时为 null"},
        "operation": {"type": ["string", "null"],
                      "description": "已注册操作名；本步调用 action 时为 null"},
        "args_json": {"type": "string",
                      "description": "参数的 JSON 对象字符串：action 的 kwarg 或 operation 的 args；无参数写 \"{}\""},
    },
}
_TRANSPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps", "needs_extraction", "reason"],
    "properties": {
        "steps": {"type": ["array", "null"], "items": _TRANSPORT_STEP,
                  "description": "recipe 步骤；返回 needs_extraction 时为 null"},
        "needs_extraction": {"type": ["string", "null"],
                             "description": "缺的前置态关键词；返回 steps 时为 null"},
        "reason": {"type": ["string", "null"],
                   "description": "needs_extraction 的原因；返回 steps 时为 null"},
    },
}
_TRANSPORT_STEP_KEYS = frozenset(_TRANSPORT_STEP["properties"])
_TRANSPORT_ROOT_KEYS = frozenset(_TRANSPORT_SCHEMA["properties"])


class TransportShapeError(ValueError):
    """传输形态无法确定性还原为内容形态（fail-closed，拒绝写盘）。"""


def _content_step(step: Any, index: int) -> Any:
    # 不带 args_json 的 step 按内容形态原样交给内容 schema 校验（归一化对内容形态是恒等变换）。
    if not isinstance(step, dict) or "args_json" not in step:
        return step
    where = f"steps/{index}"
    extra = sorted(set(step) - _TRANSPORT_STEP_KEYS)
    if extra:
        raise TransportShapeError(f"{where}: 传输 step 含未声明字段 {extra}")
    action, operation, raw_args = step.get("action"), step.get("operation"), step.get("args_json")
    if not isinstance(raw_args, str):
        raise TransportShapeError(f"{where}/args_json: 必须是 JSON 对象字符串")
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
    except json.JSONDecodeError as exc:
        raise TransportShapeError(f"{where}/args_json: 不是合法 JSON（{exc.msg}）") from exc
    if not isinstance(args, dict):
        raise TransportShapeError(f"{where}/args_json: 必须解码为 JSON 对象")
    if (action is None) == (operation is None):
        raise TransportShapeError(f"{where}: action 与 operation 必须恰好给出一个")
    if operation is not None:
        return {"operation": operation, "args": args} if args else {"operation": operation}
    reserved = sorted({"action", "operation"} & set(args))
    if reserved:
        raise TransportShapeError(f"{where}/args_json: 参数名不得为 {reserved}")
    return {"action": action, **args}


def _content_from_transport(payload: dict[str, Any]) -> dict[str, Any]:
    """传输形态 → 内容形态。只做确定性映射；互斥、杂键、参数合法性仍交给后续校验。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _TRANSPORT_ROOT_KEYS and value is None:
            continue  # 未选用的一支以 null 表达，还原为缺省
        out[key] = value
    steps = out.get("steps")
    if isinstance(steps, list):
        out["steps"] = [_content_step(step, i) for i, step in enumerate(steps)]
    return out

_SYSTEM = (
    "你是 autophone 的「用例→recipe 转换」agent。把给定的自然语言回归用例（前置/步骤/预期）转成一条"
    "可执行的 **typed IR recipe**：用已注册的 operation/action 把设备驱动到用例场景，并把用例的"
    "【预期】逐条钉成断言步骤（assert_* action，或下方列出的断言型 operation）。\n"
    "每个 step 恰好二选一：\n"
    "  - {\"action\": <verb>, <参数名>: <值>, …}  调一个白名单 verb；参数名必须逐字等于该 verb 的 kwarg。\n"
    "  - {\"operation\": <name>, \"args\": {…}}      引用一条已注册的有状态操作（args 的键等于其 kwarg）。\n"
    "硬约束：\n"
    "  1. recipe **必须至少含一个断言步骤**钉住用例预期——无断言的 recipe 测不了任何东西。断言步骤是 assert_* "
    "action，或「断言型操作」清单中的 operation；其他 operation 即使名字像断言也不算。\n"
    "  2. **禁止控制流**（循环/条件/重试/状态归位）：那类逻辑属于手写 operation，只能引用一条已有 operation，"
    "不要展开成动作序列。\n"
    "  3. 只能用下方「可用动作 / 可用操作」里列出的名字与签名（带 ? 的可选，其余必填），不要瞎编 verb 或参数名。\n"
    "  4. **缺操作就不要臆造**：若用例的某个前置态没有任何已注册 operation 能达成（例如需要先造一单、"
    "先上岗、先取餐），不要编一串 action 去凑，改为只返回 "
    "{\"needs_extraction\": \"<缺的前置态关键词>\", \"reason\": \"<为什么现有能力不够>\"}。\n"
    "输出编码（结构化输出要求所有字段都出现）：\n"
    "  - 根对象恒含 steps / needs_extraction / reason 三个键，未选用的一支写 null。\n"
    "  - 每个 step 写成 {\"action\": <verb 或 null>, \"operation\": <操作名 或 null>, \"args_json\": <参数 JSON 对象字符串>}，"
    "action 与 operation 恰好一个非 null；参数放进 args_json，例如 "
    "{\"action\": \"assert_present\", \"operation\": null, \"args_json\": \"{\\\"queries\\\": [\\\"下班\\\"]}\"}、"
    "{\"action\": null, \"operation\": \"login\", \"args_json\": \"{}\"}。"
)


def _load_case(cases_path: Path, case_id: str) -> dict[str, Any] | None:
    doc = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    for case in doc.get("cases") or []:
        if str(case.get("id")) == case_id:
            return case
    return None


def _extraction_case_context(case: dict[str, Any]) -> dict[str, Any]:
    return {key: case.get(key) for key in ("id", "revision", "item", "precondition", "steps", "expects")}


def _active_operation_contracts(library: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    contracts = []
    for name, operation in sorted(library.items()):
        contract = {key: operation.get(key) for key in ("id", "revision", "status")}
        contract["name"] = name
        witness = operation.get("witness")
        if isinstance(witness, dict):
            contract["witness"] = {key: witness.get(key) for key in ("kind", "expect_stage")}
            negative = witness.get("negative_control")
            if isinstance(negative, dict):
                contract["witness"]["negative_control"] = {
                    key: negative.get(key) for key in ("flip_arg", "expect_stage")
                }
        contracts.append(contract)
    return contracts


def _read_operation_library(path: Path | None, *, allow_missing: bool = False) -> dict[str, dict[str, Any]]:
    if path is None or (allow_missing and not path.exists()):
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise OperationError(f"operation registry must be a mapping: {path}")
    return index_operations(document.get("operations") or [], source=path)


def _build_prompt(case: dict[str, Any], brief: str, assertion_ops: list[str] | None = None) -> str:
    def _lines(key: str) -> str:
        vals = case.get(key) or []
        return "\n".join(f"  - {v}" for v in vals) if vals else "  （无）"

    parts = [
        f"用例编号：{case.get('id')}",
        f"模块：{case.get('module')}",
        f"标题：{case.get('item')}",
        f"前置条件：\n{_lines('precondition')}",
        f"步骤：\n{_lines('steps')}",
        f"预期：\n{_lines('expects')}",
        "把它转成一条可执行的 typed IR recipe（steps），预期逐条钉成断言步骤。",
        "\n" + brief,
        "\n断言型操作（操作库声明 case_assertion，可作为断言步骤）：\n"
        + ("\n".join(f"  - {name}" for name in assertion_ops) if assertion_ops else "  （无）"),
    ]
    return "\n".join(parts)


def _registered_assertion_operations(
    app: str, profile: Any, library: dict[str, dict[str, Any]],
) -> list[str]:
    """操作库声明为 Case Assertion、且有已注册执行器的操作名，只用于提示词清单。"""
    declared = case_assertion_operation_names(library)
    if not declared:
        return []
    from autophone.execution.engine import recipe

    registered = recipe.operations_for_app(app, root=getattr(profile, "root", None) or CODE_ROOT)
    return [name for name in declared if name in registered]


def _report(
    progress: ProgressFn | None,
    stage: str,
    status: str,
    summary: str,
    **details: Any,
) -> None:
    """Progress is observability only and must never change conversion truth."""
    if progress is None:
        return
    try:
        progress(stage_event(stage, status, summary, **details))
    except Exception:
        # A dashboard disconnect or event persistence issue cannot turn a valid
        # deterministic converter result into a different business outcome.
        return


def write_recipe_draft(
    app: str,
    recipe_id: str,
    steps: list[dict[str, Any]],
    *,
    recipes_dir: Path | None = None,
    provenance: dict[str, Any] | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Write a recipe draft under ``recipes/drafts/`` and never under live recipes.

    This is the shared sink for AI convert and AI fix proposals. Callers must
    pass already-reviewed/validated step payloads; the sink still runs the IR
    dispatcher validation before touching disk.
    """
    assert_assets_writable("write recipe draft", path=recipes_dir)
    from autophone.execution import effects
    from autophone.execution.engine.recipe import RecipeError, operation_scope, validate_recipe_for_app

    recipe_id = str(recipe_id).upper()
    profile = load_app_profile(app)
    profile_root = getattr(profile, "root", None) or CODE_ROOT
    import_app_recipes(app, root=profile_root)
    rdir = recipes_dir or profile.recipes_dir()
    with operation_scope(app, root=profile_root):
        try:
            validate_recipe_for_app(steps, profile)
        except (RecipeError, effects.EffectPolicyError) as e:
            raise ValueError(f"recipe draft steps invalid: {e}") from e
    draft_dir = rdir / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    out_path = draft_dir / f"{recipe_id.lower()}.yaml"
    doc = {
        "id": recipe_id,
        "status": "draft",
        "provenance": provenance or {"source": "conversation"},
        "steps": steps,
    }
    with asset_store.path_write_lock(out_path):
        if out_path.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"recipe draft already exists: {out_path}",
                "path": str(out_path),
            }
        asset_store.atomic_write_text(
            out_path,
            yaml.safe_dump(
                doc,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
        )
    return {"ok": True, "recipe": {"id": recipe_id, "steps": steps}, "path": str(out_path), "status": "draft"}


def convert_case(
    app: str,
    case_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 300.0,
    runner: CommandRunner = _subprocess_runner,
    cases_path: Path | None = None,
    recipes_dir: Path | None = None,
    note: str | None = None,
    ops_path: Path | None = None,
    require_active_operations: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """把用例 `case_id` 转成 recipe 草稿，落 `recipes/drafts/<id>.yaml`（status=draft）。

    `ops_path` 只覆盖操作库声明读取；执行器仍按 App 的资产根注册，不由该参数自动注册。
    显式路径不可读取或库内 active 重名时拒绝，不回退默认库。
    自动抽取入口设 `require_active_operations=True`，仅使用 active Operation；普通草稿保持原准入规则。

    返回：
      - {ok:True, recipe:{id,steps}, path, status:"draft"}        成功落草稿
      - {ok:False, needs_extraction:<token>, reason, case_id}     缺操作，结构化 demand 信号（非崩溃）
      - {ok:False, error}                                          fail-closed（无回包/不贴 schema/不过静态闸/无断言）
    """
    assert_assets_writable("convert case to recipe draft", path=recipes_dir)
    case_id = str(case_id).upper()
    _report(progress, "import", "running", f"正在读取用例 {case_id}")
    profile = load_app_profile(app)
    cpath = cases_path or profile.cases_path()
    if not cpath.exists():
        _report(progress, "import", "failed", "没有找到用例注册表",
                blockers=[str(cpath)], next_actions=["确认 App 配置和用例文件路径"])
        return {"ok": False, "error": f"cases registry not found: {cpath}"}
    case = _load_case(cpath, case_id)
    if case is None:
        _report(progress, "import", "failed", f"用例 {case_id} 不存在",
                evidence_refs=[str(cpath)], next_actions=["刷新用例库或核对用例编号"])
        return {"ok": False, "error": f"case not found: {case_id}（{cpath}）"}

    _report(
        progress, "import", "completed", f"已读取 {case_id} · {case.get('item') or '未命名用例'}",
        evidence_refs=[str(cpath)], next_actions=["确认目标 App 与 Build"],
    )
    identity_refs = [
        f"app-profile:{app}",
        f"environment:{getattr(profile, 'env', None) or 'unspecified'}",
        f"bundle-id:{profile.bundle_id}",
    ]
    build_label = getattr(profile, "build_label", None)
    if build_label:
        identity_refs.append(f"build:{build_label}")
    _report(
        progress,
        "identity",
        "completed" if build_label else "attention",
        (
            f"目标已锁定为 {profile.display_name} · {build_label}"
            if build_label else f"已确认 {profile.display_name}，但尚未固定 Build"
        ),
        evidence_refs=identity_refs,
        next_actions=["解析用例测试意图"] if build_label else ["在 App 配置中补充 build_label 后再试运行"],
    )
    _report(
        progress, "intent", "completed",
        (
            f"已整理 {len(case.get('precondition') or [])} 个前置、"
            f"{len(case.get('steps') or [])} 个步骤、{len(case.get('expects') or [])} 个预期"
        ),
        explanation="只解析自然语言测试语义；当前未驱动设备，也未修改业务数据。",
        evidence_refs=[f"case:{case_id}", f"module:{case.get('module') or '未分类'}"],
        next_actions=["从该 App 的知识包匹配可复用能力"],
    )
    _report(progress, "knowledge", "running", "正在检索已注册操作和动作白名单")
    # 断言判据由操作库声明（BL-210）：读取失败（如 active 重名）时 fail-closed，不调用模型。
    try:
        operation_path = ops_path or (
            profile.operations_path() if callable(getattr(profile, "operations_path", None)) else None
        )
        operation_library = _read_operation_library(operation_path, allow_missing=ops_path is None)
    except (OperationError, OSError, yaml.YAMLError, AttributeError, TypeError) as exc:
        _report(progress, "knowledge", "failed", "操作库不可读取",
                blockers=[str(exc)], next_actions=["修正操作库后重新生成"])
        return {"ok": False, "error": f"operation registry invalid: {exc}"}
    active_library = {name: op for name, op in operation_library.items() if op.get("status") == "active"}
    brief = _capability_brief(
        app,
        root=getattr(profile, "root", None) or CODE_ROOT,
        operation_names=set(active_library) if require_active_operations else None,
    )
    assertion_ops = _registered_assertion_operations(
        app, profile, active_library if require_active_operations else operation_library,
    )
    _report(
        progress, "knowledge", "completed", "已载入该 App 的操作库与动作能力",
        evidence_refs=[f"app-profile:{app}", f"operation-registry:{app}", "action-dispatcher"],
        next_actions=["用已确认能力设计准备、操作和断言链路"],
    )
    _report(progress, "design", "running", "AI 正在组合候选执行链路",
            explanation="模型只看到白名单能力；缺少前置能力时必须报告缺口，不能臆造动作。")
    prompt = _build_prompt(case, brief, assertion_ops)
    if require_active_operations:
        prompt += (
            "\n当前 active Operation 契约（从本次实际操作库读取）：\n"
            + json.dumps(_active_operation_contracts(active_library), ensure_ascii=False)
            + "\n根据当前契约判断缺料，已有对应契约时输出 steps。"
        )
    req = AiRequest(
        prompt=prompt,
        system=_SYSTEM,
        tools="none",
        json_schema=_TRANSPORT_SCHEMA,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
    )
    res = invoke(req, runner=runner)
    if not res.ok or not isinstance(res.json, dict):
        _report(progress, "design", "failed", "执行链路生成失败",
                blockers=[res.error or "模型没有返回结构化结果"],
                next_actions=["检查 AI provider 后重试；用例原文不会被改动"])
        return {"ok": False, "error": res.error or "convert agent returned no structured json"}

    # fail-closed：落任何文件前，先把传输形态还原为内容形态，再用内容 schema 本地校验（两支互斥、无杂键）。
    try:
        payload = _content_from_transport(res.json)
    except TransportShapeError as exc:
        _report(progress, "design", "failed", "候选结果不符合 recipe 结构",
                blockers=[str(exc)], next_actions=["保留原用例并重新生成或人工检查模型输出"])
        return {"ok": False, "error": f"convert 回包不贴 schema（拒绝写脏）：{exc}"}
    from jsonschema import Draft202012Validator
    schema_errors = sorted(Draft202012Validator(_CONVERT_SCHEMA).iter_errors(payload),
                           key=lambda e: list(e.path))
    if schema_errors:
        detail = "；".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in schema_errors[:5])
        _report(progress, "design", "failed", "候选结果不符合 recipe 结构",
                blockers=[detail], next_actions=["保留原用例并重新生成或人工检查模型输出"])
        return {"ok": False, "error": f"convert 回包不贴 schema（拒绝写脏）：{detail}"}

    # 缺料分支：结构化 demand 信号（铁律 2）。这是 ADR-0004 抽取需求的入口，不当作错误崩溃。
    if "needs_extraction" in payload:
        _report(progress, "design", "completed", "已形成链路设计，但发现前置能力缺口")
        _report(
            progress, "readiness", "waiting_user", "自动化条件尚未齐全，需要补充操作能力",
            blockers=[str(payload["needs_extraction"])],
            evidence_refs=[f"operation-registry:{app}", f"case:{case_id}"],
            next_actions=["打开操作库沉淀缺失能力", "完成后回到本用例重新生成执行草稿"],
        )
        return {"ok": False, "case_id": case_id,
                "needs_extraction": payload["needs_extraction"],
                "reason": payload.get("reason", ""),
                "case_context": _extraction_case_context(case)}

    steps = payload["steps"]
    _report(progress, "design", "completed", f"已设计 {len(steps)} 个 typed-IR 步骤")
    _report(progress, "readiness", "running", "正在检查动作、参数、副作用和断言")

    # fail-closed（静态闸）：每个 step 解析到已知 action/operation、闭集形态、参数名逐字等于 kwarg。
    # 这把内容 schema（只钉形态）挡不住的「名对值错 / 瞎编 verb / 引用未注册 operation」全拦在落库前。
    # _capability_brief 已 import 过 app recipes 包，OPERATIONS/ACTIONS 此刻就绪。
    from autophone.execution import effects
    from autophone.execution.engine.recipe import RecipeError, validate_recipe_for_app
    try:
        validate_recipe_for_app(steps, profile)
        inactive = sorted({step["operation"] for step in steps
                           if "operation" in step and step["operation"] not in active_library})
        if require_active_operations and inactive:
            raise RecipeError(f"operation not active: {', '.join(inactive)}")
    except (RecipeError, effects.EffectPolicyError) as e:
        _report(progress, "readiness", "failed", "候选链路未通过静态安全检查",
                blockers=[str(e)], next_actions=["修正非法动作、参数或副作用后重新生成"])
        return {"ok": False, "error": f"convert steps 不过 IR dispatcher 校验（拒绝写脏）：{e}"}

    # fail-closed（非空断言）：recipe 必须含 ≥1 个断言步，否则等于 vacuous pass（提前到落草稿时拦——
    # AI 转出来一条没断言的 recipe 是无效资产）。断言步 = assert_* action，或引用操作库中声明
    # case_assertion: true 的 Operation（BL-210）；解析不到或未声明的 Operation 不算，不按名字前缀推断。
    if not any(is_case_assertion_step(s, operation_library) for s in steps):
        _report(progress, "readiness", "failed", "候选链路没有可验证断言",
                blockers=["recipe 无 assert_* 步骤，也未引用声明 case_assertion 的 Operation"],
                next_actions=["补充可观察的预期结果"])
        return {"ok": False, "error": (
            "convert recipe 无断言步骤（无 assert_* action，也未引用操作库声明 case_assertion 的 "
            "Operation；vacuous，拒绝写脏）"
        )}

    _report(
        progress, "readiness", "completed", "静态检查通过：能力、参数、副作用与断言均可解析",
        evidence_refs=["typed-ir-schema", f"effect-policy:{app}", "assertion-nonempty"],
        next_actions=["保存为待人工复核的执行草稿"],
    )
    _report(progress, "draft", "running", "正在写入隔离的 recipe 草稿区")
    # 落草稿：recipes/drafts/<id>.yaml，status=draft + provenance。load_recipe 只读 steps，忽略额外键，
    # 故转正搬到 recipes/ 后这些键对 dispatcher 无害；保留它们让 review 看清这是 AI 转写、源自哪条用例。
    prov_note = f"AI convert via {res.provider}" + (f"/{res.model}" if res.model else "")
    if note:
        prov_note += f"; {note}"
    try:
        references = {step["operation"] for step in steps if "operation" in step}
        with asset_store.path_write_lock(operation_path) if operation_path and references else nullcontext():
            if references:
                try:
                    current = _read_operation_library(operation_path, allow_missing=ops_path is None)
                except (OperationError, OSError, yaml.YAMLError, AttributeError, TypeError) as exc:
                    raise OperationError(f"operation registry unreadable during conversion: {exc}") from exc
                changed = sorted(name for name in references
                                 if current.get(name) != operation_library.get(name))
                if changed:
                    raise OperationError(f"operation changed during conversion: {', '.join(changed)}")
            result = write_recipe_draft(
                app,
                case_id,
                steps,
                recipes_dir=recipes_dir,
                provenance={"source": "conversation", "from": f"case:{case_id}", "note": prov_note},
            )
    except OperationError as exc:
        _report(progress, "readiness", "failed", "操作库已变化，未保存执行草稿",
                blockers=[str(exc)], next_actions=["重新读取操作库并生成草稿"])
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        _report(progress, "draft", "failed", "执行草稿写入失败",
                blockers=[str(exc)], next_actions=["检查草稿目录与 recipe 校验错误"])
        raise
    if result.get("ok"):
        _report(
            progress, "draft", "completed", f"已生成 {len(steps)} 步执行草稿，尚未进入正式回归",
            evidence_refs=[str(result.get("path") or "")],
            next_actions=["人工复核草稿", "在目标 Build 上试运行并收集证据"],
        )
    else:
        _report(progress, "draft", "failed", "执行草稿未写入",
                blockers=[str(result.get("error") or "unknown draft write error")])
    return result
