"""页面 × API 影响图谱。

图谱不是静态的「页面清单 + 接口清单」，而是执行证据的投影：

    后端配置/数据变化 → 接口响应字段 → 消费端页面状态与区域
      → 到达页面的登录会话/门店/履约模式/operation 路径 → API 差异 + 截图 + 视觉差异

分层（每层一个模块，边界即真源边界）：

- ``endpoints``  —— endpoint identity 标准化：METHOD + host_alias + path_template，
  真实门店/商品/账号/订单 ID 一律模板化，不进 identity。
- ``context``    —— ConsumerContext 归一 + 脱敏；页面身份（screen_id）与消费上下文
  （variant）分离，variant_key 是覆盖前沿的去重键。
- ``observations`` —— 把 action mark、API capture、前后 screen checkpoint 和上下文
  **确定性**合并成 append-only 的迁移事实；只描述发生了什么，不声称因果。
- ``bindings``   —— 字段 → 页面区域的人审关系登记表 + 置信度闸（correlated→causal
  必须有控制变量实验或后端权威契约）。
- ``index``      —— 由资产 + 观测派生的可重建投影，允许删除重建。
- ``manifest``   —— 后端变更清单摄取与输入能力分级（缺 JSONPath 显式降级）。
- ``planner``    —— 影响闭包 → 目标 → 最短安全路径 → impact-plan.v1。
- ``admission``  —— Prod 安全闸：默认只读，未获准 effect 落 effect_not_admitted。
- ``result``     —— verdict 评估（「API 变了 UI 没变」是有效结论）。
- ``report``     —— 自包含 HTML 报告。

治理：raw body / raw 帧不进图谱资产，只留标准化结构与脱敏摘要。
"""
from __future__ import annotations

from autophone.observation.impact.endpoints import (
    EndpointKey,
    endpoint_key,
    host_alias,
    normalize_endpoint,
    path_template,
)

__all__ = [
    "EndpointKey",
    "endpoint_key",
    "host_alias",
    "normalize_endpoint",
    "path_template",
]
