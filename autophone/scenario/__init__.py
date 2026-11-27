"""接口场景拦截（API Scenario Interception）—— host 侧控制面。

设计：docs/api-scenario-interception.md（第一期 spike 落地）。
在 Phase 4「进程内只读抓取」之上补一层**受控接口拦截**，让测试同学造
「固定 JSON 返回 / 立即超时」场景。复用同一条 Frida 通道、同一个 net_tap agent。

三块：
  - ``rules``：规则模型 + 静态校验 + host 侧编译（纯函数、可测）+ 快照 hash + 环境闸。
  - ``controller``：``ScenarioController`` 深模块（validate/preflight/start/status/close），
    两段式启动（LaunchPlan 启动前可知 / ScenarioSession 启动后事实）+ fail-closed 三闸。
  - ``session``：把编译好的规则接进 net_tap agent 的启动时序（规则先于 resume，§六）。
  - ``runtime``：跨 HTTP 请求持有 controller session + ScreenLock，收口 plan/start/status/close/TTL。
  - ``presets``：通用接口场景的不可变规则版本与原子 YAML 真源。
"""
from __future__ import annotations

from autophone.scenario.presets import (
    ScenarioPresetConflict,
    ScenarioPresetError,
    ScenarioPresetNotFound,
    ScenarioPresetStore,
)
from autophone.scenario.rules import (
    CompiledScenario,
    Diagnostic,
    ScenarioError,
    compile_rules,
    env_gate,
    rule_snapshot_hash,
    validate,
    validate_or_raise,
)
from autophone.scenario.runtime import (
    ScenarioDeviceBusy,
    ScenarioPlanNotFound,
    ScenarioRuntime,
    ScenarioRuntimeError,
    ScenarioSessionNotFound,
)

__all__ = [
    "CompiledScenario",
    "Diagnostic",
    "ScenarioError",
    "ScenarioPresetConflict",
    "ScenarioPresetError",
    "ScenarioPresetNotFound",
    "ScenarioPresetStore",
    "ScenarioDeviceBusy",
    "ScenarioPlanNotFound",
    "ScenarioRuntime",
    "ScenarioRuntimeError",
    "ScenarioSessionNotFound",
    "compile_rules",
    "env_gate",
    "rule_snapshot_hash",
    "validate",
    "validate_or_raise",
]
