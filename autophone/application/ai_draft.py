"""ai_draft.py — AI-4 操作起草（platform-ai-capability §3.4）。

把「对话轨迹 / 需求 / 探索结果」交给本地 agent 起草成一条 operation recipe，落进
`apps/<app>/operations/<file>.yaml`，**`status: draft`**——AI 只在创建期在环，产物绝不直接进 active /
回归库。转正沿用既有冻结门（人审 + draft 连续 N 次 passed + app-aware 区分力闸 → `evaluate_promotion_gate`）。

两条铁律（§3.4 ⚠️）：
1. **provenance 用现有 enum**：`source: conversation`（对话轨迹冻结），provider/model 等 AI 来源打进
   `note`（provenance 是 `additionalProperties:false`，不能凭空加键，更不能写 `source: ai`）。
2. **产物是 draft**，落库即 `status=draft`，由本函数 + `append_operation` 管 id/revision/status。

operation 无 expects（断言属于用例，schema 层强制），故喂模型的内容 schema 只要 name/summary/params/steps。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, invoke
from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from autophone.assets.policy import assert_assets_writable
from autophone.assets import store as asset_store
from autophone.assets.apps import load_app_profile
from autophone.execution.app_recipes import import_app_recipes
from autophone.contracts import ContractError, validate_or_raise
from autophone.assets.operations_store import OPERATIONS_SPEC, append_operation
from autophone.assets.promotion import evaluate_promotion_gate, load_discrimination_proof


def _state_root_for(root: Path) -> Path:
    """Map the production code root to its separated mutable-state root."""
    return STATE_ROOT if root.resolve() == APP_ASSET_ROOT.resolve() else root

# 一个 typed IR step 的形态（镜像 operation.v1.json $defs/step 的两种结构化分支，丢掉 prose 分支
# ——AI 起草恒产 typed IR）。action 与 operation 互斥（oneOf 恰中其一）；语义层（参数名==kwarg、
# 值类型、operation 已注册）交给 recipe.validate_recipe 在落库前把关，schema 只钉形态。
_ACTION_STEP: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "not": {"required": ["operation"]},
    "properties": {"action": {"type": "string", "minLength": 1}},
}
_OPERATION_STEP: dict[str, Any] = {
    "type": "object",
    "required": ["operation"],
    "additionalProperties": False,
    "properties": {"operation": {"type": "string", "minLength": 1}, "args": {"type": "object"}},
}

# 可观测后置条件 witness（ADR-0001，Gap2）——镜像 operation.v1.json $defs/witness。选填：仅当
# 「用例期望 == 操作成功条件」的操作（如 login_negative：达成态=被拒、仍在登录页）才声明。声明了它，
# 该操作转正须过区分力闸（draft_operation 的 declares_witness 路径点亮，附 negctl 负控证据才 ready）。
# negative_control 是闸门读的负控契约：flip 哪个 param、翻成什么、witness 必须翻成的预测对偶 stage。
_WITNESS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "expect_stage"],
    "properties": {
        "kind": {"enum": ["ocr_markers", "ax_count"],
                 "description": "witness 观测方式；ocr_markers=OCR 标记集；ax_count=AX 节点计数与期望值比较"},
        "expect_stage": {"type": "string", "minLength": 1,
                         "description": "操作达成目标态时 witness 应报告的正向终态 stage（如 login_rejected）"},
        "markers": {"type": "array", "items": {"type": "string", "minLength": 1},
                    "description": "证明达成态的正向 OCR 标记集（任一命中即认达成）"},
        "negative_control": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flip_arg", "flip_to", "expect_stage"],
            "properties": {
                "flip_arg": {"type": "string", "minLength": 1,
                             "description": "被 flip 的 param 名（须是本操作已声明的 param）"},
                "flip_to": {"description": "flip 成的值（应产生相反结果的输入）"},
                "expect_stage": {"type": "string", "minLength": 1,
                                 "description": "flip 后 witness 必须翻成的预测对偶 stage（如 unexpectedly_logged_in）"},
            },
        },
    },
}

# 喂模型的「操作内容 schema」——只要业务字段；id/status/revision/provenance 由我们装配。
# 镜像 operation.v1.json $defs/operation 的内容子集 + $defs/param，additionalProperties:false 防漂移。
# steps 升级成 typed IR（§10.10）：每个 step 恰好 {action,…} 或 {operation,args} 二选一。
_OP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "summary", "steps"],
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$",
                 "description": "稳定语义名 snake_case"},
        "summary": {"type": "string", "minLength": 1},
        "params": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"enum": ["string", "int", "float", "bool", "enum"]},
                    "values": {"type": "array"},
                    "default": {},
                    "required": {"type": "boolean"},
                    "desc": {"type": "string"},
                },
            },
        },
        "steps": {"type": "array", "minItems": 1,
                  "items": {"oneOf": [_ACTION_STEP, _OPERATION_STEP]}},
        "screen_starts_with": {"type": "string", "minLength": 1},
        "screen_expectations": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
        },
        "witness": _WITNESS,
    },
}

# 开放参数、typed IR 和 witness 保留原内容 Schema；传输时编码为 JSON 字符串，
# 避免向 provider 发送 oneOf/not、开放对象或无类型字段。解码后仍过原内容与 dispatcher 校验。
_OP_TRANSPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "summary", "params_json", "steps_json", "witness_json",
                 "screen_starts_with", "screen_expectations_json"],
    "properties": {
        "name": _OP_SCHEMA["properties"]["name"],
        "summary": _OP_SCHEMA["properties"]["summary"],
        "params_json": {"type": ["string", "null"],
                        "description": "params 数组的 JSON 字符串；无参数时为 null"},
        "steps_json": {"type": "string",
                       "description": "typed IR steps 数组的 JSON 字符串；每步为 action 或 operation"},
        "witness_json": {"type": ["string", "null"],
                         "description": "witness 对象的 JSON 字符串；不声明时为 null"},
        "screen_starts_with": {"type": ["string", "null"]},
        "screen_expectations_json": {"type": ["string", "null"],
                                     "description": "screen_expectations 对象的 JSON 字符串；不用时为 null"},
    },
}


def _operation_content(payload: dict[str, Any]) -> dict[str, Any]:
    """还原传输字段；拒绝混合形态、非法 JSON 和未声明字段。旧内容形态保持兼容。"""
    if not any(key.endswith("_json") for key in payload):
        return {key: payload[key] for key in _OP_SCHEMA["properties"] if key in payload}
    from jsonschema import Draft202012Validator

    errors = list(Draft202012Validator(_OP_TRANSPORT_SCHEMA).iter_errors(payload))
    if errors:
        raise ValueError(f"operation transport schema: {errors[0].message}")
    out = {key: payload[key] for key in ("name", "summary", "screen_starts_with")
           if payload[key] is not None}
    for field in ("params", "steps", "witness", "screen_expectations"):
        raw = payload[f"{field}_json"]
        if raw is not None:
            try:
                out[field] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field}_json: invalid JSON ({exc.msg})") from exc
    return out


# 收录来源 provenance 的内容 schema——镜像 operation.v1.json $defs/provenance（additionalProperties:false）。
# 落库前单独校验它，因为 provenance 是**调用方**传入、且不在 _OP_SCHEMA（喂 AI 的内容 schema 故意不含
# provenance/id/status）里——只有 append 后的整库 validate 才兜得住它，等到那时库已半写脏。
_PROVENANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source"],
    "properties": {
        "source": {"enum": ["distilled", "conversation", "excel", "verification"]},
        "from": {"type": "string"},
        "run_id": {"type": "string"},
        "note": {"type": "string"},
        # 录制来源专用：steps[i] ← 录制事件下标（合成步为 null），供回放还原人手节奏。
        "step_events": {"type": "array", "items": {"type": ["integer", "null"], "minimum": 0}},
        "detached": {"type": "boolean"},
    },
}

_SYSTEM = (
    "你是 autophone 的操作起草 agent。把给定需求/对话轨迹冻结成一条可复用、目标导向、确定性的 operation recipe："
    "起一个 snake_case 的稳定 name、一句 summary、需要的入参 params，以及 **typed IR 步骤序列 steps**。\n"
    "steps 是闭集动作序列，每个 step 恰好二选一：\n"
    "  - {\"action\": <verb>, <参数名>: <值>, …}  调一个白名单 verb；参数名必须逐字等于该 verb 的 kwarg。\n"
    "  - {\"operation\": <name>, \"args\": {…}}      引用一条已注册的有状态操作（args 的键等于其 kwarg）。\n"
    "**禁止控制流**：不要在 steps 里写循环/条件/重试/状态归位——那类逻辑属于手写执行器（operation），"
    "不能下沉进 IR。若需求本质需要循环/分支，只能引用一条已有 operation，不要展开成动作序列。\n"
    "操作只描述「把事做成」，**不含断言/期望**（断言属于用例，别写 assert_*/校验类 step；要等元素就用 wait_text）。\n"
    "只能用下方「可用动作 / 可用操作」里列出的名字与参数，严格按签名（带 ? 的是可选参，其余必填）；不要瞎编 verb 或参数名。\n"
    "\n"
    "**witness（选填，少数情况）**：仅当本操作属于「用例期望 == 操作成功条件」那类——即操作的『成功』本身"
    "就是某条用例要验证的事（典型：负路登录 login_negative，成功=被拒、仍在登录页）——才声明 witness：\n"
    "  witness: {kind: <ocr_markers 或 ax_count>, expect_stage: <成功终态名>, markers: [<仅 OCR 观测填写屏上文字>…],\n"
    "            negative_control: {flip_arg: <把哪个 param 反转>, flip_to: <反转成的值>, expect_stage: <反转后必翻成的对偶终态>}}\n"
    "negative_control.flip_arg **必须**是上面 params 里声明过的参数名。绝大多数操作是纯 fixture（造单/上下岗/"
    "导航），**没有**可机器判定的成功终态，**不要**声明 witness——宁缺毋滥，编不出真实对偶终态就别写。"
)


def _capability_brief(
    app: str, *, root: Path = APP_ASSET_ROOT, operation_names: set[str] | None = None,
) -> str:
    """把 dispatcher 的白名单（recipe.ACTIONS / 该 app 注册的 OPERATIONS）连同各自 kwarg 签名
    渲染成给模型的清单——这是「把 LLM 摆到 dispatcher 前面」：模型据真实白名单产 typed IR，
    再由 validate_recipe 兜底，而非凭空猜 verb。import app recipes 包以填充 OPERATIONS（best-effort）。"""
    import inspect

    from autophone.execution.engine import recipe

    _ensure_app_ops(app, root=root)

    def _sig(fn: Any) -> str:
        params = list(inspect.signature(fn).parameters.values())[1:]  # 去掉首位 ctx/self
        kw = [p for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
        return ", ".join(p.name + ("" if p.default is inspect.Parameter.empty else "?") for p in kw) or "—"

    actions = "\n".join(f"  - {n}({_sig(fn)})" for n, fn in sorted(recipe.ACTIONS.items()))
    ops = "\n".join(
        f"  - {n}({_sig(fn)})"
        for n, fn in sorted(recipe.operations_for_app(app, root=root).items())
        if operation_names is None or n in operation_names
    ) or "  （该 app 暂无已注册操作）"
    try:
        from autophone.knowledge.catalog import KnowledgeCatalog

        catalog = KnowledgeCatalog(app, root=root)
        claims = [
            str(row.get("statement") or "")
            for row in catalog.claims()
            if row.get("status") == "active" and row.get("statement")
        ][:50]
    except Exception:  # noqa: BLE001 - an empty catalog must not block drafting
        claims = []
    knowledge = (
        "\n".join(f"  - {statement}" for statement in claims)
        if claims else "  （暂无 Active Knowledge Claim）"
    )
    return (
        f"可用动作 action（参数名即 kwarg，带 ? 为可选）：\n{actions}"
        f"\n\n可用操作 operation（args 的键即 kwarg）：\n{ops}"
        f"\n\n当前 Knowledge Catalog（仅 Active Claim）：\n{knowledge}"
    )


def _ensure_app_ops(app: str, *, root: Path = APP_ASSET_ROOT) -> None:
    """import `apps.<app>.recipes` 以把该 app 的操作执行器注册进 recipe.OPERATIONS。best-effort——
    没有 recipes 包的 app（只在 ACTIONS 白名单内起草）静默跳过。"""
    import_app_recipes(app, root=root)


def _build_prompt(requirement: str, source_text: str | None, brief: str) -> str:
    lines = [f"需求：{requirement}",
             "输出传输对象：name/summary 保持字符串；params_json/steps_json/witness_json/"
             "screen_expectations_json 分别编码为对应字段的 JSON 字符串，不使用的可选字段为 null；"
             "screen_starts_with 为字符串或 null。解码后须符合下述 Operation 内容契约。",
             "把它冻结成一条 operation recipe（name/summary/params/typed IR steps）。",
             "\n" + brief]
    if source_text:
        lines.append("\n以下是对话轨迹/探索记录，据此提炼步骤：\n" + source_text)
    return "\n".join(lines)


def draft_operation(
    app: str,
    requirement: str,
    *,
    source_text: str | None = None,
    from_ref: str | None = None,
    note: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 300.0,
    runner: CommandRunner = _subprocess_runner,
    ops_path: Path | None = None,
    required_passes: int = 3,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """起草一条操作并落库为 draft，返回 {ok, operation, gate, path} 或 {ok:False, error}。

    fail-closed：agent 没回结构化 JSON / 落库后整库不贴 operation.v1 → ok=False，不写脏数据。
    """
    assert_assets_writable("draft operation asset", path=ops_path)
    profile = load_app_profile(app, root=root)
    path = ops_path or profile.operations_path()
    if not path.exists():
        return {"ok": False, "error": f"operations registry not found: {path}（先建操作库再起草）"}

    req = AiRequest(
        prompt=_build_prompt(requirement, source_text, _capability_brief(app, root=root)),
        system=_SYSTEM,
        tools="none",
        json_schema=_OP_TRANSPORT_SCHEMA,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
    )
    res = invoke(req, runner=runner)
    if not res.ok or not isinstance(res.json, dict):
        return {"ok": False, "error": res.error or "draft agent returned no structured operation json"}

    try:
        fields = _operation_content(res.json)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # §3.4 铁律①：source 用现有 enum，AI 来源（provider/model）打进 note，绝不写 source: ai。
    prov_note = f"AI draft via {res.provider}" + (f"/{res.model}" if res.model else "")
    if note:
        prov_note += f"; {note}"
    provenance: dict[str, Any] = {"source": "conversation", "note": prov_note}
    if from_ref:
        provenance["from"] = from_ref
    if run_id:
        provenance["run_id"] = run_id

    return land_operation_draft(
        app,
        fields,
        provenance=provenance,
        ops_path=path,
        required_passes=required_passes,
        root=root,
    )


def _validate_operation_fields(
    app: str,
    fields: dict[str, Any],
    provenance: dict[str, Any],
    path: Path,
    *,
    exclude_self_id: str | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any] | None:
    """共享 fail-closed 闸：内容 schema / provenance schema / IR dispatcher / witness 负控 / active 重名。

    exclude_self_id 用于更新场景：校验 active 重名时排除自身（自己改名叫自己不算重名）。
    返回 None 通过；返回 {ok:False, error} 拒绝。
    """
    # fail-closed（审查 P3）：**落库前**先用「内容 schema」本地校验——否则非法字段（如 params.type=float）
    # 会先 append 进 YAML，再被整库 validate_or_raise 抛错，留下脏 draft。
    from jsonschema import Draft202012Validator
    schema_errors = sorted(Draft202012Validator(_OP_SCHEMA).iter_errors(fields), key=lambda e: list(e.path))
    if schema_errors:
        detail = "；".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in schema_errors[:5])
        return {"ok": False, "error": f"draft 内容不贴合 operation 内容 schema（拒绝写脏）：{detail}"}

    # fail-closed（同款写前校验）：provenance 是调用方传入、不在 _OP_SCHEMA 内，单独过 $defs/provenance
    # ——否则非法 provenance（如未知 source / 多余键）会 append 进库后才被整库 validate 抛错，留下脏 OP{n}。
    prov_errors = sorted(Draft202012Validator(_PROVENANCE_SCHEMA).iter_errors(provenance), key=lambda e: list(e.path))
    if prov_errors:
        detail = "；".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in prov_errors[:5])
        return {"ok": False, "error": f"provenance 不贴合操作收录来源 schema（拒绝写脏）：{detail}"}

    # fail-closed（②「把 LLM 摆到 dispatcher 前面」）：typed IR steps 必须过 dispatcher 的静态闸——
    # 每个 step 解析到已知 action/operation、闭集形态、参数名逐字等于 kwarg（无未知/无缺必填）、值类型正确。
    # 这把 _OP_SCHEMA（只钉形态）挡不住的「名对值错 / 瞎编 verb / 引用未注册 operation」全拦在落库前。
    # 录制提升的 steps 只含 tap_text/tap_xy（不引用 operation），但仍 _ensure_app_ops 以防引用到已注册操作。
    _ensure_app_ops(app, root=root)
    from autophone.execution import effects
    from autophone.execution.engine.recipe import RecipeError, operation_scope, validate_recipe_for_app
    try:
        with operation_scope(app, root=root):
            validate_recipe_for_app(
                fields["steps"],
                load_app_profile(app, root=root),
            )
    except (RecipeError, effects.EffectPolicyError) as e:
        return {"ok": False, "error": f"draft steps 不过 IR dispatcher 校验（拒绝写脏）：{e}"}

    # fail-closed（Gap2）：witness 若声明 negative_control，其 flip_arg 必须是本操作已声明的 param——
    # 否则闸门 flip 一个不存在的参数（apply_perturbation 仍会塞进 args，但执行器收不到 → 假 no-op），
    # 区分力闸会被一条引用幽灵 param 的负控声明骗过。镜像 recipe 静态闸「param 名必须等于 kwarg」的纪律。
    witness = fields.get("witness")
    if witness and (nc := witness.get("negative_control")):
        param_names = {p.get("name") for p in (fields.get("params") or [])}
        if nc["flip_arg"] not in param_names:
            return {"ok": False, "error": (
                f"witness.negative_control.flip_arg={nc['flip_arg']!r} 不是已声明 param"
                f"（已声明：{sorted(n for n in param_names if n)}）——拒绝写脏")}

    # fail-closed（审查 P4）：拒绝与库内 active 操作重名——一条 draft 复用 active name 会导致
    # 未来转正时冲突。draft 操作允许同名共存（试验阶段的多次迭代），仅 active 需要唯一性。
    import yaml as _yaml
    existing = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    active_names = {
        (op.get("name"), op.get("id"))
        for op in (existing.get("operations") or [])
        if op.get("status") == "active"
    }
    collisions = [op_id for name, op_id in active_names if name == fields["name"] and op_id != exclude_self_id]
    if collisions:
        return {"ok": False, "error": f"操作 name 与 active 操作重名，拒绝写入：{fields['name']}"}
    return None


def _draft_gate(app: str, op: dict[str, Any], *, root: Path,
                required_passes: int) -> dict[str, Any]:
    """创建期摘要按当前 App、Build、精确制品摘要、符号绑定与 Operation 契约身份读取区分力证据。"""
    from autophone.assets.operation_witness import operation_identity
    from autophone.assets.promotion_identity import current_discrimination_context

    profile = load_app_profile(app, root=root)
    declares_witness = bool(op.get("witness"))
    identity = operation_identity(app, op) if declares_witness else None
    state_root = _state_root_for(root)
    disc_ctx = (
        current_discrimination_context(
            profile,
            op_def=op,
            state_root=state_root,
        )
        if declares_witness
        else {
            "current_artifact_content_digest": None,
            "current_binding_sha256": None,
            "uses_symbol_binding": False,
        }
    )
    proof = (
        load_discrimination_proof(
            state_root / "runs" / "negctl", operation=op["name"],
            app=app, current_operation_identity=identity,
            preferred_build_label=profile.build_label,
            **disc_ctx,
        ) if declares_witness else None
    )
    return evaluate_promotion_gate(
        [], draft_id=op["id"], required_passes=required_passes,
        discrimination=proof, require_discrimination=declares_witness,
        current_build_label=profile.build_label, current_operation_identity=identity,
        **disc_ctx,
    )


def land_operation_draft(
    app: str,
    fields: dict[str, Any],
    *,
    provenance: dict[str, Any],
    ops_path: Path | None = None,
    required_passes: int = 3,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """把一组**已成形**的 operation 业务字段（name/summary/params?/steps/witness?）落库为 draft，
    返回 {ok, operation, path, gate} 或 {ok:False, error}。

    AI-4 起草（`draft_operation`）和录制提升（`record_lift`，M2）共用这一条落库路径——区别只在
    `fields`/`provenance` 谁来产（AI agent vs 人点录制 + lifter）。所有 fail-closed 闸都在这里收口，
    两条入口同一纪律、不各写一份：
      · 内容 schema（_OP_SCHEMA）——落库前先拦非法字段，否则脏 draft 先落盘再被整库校验抛错；
      · IR dispatcher 静态闸（validate_recipe）——瞎编 verb / 名对值错 / 引用未注册 operation；
      · witness.negative_control.flip_arg 必须是已声明 param；
      · 与库内任意状态操作重名（load_operations 对重名 fail-loud）；
      · append 后整库须贴 operation.v1（asset_store 不校验 schema 的兜底）。
    任一不过 → ok=False，不写脏。
    """
    assert_assets_writable("land operation draft", path=ops_path)
    path = ops_path
    if path is None:
        path = load_app_profile(app, root=root).operations_path()
    with asset_store.path_write_lock(path):
        if not path.exists():
            return {"ok": False, "error": f"operations registry not found: {path}（先建操作库再落库）"}

        err = _validate_operation_fields(
            app,
            fields,
            provenance,
            path,
            root=root,
        )
        if err is not None:
            return err

        land_fields = {
            k: fields[k]
            for k in (
                "name", "summary", "params", "steps", "screen_starts_with",
                "screen_expectations", "witness",
            )
            if k in fields
        }
        land_fields["provenance"] = provenance

        # §3.4 铁律②：落库即 draft。append 前先快照原文：append 后整库必须贴 operation.v1
        # （asset_store 不校验 schema 的兜底）；万一不过（理论上写前校验已覆盖所有 caller 输入），
        # **回滚到 append 前**再返回 ok=False——绝不留下半写脏库（OP{n} 已落、next_seq 已 ++）。
        import yaml as _yaml
        before_text = path.read_text(encoding="utf-8")
        op = append_operation(path, land_fields, status="draft")
        doc = _yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            validate_or_raise("operation.v1.json", doc)
        except ContractError as e:
            asset_store.atomic_write_text(path, before_text)  # 回滚到 append 前，库不留脏
            return {"ok": False, "error": f"落库对象不贴 operation.v1（已回滚，未写脏）：{e}"}

        # 起草仍无稳定运行或评价；返回当前候选身份下的门禁缺口。
        gate = _draft_gate(app, op, root=root, required_passes=required_passes)
        return {"ok": True, "operation": op, "path": str(path), "gate": gate}


def update_operation_draft(
    app: str,
    op_id: str,
    fields: dict[str, Any],
    *,
    provenance: dict[str, Any],
    ops_path: Path | None = None,
    required_passes: int = 3,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """更新一条已存在的 operation draft（录制重新提升的落库路径），返回 {ok, operation, path, gate} 或 {ok:False, error}。

    与 land_operation_draft 并列：同一套 fail-closed 闸（内容 schema / IR dispatcher / active 重名 / 整库 operation.v1），
    更新在操作库锁内先校验完整候选，再以一次原子替换写回；拒绝更新 active/archived（须先废弃）。
    draft/deprecated 都允许（deprecated 恢复为 draft，revision++）。
    """
    assert_assets_writable("update operation draft", path=ops_path)
    path = ops_path
    if path is None:
        path = load_app_profile(app, root=root).operations_path()
    with asset_store.path_write_lock(path):
        if not path.exists():
            return {"ok": False, "error": f"operations registry not found: {path}（先建操作库再更新）"}

        # 读取现有 operation，检查 status
        import yaml as _yaml
        existing = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        target = next((op for op in (existing.get("operations") or []) if op.get("id") == op_id), None)
        if target is None:
            return {"ok": False, "error": f"operation {op_id} not found"}

        status = str(target.get("status") or "").strip()
        if status in ("active", "archived"):
            return {"ok": False, "error": f"操作已转正（status={status}），请先废弃再重新提升"}

        err = _validate_operation_fields(
            app,
            fields,
            provenance,
            path,
            exclude_self_id=op_id,
            root=root,
        )
        if err is not None:
            return err

        changes = {
            k: fields[k]
            for k in (
                "name", "summary", "steps", "params", "screen_starts_with",
                "screen_expectations", "witness",
            )
            if k in fields
        }
        changes["provenance"] = provenance
        if status == "deprecated":
            changes["status"] = "draft"

        # 完整候选先过 Schema；内容与 provenance 一次写回，失败时无需覆盖整库回滚。
        target.update(changes)
        target["revision"] = int(target.get("revision") or 1) + 1
        try:
            validate_or_raise("operation.v1.json", existing)
        except ContractError as exc:
            return {"ok": False, "error": f"更新候选不贴 operation.v1（未写入）：{exc}"}
        try:
            op = asset_store.rewrite_item_fields(
                OPERATIONS_SPEC, path, op_id, changes, bump_revision=True,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        gate = _draft_gate(app, op, root=root, required_passes=required_passes)
        return {"ok": True, "operation": op, "path": str(path), "gate": gate}
