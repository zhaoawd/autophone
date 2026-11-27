"""CLI、HTTP 与后台 worker 的 Run 提交通道（BL-024，目标架构 §16.0 兼容路径退出第三步①）。

CLI 的 `run`、`replay`、`operation run`、`impact run` 原来都直接 `RunService.submit(...)`，
即单 VM 兼容路径；页面 `POST /runs` 在控制面已配置时提交 Assignment。同一条 Run 因此按入口
走两条路。本模块把「提交给谁」收成一处：

- `configured_control_plane()` 返回 ControlPlane，且这条 Run 需要真实设备：构造 `kind=run`、
  `requires_rig=true` 的 Work Plan 提交给 Scheduler，等 Assignment 走到终态，用 rig 回报的
  Run Result 组回 CLI 原样打印的那份 handle；
- 返回 ControlPlane，但这条 Run 不碰设备（只做 API / 解析 / 报告的 Host-only 工作）：按
  §16.0 的 `requires_rig` 两类路由不创建 Rig Assignment，在本进程执行。控制面的
  `submit_assignment` 也拒收 `requires_rig=false` 的 Work Plan；
- 返回 `None`（`AUTOPHONE_DATABASE_URL` 未设，即控制面未配置）：走兼容路径，行为与本轮之前
  一致。

**没有第四条路**：DSN 已设但 PostgreSQL 连不上时 `configured_control_plane()` 抛异常，本模块
不接也不回退——「已配置但不可用」静默落回兼容路径正是 BL-026 要消除的行为。

本模块不 import `autophone.platform`：兼容路径的执行者由调用方注入（CLI 注入 `RunService`），
只按 `RunSubmissionBackend` 的三个方法取用。

Work Plan 的 `required_capabilities` 由 `run_capabilities()` 按 Run 类型与 `planes.api` 取值
（BL-025 回填），不再是空集；标识与 rig agent 注册的 `capability_support` 同一套。

**单机前提**：Assignment 终态回来后，run 目录由 backend 按 `run_id` 在本机 run root 下解析。
当前控制面只调度本机 rig，rig agent 与 CLI 进程共享文件系统，该前提成立；跨主机调度要先把
产物搬回提交侧，未做，与 §16.0 第二步的同名前提一起登记。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol


#: 三类 Run 的 Assignment 类别，与 `delivery`、`provisioning` 并列。
RUN_ASSIGNMENT_KIND = "run"

#: 每种 Run 类型要求执行它的 Rig 具备哪些能力（BL-025 回填）。标识与 rig agent 注册进
#: Control Plane 的 `capability_support` 是同一套（`scripts/start_vphone.sh` 的
#: `RIG_CAPABILITIES`、`autophone/workflow/coordinator/device_work.py` 的
#: `DEVICE_WORK_CAPABILITIES`），不另造命名体系：
#:
#: - `vm_uds`：UDS 设备面（截图 / tap / shell）。走到这里的 Run 都过了
#:   `requires_device()`，即会竞争设备 ScreenLock，三类都要；
#: - `app_install` 不列：Run Spec 里没有装包声明（`run-spec.v1.json` 无 build 字段），
#:   装包是 `app package install` 自己的 Assignment；
#: - `frida` 不写死在这张表里，按 Run Spec 的 `planes.api` 逐条追加（见
#:   `run_capabilities()`）——写死会让 `SKIP_FRIDA=1` 起的 rig 匹配不上任何 Run。
#:
#: `explore` 不在表里：rig agent 的执行器工厂（`autophone.execution.handlers`）没有它的
#: handler，派过去必然在 rig 侧失败，不如在提交侧 fail-closed。
RUN_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "regression": ("vm_uds",),
    "verification": ("vm_uds",),
    "operation": ("vm_uds",),
}

#: `planes.api.mode` 取这个值时这条 Run 真的要抓 API，执行它的 rig 必须有 frida 隧道。
_API_PLANE_REQUIRED = "required"

#: Assignment 走到终态的默认上限与轮询间隔（秒）。上限按小时给：一条 regression 可以跑很久，
#: 而 CLI 的原兼容路径是同步执行、本来就没有上限。
RUN_ASSIGNMENT_TIMEOUT_S = 7200.0
RUN_ASSIGNMENT_POLL_INTERVAL_S = 2.0

#: 与 `autophone.control.postgres` 同一份终态集合，在这里显式列出以免反向依赖它的私有名。
_TERMINAL_ASSIGNMENT_STATUSES = frozenset(
    {"passed", "failed", "error", "blocked", "cancelled", "indeterminate"},
)

#: Assignment 没跑到 Run 就终止时，终态到 Run 状态的对照。CLI 的退出码表
#: （`autophone.execution.contracts.regression_exit_code`）只认 6 个状态，`cancelled` 与
#: `indeterminate` 没有对应位：前者按「未执行」记 blocked，后者按「结果不可判」记 error。
_ASSIGNMENT_STATUS_AS_RUN_STATUS = {
    "cancelled": "blocked",
    "indeterminate": "error",
}


class RunDispatchError(RuntimeError):
    """派单通道本身不成立（Assignment 迟迟不到终态、目标 Rig 隔离等），带稳定 ``code``。"""

    def __init__(
        self, code: str, message: str, *, assignment_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.assignment_id = assignment_id
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"refused": self.code, "detail": str(self), **self.details}


#: 控制面拒收提交时带稳定 code 的错误（BL-224 `screen_quarantined`）。经本通道提交的入口
#: 把它们转成 `RunDispatchError`，CLI 与 HTTP 据 code 给结构化拒绝，不提交 Assignment。
_CONTROL_REFUSAL_CODES = frozenset({"screen_quarantined"})


def run_capabilities(spec: Mapping[str, Any]) -> list[str]:
    """这条 Run 要求的 Rig 能力；不认识的 Run 类型 fail-closed（BL-025）。

    不回退成空集：空集会让任何 rig 都匹配得上，把「不认识的 Run」派到设备上去。
    改这里的标识必须同时改 `scripts/start_vphone.sh` 的 `RIG_CAPABILITIES`——只改一边，
    Assignment 会停在 `wait_reason=capability_support_insufficient`，表现为永久排队。
    """
    run_type = str(spec.get("type") or "")
    try:
        capabilities = list(RUN_CAPABILITIES[run_type])
    except KeyError:
        raise RunDispatchError(
            "run_type_unsupported",
            f"cannot dispatch Run type {run_type!r}: no Rig capability set is declared for it",
        ) from None
    planes = spec.get("planes")
    api_plane = planes.get("api") if isinstance(planes, Mapping) else None
    if isinstance(api_plane, Mapping) and str(api_plane.get("mode") or "") in {_API_PLANE_REQUIRED, "controlled"}:
        capabilities.append("frida")
    return capabilities


class RunSubmissionBackend(Protocol):
    """兼容路径的执行者，也是 run 目录与 handle 形态的持有者（CLI 注入 `RunService`）。"""

    def submit(self, spec: dict[str, Any]) -> Any:
        """在本进程内同步跑完一条 Run，返回 handle。"""

    def requires_device(self, spec: Mapping[str, Any]) -> bool:
        """这条 Run 是否需要真实设备，即 Work Plan 的 `requires_rig` 取值。"""

    def handle_for(self, run_id: str, result: Mapping[str, Any]) -> Any:
        """按 `run_id` 与已知 Run Result 组一个 handle（派单路径回读用）。"""


class RunDispatcher:
    """提交方的控制面路由；同步等待或返回带 Assignment 标识的排队 handle。"""

    def __init__(
        self,
        backend: RunSubmissionBackend,
        *,
        timeout_s: float = RUN_ASSIGNMENT_TIMEOUT_S,
        poll_interval_s: float = RUN_ASSIGNMENT_POLL_INTERVAL_S,
    ) -> None:
        self.backend = backend
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)

    def submit(
        self, spec: Mapping[str, Any], *, background: bool | None = None,
        telemetry_identity: dict[str, Any] | None = None,
        assignment_options: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        pin_device: bool = False,
    ) -> Any:
        from autophone.rig.source_identity import SourceProcessStale, assert_process_source_current

        try:
            assert_process_source_current()
        except SourceProcessStale as exc:
            raise RunDispatchError(exc.code, str(exc)) from exc
        planes = spec.get("planes")
        api_plane = planes.get("api") if isinstance(planes, Mapping) else None
        if isinstance(api_plane, Mapping) and api_plane.get("mode") == "controlled":
            from autophone.observation import capture

            protocol = getattr(capture, "CONTROLLED_CAPTURE_PROTOCOL", None)
            if type(protocol) is not int or protocol != 1:
                raise RunDispatchError(
                    "controlled_capture_unavailable",
                    "controlled capture protocol is unavailable",
                )
        from autophone.application.control_plane import configured_control_plane

        control = configured_control_plane()
        if control is None or not self.backend.requires_device(spec):
            kwargs = {} if background is None else {"background": background}
            if telemetry_identity is not None:
                kwargs["telemetry_identity"] = telemetry_identity
            return self.backend.submit(dict(spec), **kwargs)
        validate_dispatch_telemetry(telemetry_identity)
        from autophone.contracts import validate_or_raise
        from autophone.assets.golden_paths import validate_golden_path_request
        from autophone.knowledge.trial_confirmation import validate_trial_confirmation

        spec = dict(spec)
        validate_or_raise("run-spec.v1.json", spec)
        validate_golden_path_request(spec)
        validate_trial_confirmation(spec)
        options = dict(assignment_options or {})
        if pin_device and not options.get("target_rig_id"):
            options["target_rig_id"] = control.resolve_device_rig(str(spec.get("device") or ""))
        capabilities = set(run_capabilities(spec))
        capabilities.update(options.pop("required_capabilities", []))
        plan = {
            "kind": RUN_ASSIGNMENT_KIND, "requires_rig": True,
            "priority": "normal", **options,
            "required_capabilities": sorted(capabilities), "run_spec": spec,
        }
        try:
            assignment = control.submit_assignment(plan, idempotency_key or f"run:{spec['run_id']}")
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in _CONTROL_REFUSAL_CODES:
                raise
            to_detail = getattr(exc, "to_detail", None)
            details = dict(to_detail()) if callable(to_detail) else {}
            details.pop("code", None)
            details.pop("message", None)
            raise RunDispatchError(str(code), str(exc), details=details) from exc
        if background:
            return self.backend.handle_for(str(spec["run_id"]), {
                **spec, "status": assignment["status"],
                "assignment_id": assignment["assignment_id"],
            })
        return self.wait(assignment, spec, control=control)

    def wait(self, assignment, spec, *, control=None, on_tick=None):
        import time
        from autophone.application.control_plane import require_control_plane

        control = control if control is not None else require_control_plane()
        assignment_id = str(assignment["assignment_id"])
        deadline = time.monotonic() + self.timeout_s
        while str(assignment.get("status") or "") not in _TERMINAL_ASSIGNMENT_STATUSES:
            if on_tick is not None:
                on_tick()
            if time.monotonic() >= deadline:
                raise RunDispatchError(
                    "run_assignment_timeout",
                    f"assignment {assignment_id} did not reach a terminal status within "
                    f"{self.timeout_s:.0f}s (status={assignment.get('status')!r})",
                    assignment_id=assignment_id,
                )
            time.sleep(self.poll_interval_s)
            assignment = control.get_assignment(assignment_id)
        return self.backend.handle_for(str(spec["run_id"]), _run_result(assignment, spec))


def validate_dispatch_telemetry(telemetry_identity):
    """Assignment 当前没有临时遥测身份传输契约，禁止静默丢弃该输入。"""
    if telemetry_identity is not None:
        from autophone.contracts import ContractError
        raise ContractError("control_plane_telemetry_identity_unsupported: "
                            "Assignment does not support telemetry_identity")


def _run_result(assignment: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """取终态 Assignment 里的 Run Result；rig 回报什么就是什么。"""
    result = assignment.get("result")
    result = dict(result) if isinstance(result, Mapping) else {}
    run_result = result.get("run_result")
    if isinstance(run_result, Mapping) and run_result:
        return {
            **dict(run_result), "assignment_id": str(assignment["assignment_id"]),
            "assignment_provisioning": dict((assignment.get("attempt") or {}).get("stage_results") or {}).get("provisioning"),
        }
    # Assignment 没跑到 Run 就终止（preflight blocked、取消、阶段报错）：Run Result 不存在，
    # 用 Assignment 终态与 detail 组一份诚实的结果，CLI 的退出码据它判。
    status = str(assignment.get("status") or "error")
    return {
        "schema_version": "v1",
        "run_id": str(spec.get("run_id") or ""),
        "type": str(spec.get("type") or ""),
        "app": str(spec.get("app") or ""),
        "device": str(spec.get("device") or ""),
        "status": _ASSIGNMENT_STATUS_AS_RUN_STATUS.get(status, status),
        "assignment_id": str(assignment.get("assignment_id") or ""),
        "assignment_status": status,
        "detail": str(result.get("detail") or f"assignment ended with status {status!r}"),
    }


__all__ = [
    "RUN_ASSIGNMENT_KIND",
    "RUN_ASSIGNMENT_POLL_INTERVAL_S",
    "RUN_ASSIGNMENT_TIMEOUT_S",
    "RUN_CAPABILITIES",
    "RunDispatchError",
    "RunDispatcher",
    "RunSubmissionBackend",
    "run_capabilities",
]
