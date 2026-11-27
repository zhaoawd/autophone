"""ConsumerContext 与 ScreenVariant（影响图谱 §6.1 / §6.2 / §7.2）。

页面身份和消费上下文必须分离：同一个「菜单」骨架会因为履约模式、门店、时段、会员
权益、库存和购物车状态呈现完全不同的样子。把每张截图当一个页面节点，营销位、价格、
倒计时、订单号和会员资产会立刻把图炸开。

所以：``screen_id`` 由 screen skeleton 匹配决定，其余全部落进归一化 ``variant``；
``(build, screen_id, variant_key)`` 是覆盖前沿的去重键。

上下文里的账号、手机号、地址、订单号、会员码、资产数值和精确坐标一律按敏感处理——
进资产和报告前先脱敏，图谱只需要「是哪一类状态」，不需要「是谁的哪一单」。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

REDACTED = "redacted"

# variant 的轴：只放会改变页面呈现的归一化状态，不放业务对象真值
VARIANT_AXES: tuple[str, ...] = (
    "login_state",
    "location_state",
    "center_tab_mode",
    "fulfillment_mode",
    "store_ref",
    "member_tier",
    "membership_status",
    "cart_state",
    "order_state",
)

# 敏感字段名（小写子串匹配）：命中即脱敏，不论嵌在上下文哪一层
_SENSITIVE_HINTS: tuple[str, ...] = (
    "account", "phone", "mobile", "tel", "address", "addr",
    "token", "auth", "cookie", "session_id", "sid", "openid", "uid", "user_id",
    "member_code", "barcode", "qrcode", "qr_code", "card_no", "cardno",
    "balance", "vgold", "v_gold", "coupon_code", "coupons", "amount", "asset",
    "order_no", "order_num", "ordernum", "order_id", "orderid",
    "lat", "lng", "latitude", "longitude", "coordinate", "geo",
    "name", "nickname", "avatar",
)
# 明确不敏感的例外（名字里带 name/lat 但语义是分类而非身份）
_SENSITIVE_EXEMPT: frozenset[str] = frozenset({
    "screen_id", "store_ref", "coordinate_fixture", "display_name",
    "benefit_catalog_version", "path_template", "host_alias", "endpoint_key",
})


def is_sensitive_key(key: str) -> bool:
    name = str(key or "").strip().lower()
    if not name or name in _SENSITIVE_EXEMPT:
        return False
    return any(hint in name for hint in _SENSITIVE_HINTS)


def redact(value: Any) -> Any:
    """深度脱敏：命中敏感键名的叶子换成 ``redacted``，结构原样保留。

    结构保留是刻意的——报告仍要说明「余额这一项存在且是数值」，只是不说是多少。
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                out[str(key)] = REDACTED if not isinstance(item, (dict, list)) else redact(item)
            else:
                out[str(key)] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_variant(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """任意上下文投影 → 归一 variant：只留已声明的轴，值收敛成标量。

    未声明的轴被丢弃而不是塞进去——variant 轴是有限闭集，新增一条轴是需要评审的
    建模决定，不是运行期想加就加的自由字段。
    """
    src = dict(raw or {})
    out: dict[str, Any] = {}
    for axis in VARIANT_AXES:
        if axis in src and src[axis] is not None:
            out[axis] = _scalar(src[axis])
    return out


def variant_key(variant: Mapping[str, Any] | None) -> str:
    """归一 variant → 稳定字符串键（覆盖前沿去重用）。空 variant → ``*``。"""
    items = normalize_variant(variant)
    if not items:
        return "*"
    return ";".join(f"{key}={items[key]}" for key in sorted(items))


def coverage_key(build: str, screen_id: str, variant: Mapping[str, Any] | None) -> str:
    """``(build, screen_id, normalized_variant)`` —— §7.2 的探索去重键。"""
    return f"{build}|{screen_id}|{variant_key(variant)}"


def variant_matches(variant: Mapping[str, Any] | None, selector: Mapping[str, Any] | None) -> bool:
    """页面状态是否落在 binding 的 variant_selector 里。

    selector 是子集匹配：空 selector 命中该 screen 的所有 variant；selector 里
    值为 ``None`` 的轴表示「该轴必须存在，取值不限」。selector 提到但 variant 没有
    的轴视为不匹配——宁可漏报一个目标，也不要把结论扩到没证据的页面状态上。
    """
    want = dict(selector or {})
    if not want:
        return True
    have = normalize_variant(variant)
    for axis, expected in want.items():
        if axis not in have:
            return False
        if expected is None:
            continue
        if _scalar(expected) != have[axis]:
            return False
    return True


def variant_from_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """ConsumerContext（§6.2 的分节结构）→ 归一 variant 投影。

    只读取已知分节的已知字段；上下文里其他内容（账号、坐标 fixture、资产快照）不进
    variant——那些是「怎么到达」的输入，不是「页面长什么样」的身份。
    """
    ctx = dict(context or {})
    app = dict(ctx.get("app") or {})
    session = dict(ctx.get("session") or {})
    location = dict(ctx.get("location") or {})
    store = dict(ctx.get("store") or {})
    journey = dict(ctx.get("journey") or {})
    membership = dict(ctx.get("membership") or {})
    cart = dict(ctx.get("cart") or {})
    order = dict(ctx.get("order") or {})
    projected = {
        "login_state": session.get("login_state"),
        "location_state": location.get("permission"),
        "center_tab_mode": app.get("center_tab_mode"),
        "fulfillment_mode": journey.get("fulfillment_mode"),
        "store_ref": store.get("store_ref"),
        "member_tier": membership.get("tier"),
        "membership_status": membership.get("status"),
        "cart_state": cart.get("state"),
        "order_state": order.get("state"),
    }
    return normalize_variant(projected)


def normalize_context(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """ConsumerContext 归一 + 脱敏，落 ``impact/consumer-context.json`` 用。"""
    ctx = redact(dict(raw or {}))
    ctx["variant"] = variant_from_context(raw)
    return ctx


def context_satisfies(
    context: Mapping[str, Any] | None,
    requires: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """当前上下文是否满足一条 operation 边的 ``navigation.requires``。

    返回 ``(ok, missing)``；``missing`` 列出不满足的条目，供 plan 里说明为什么这条
    路径不可用（比 route_unavailable 一个字更能定位问题）。
    """
    want = dict(requires or {})
    if not want:
        return True, []
    raw_variant = (context or {}).get("variant")
    have = (
        normalize_variant(raw_variant)
        if isinstance(raw_variant, Mapping)
        else variant_from_context(context)
    )
    flat = dict(have)
    # requires 允许直接引用上下文里的布尔事实（如 store_selected）
    store = dict((context or {}).get("store") or {})
    flat.setdefault("store_selected", bool(store.get("store_ref")))
    missing: list[str] = []
    for key, expected in want.items():
        actual = flat.get(key)
        if expected is None:
            if actual is None:
                missing.append(key)
            continue
        if _scalar(expected) != _scalar(actual):
            missing.append(f"{key}={expected}(actual={actual})")
    return (not missing), missing


def merge_required_context(items: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """多个目标的所需上下文合并；同轴冲突保留冲突标记，交给 planner 拆 run。"""
    merged: dict[str, Any] = {}
    conflicts: set[str] = set()
    for item in items:
        for key, value in normalize_variant(item).items():
            if key in merged and merged[key] != value:
                conflicts.add(key)
            merged.setdefault(key, value)
    if conflicts:
        merged["_conflicts"] = sorted(conflicts)
    return merged
