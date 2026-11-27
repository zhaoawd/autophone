"""控制面回给 Rig Agent 的 ack 字段契约（BL-083 第三轮）。

2026-10-11 vm-alt 上 `food_ordering` 一个 Run 完成后，控制面把 `complete_assignment` 返回的整条
Assignment 投影（`assignments.result` 与 `attempt.result` 各一份完整 Assignment Result，含
518 KB 的 `artifact_refs`）放进 ack 回发，ack 约 1.41 MB，超过 Agent 侧 websocket 客户端默认
1 MiB 接收上限；连接以 1009 关闭，Agent 判定「完成未确认」后退出。控制面此时已写入终态。

Agent 只读 ack 中下列字段，因此控制面只回这些字段：

- `heartbeat`、`stage_result`、`complete`、`decline`、`attempt_interrupted`（BL-240）返回 Assignment 记录。Agent 读
  `status`（心跳据此看到 `cancel_requested`；完成与拒单写日志）与
  `result.product_verdict`、`result.rig_disposition`、`result.reason`（完成日志与
  `device_quiesce_unverified` 判定）。ack 只带 `assignment_id`、`status` 与终态结果的这几个
  标量字段，大小不随 Run Result、`artifact_refs`、Stage Result 增长。
- `assignment_observed_state`、`observed_state` 返回 Rig 记录。安装发布核对
  `host_id`、`rig_id`、`generation`、`observed_state`；`observed_state` 更新的 ack 未被读取，
  同样只回这四个字段。
- `validate_write`（7 个标量字段）、`read_observed_state`（被读取的观测本身）与
  `screen_quarantine`（Agent 上报状态的回显）原样返回：内容固定或由 Agent 自身输入决定，
  不随 Run 增长。
- `sync` 与 `assignment` 通知不是 ack：Agent 要执行其中的 Assignment，需要完整记录。
"""
from __future__ import annotations

from typing import Any, Mapping

ASSIGNMENT_ACK_KINDS = frozenset({
    "heartbeat", "stage_result", "complete", "decline", "attempt_interrupted",
})
ASSIGNMENT_ACK_FIELDS = ("assignment_id", "status")
ASSIGNMENT_RESULT_ACK_FIELDS = ("status", "product_verdict", "rig_disposition", "reason")

RIG_ACK_KINDS = frozenset({"assignment_observed_state", "observed_state"})
RIG_ACK_FIELDS = ("host_id", "rig_id", "generation", "observed_state")


def assignment_ack(record: Any) -> dict[str, Any]:
    """Assignment 记录收窄为 Agent 读取的字段；终态结果只保留标量摘要。"""
    source = record if isinstance(record, Mapping) else {}
    ack = {key: source[key] for key in ASSIGNMENT_ACK_FIELDS if key in source}
    terminal = source.get("result")
    if isinstance(terminal, Mapping):
        ack["result"] = {
            key: terminal[key] for key in ASSIGNMENT_RESULT_ACK_FIELDS if key in terminal
        }
    return ack


def rig_ack(record: Any) -> dict[str, Any]:
    source = record if isinstance(record, Mapping) else {}
    return {key: source[key] for key in RIG_ACK_FIELDS if key in source}


def agent_ack(kind: str, result: Any) -> Any:
    """按请求类型返回 ack 的 `result`；未列出的类型原样返回。"""
    if kind in ASSIGNMENT_ACK_KINDS:
        return assignment_ack(result)
    if kind in RIG_ACK_KINDS:
        return rig_ack(result)
    return result
