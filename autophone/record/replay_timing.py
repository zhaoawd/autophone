"""回放节奏（倍速 + 步间停顿 dwell）的纯逻辑。

回放本质不是视频播放，而是把 typed-IR steps 经 dispatcher 逐步在真机上**重放执行**
（`operation_run._exec_typed_ir`）。每个 tap 之后已有自适应 `_settle`（轮询到两帧一致）保
正确性——本模块**只算「dispatch 完该睡多久」**，绝不碰 settle / 断言 / pass-fail。倍速纯
观感 + 取数窗口，对裁决零影响（record-replay-design §倍速回放）。

「原速(1x)」= 重放录制时人操作的真实墙钟间隔；倍速缩放它（2x→一半，0.5x→两倍）。

数据流（不写 sidecar 文件，trace.json 本就是真源）：
  录制 trace 每个 event 带 `t`（秒，相对会话起点，**after-shot 之后**记录，即「动作 i 完成
  时刻」）。lift 是 1 event → 1 step，故 post_gaps 与 steps **位置对齐、等长**。dispatch 完
  step i 后该睡的是 `t[i+1] - t[i]`（**post-gap**，人在两个动作之间的停顿），最后一步无后继
  → post_gap = 0（仍经 DWELL_MIN_MS 留一个取数窗）。回放时按 operation.provenance.from 找
  trace 重算。

对齐闸（fail-open）：操作落库后 steps 可被人编辑（增删步），trace 事件数与当前 step 数失配
→ **丢弃 timing**，回放退化成均匀 DWELL_MIN_MS 节奏。timing 只影响停顿时长，失配最坏是节奏
不对，**绝不会让 dispatch 跑错**——故 fail-open 安全（与录屏/抓取证据同纪律）。

归位（BL-014 执行段第二轮，2026-09-10）：BL-012 时因函数体内 import
`platform.recordings` 而连带留守；`recordings` 已归位 `autophone/record/recordings.py`，
本模块随之由 `platform/replay_timing.py` 搬到本路径。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# 倍速台阶：0.5x–4x，每 0.5 一档，默认原速。
SPEED_MIN = 0.5
SPEED_MAX = 4.0
SPEED_STEP = 0.5
DEFAULT_SPEED = 1.0

# 单个 post-gap 上限：人录制时中途走神停了 30s，不该原样重放。
GAP_CAP_MS = 3000.0
# dwell 下限：就算 post_gap=0（最后一步 / 失配退化）也留一个取数窗口（抓 after-shot、后续接
# API/元素取数）。上限 = GAP_CAP / SPEED_MIN，0.5x 慢放 3s gap 也不会无限拖。
DWELL_MIN_MS = 100.0
DWELL_MAX_MS = GAP_CAP_MS / SPEED_MIN  # = 6000.0


def allowed_speeds() -> list[float]:
    """合法倍速档位列表（schema enum / UI 选择器共用）：[0.5, 1.0, ..., 4.0]。"""
    n = round((SPEED_MAX - SPEED_MIN) / SPEED_STEP)
    return [round(SPEED_MIN + i * SPEED_STEP, 1) for i in range(n + 1)]


def validate_speed(value: Any) -> float:
    """把入参归一成合法倍速。None → 默认原速；非 0.5 台阶 / 越界 → ValueError（fail-closed，
    由上层包成 ContractError）。倍速是 executor 控制项，必须显式合法，不静默吞成 1x。"""
    if value is None:
        return DEFAULT_SPEED
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"replay_speed 不是数字：{value!r}") from exc
    allowed = allowed_speeds()
    # 浮点比较容差：0.1 步内即认作命中某档（0.5/1.0/.../4.0）。
    if not any(abs(speed - s) < 1e-6 for s in allowed):
        raise ValueError(
            f"replay_speed={value!r} 非法，必须是 {SPEED_MIN}x–{SPEED_MAX}x 间每 {SPEED_STEP} 一档：{allowed}"
        )
    # 归一到精确档位值（消除 1.4999999 之类的浮点尾巴）。
    return next(s for s in allowed if abs(speed - s) < 1e-6)


def compute_post_gaps(
    trace: dict[str, Any],
    *,
    step_count: int | None = None,
    step_events: list[int | None] | None = None,
) -> list[float] | None:
    """trace → 每步的 post-gap（毫秒，与 steps 位置对齐、等长）。

    两种对齐口径：

    · **有 step_events**（落库时写进 provenance 的 step→event 映射，见 record_lift）：
      按映射取时刻，`gaps[i] = clamp((t[e(i+1)]-t[e(i)])*1000, 0, GAP_CAP)`。合并步、合成步
      （映射为 None）以及末步一律 0——**逐步 fail-open**，一两步对不上不牵连其余步的节奏。
    · **无映射**（旧 operation）：退回「1 event → 1 step」的等长假设，失配即整块丢弃。

    任一 event 缺 `t` / 空事件 → None（fail-open，调用方退化成均匀 DWELL_MIN_MS）。
    """
    events = trace.get("events") or []
    if not events:
        return None
    times: list[float] = []
    for ev in events:
        t = ev.get("t")
        if not isinstance(t, (int, float)):
            return None  # 缺时间戳（旧 trace / 异常）→ 整条丢弃，不猜
        times.append(float(t))
    if step_events is not None:
        if step_count is not None and len(step_events) != step_count:
            return None  # 映射与当前 steps 对不上（落库后被增删）→ 丢弃，宁可均匀也不错位
        gaps = []
        for i, current in enumerate(step_events):
            nxt = step_events[i + 1] if i + 1 < len(step_events) else None
            if (
                not _is_index(current, len(times))
                or not _is_index(nxt, len(times))
            ):
                gaps.append(0.0)
                continue
            gaps.append(_clamp((times[nxt] - times[current]) * 1000.0, 0.0, GAP_CAP_MS))
        return gaps
    if step_count is not None and step_count != len(events):
        return None  # 落库后 steps 被增删，gap 已对不齐 → 丢弃，宁可均匀也不错位
    gaps = []
    for i in range(len(times)):
        if i == len(times) - 1:
            gaps.append(0.0)  # 末步无后继动作，无 post-gap（仍经 DWELL_MIN 留取数窗）
            continue
        delta_ms = (times[i + 1] - times[i]) * 1000.0
        gaps.append(_clamp(delta_ms, 0.0, GAP_CAP_MS))
    return gaps


def _is_index(value: Any, size: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size


def dwell_ms(post_gap_ms: float, speed: float) -> float:
    """倍速缩放后的实际停顿（毫秒）：clamp(post_gap / speed, [DWELL_MIN, DWELL_MAX])。

    倍速只缩放这个 dwell——settle 是自适应正确性闸门，绝不被倍速碰。"""
    if speed <= 0:
        speed = DEFAULT_SPEED
    return _clamp(post_gap_ms / speed, DWELL_MIN_MS, DWELL_MAX_MS)


def infer_step_events(trace: dict[str, Any], steps: list[Any] | None) -> list[int | None] | None:
    """给 `provenance.step_events` 之前落库的 operation 补一张映射（只补能证明的那种）。

    这不是猜：落库唯一会**多加**一步的地方是「补上录制声明的起始复位算子」
    （`recordings._steps_with_start`），而录制自己在 trace header 里写着 `starts_with` 是什么。
    所以当且仅当「首步恰好是 `{'operation': <trace 的 starts_with>}`、其余步数与事件数相等」时，
    映射必然是 `[None, 0, 1, …]`。差一步以上、首步对不上 → 返回 None（老实丢弃）。
    """
    events = trace.get("events") or []
    if not isinstance(steps, list) or not events:
        return None
    if len(steps) == len(events):
        return list(range(len(events)))
    starts_with = str(((trace.get("start") or {}).get("starts_with") or "")).strip()
    if (
        starts_with
        and len(steps) == len(events) + 1
        and steps[0] == {"operation": starts_with}
    ):
        return [None, *range(len(events))]
    return None


def resolve_post_gaps(
    recording_from: str | None,
    step_count: int,
    *,
    step_events: list[int | None] | None = None,
    steps: list[Any] | None = None,
    root: Path | None = None,
) -> list[float] | None:
    """按 operation.provenance.from（录制目录 repo-rel 路径）找 trace.json 重算 post-gaps。

    `step_events` 来自同一条 operation 的 `provenance.step_events`（落库时写的 step→event 映射）；
    给了就按映射对齐（合并/合成步不再毁掉整条 timing），没给则退回等长假设。

    无 from / 解析不出 / 无 trace / 缺时间戳 / 与 step_count 失配 → None（fail-open，回放退化成
    均匀 DWELL_MIN 节奏）。路径越界等安全校验复用 recordings.resolve_recording_id。"""
    if not recording_from:
        return None
    from autophone.record import recordings

    try:
        directory = recordings.resolve_recording_id(
            recording_from, **({"root": root} if root is not None else {})
        )
        trace_path = directory / "trace.json"
        if not trace_path.exists():
            return None
        import json

        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 timing 是 advisory，找不到/坏了 → fail-open，绝不拖垮回放
        return None
    if step_events is None:
        step_events = infer_step_events(trace, steps)
    return compute_post_gaps(trace, step_count=step_count, step_events=step_events)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
