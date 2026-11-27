"""平台侧 API 抓取桥：把 phase4 的 Frida 抓取接进 platform run 生命周期。

两半：
  - `build_session`：构造一个 spawn-gate 抓取会话（**fail-open**：缺 frida / 缺 agent bundle
    → 返回 None，runner 退回普通 uiopen 启动，不让缺抓取能力拖垮整条回归）。
  - `finalize_api_plane`：**纯 host 逻辑、无 frida、可测**——合并产物落 `planes/api/capture.jsonl`，
    逐用例跑 `api_oracle` 断言，并返回新的 UI×API 合成结果。
    回归链运行期只在这里触达 phase4 抓取产物的离线处理（不再起 agent）。

env 覆写：`AUTOPHONE_FRIDA_HOST`（默认 127.0.0.1:27042）、`AUTOPHONE_CAPTURE_WARMUP`（默认 12s）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from threading import Event, Timer
from typing import Any, cast
from autophone.assets.apps import AxBackend, DEFAULT_AX_BACKEND
from autophone.device.frida_endpoint import resolve_frida_host

logger = logging.getLogger(__name__)

CONTROLLED_CAPTURE_PROTOCOL = 1

CAPTURE_REL = "planes/api/capture.jsonl"
READY_REL = "planes/api/capture-ready"
EARLY_LIFECYCLE_TIMEOUT_S = 5.0


def _frida_host() -> str:
    return resolve_frida_host()


def _warmup() -> float:
    try:
        return float(os.environ.get("AUTOPHONE_CAPTURE_WARMUP", "12"))
    except ValueError:
        return 12.0


def _ready_timeout(profile: Any) -> float:
    env = os.environ.get("AUTOPHONE_CAPTURE_READY_TIMEOUT")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            return 20.0
    try:
        raw = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {}).get("api_capture") or {})
        return float(raw.get("ready_timeout_s", 20))
    except (TypeError, ValueError):
        return 20.0


def _ready_poll() -> float:
    try:
        return max(0.1, float(os.environ.get("AUTOPHONE_CAPTURE_READY_POLL", "1")))
    except ValueError:
        return 1.0


def _ready_markers(profile: Any) -> list[str]:
    env = os.environ.get("AUTOPHONE_CAPTURE_READY_TEXTS")
    if env is not None:
        return [p.strip() for p in env.split(",") if p.strip()]
    raw = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {}).get("api_capture") or {})
    markers = raw.get("ready_markers") or []
    return [str(m).strip() for m in markers if str(m).strip()]


def dispatch_reset_to_home(shot_dir: Path, profile: Any, *, case_id: str) -> dict[str, Any]:
    """Dispatch the App's registered ``reset_to_home`` Operation once and return its outcome.

    The same path Case navigation uses (no AI decision).  Exceptions are returned, not
    raised: callers keep their own verdict when the reset cannot run or fails.
    """
    started = time.monotonic()
    outcome: dict[str, Any] = {"operation": "reset_to_home", "ok": False}
    try:
        from autophone.execution.engine.actions import CaseContext
        from autophone.execution.engine.recipe import dispatch_step, operation_scope
        from autophone.paths import CODE_ROOT
        from autophone.execution.app_recipes import import_app_recipes

        root = getattr(profile, "root", None) or CODE_ROOT
        import_app_recipes(profile.app, root=root)
        with operation_scope(profile.app, root=root) as ops:
            if "reset_to_home" not in ops:
                outcome["registered"] = False
                outcome["error"] = (
                    f"RecipeError: unknown operation 'reset_to_home'; registered={sorted(ops)}"
                )
            else:
                outcome["registered"] = True
                ctx = CaseContext(case_id=case_id, shot_dir=shot_dir)
                dispatch_step(ctx, {"operation": "reset_to_home"})
                outcome["ok"] = True
    except Exception as exc:  # noqa: BLE001 - the outcome is evidence; the caller decides
        kind = getattr(getattr(exc, "result", None), "kind", None)
        name = type(exc).__name__ + (f"[{kind}]" if kind else "")
        outcome["error"] = f"{name}: {exc}"
    outcome["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return outcome


def _reset_before_ready(shot_dir: Path, profile: Any) -> None:
    """attach 前先把界面拨回首页：uiopen 只前台化、不重启，上个 run 最后一个用例若停在
    子页（如「我的业绩」，见 R119 无 teardown），下个 run 的 uiopen 会把这个残留子页原样
    前台化——OCR ready gate 找的是首页 marker，在子页上永远等不到，20s 超时纯浪费且整
    run 落回不抓包。这里在 OCR 轮询前主动 dispatch 一次 reset_to_home，用与用例导航同一
    条路径把残留态清掉；异常不外抛（fail-open，清不掉照旧交给下面的 OCR 轮询/超时判）。
    Run 准备阶段的同类处理见 autophone/rig/maintenance.py ``_reset_home_and_probe``。"""
    outcome = dispatch_reset_to_home(
        shot_dir / "_capture_ready_reset", profile, case_id="capture-ready-reset")
    if not outcome["ok"]:
        logger.warning("attach 前 reset_to_home 复位失败（%s），沿用 OCR ready gate 原判", outcome["error"])


def _reset_then_wait_ready(shot_dir: Path, profile: Any) -> dict[str, Any]:
    # Preserve an already visible app surface.  In attach mode a forced kill/reset
    # can race the guest's auto-respawn and leave SpringBoard frontmost just before
    # the ready gate.  A profile-owned OCR/AX marker is stronger evidence than
    # blindly resetting a known-good menu page.
    already_ready = wait_for_spawn_ready(shot_dir, profile, timeout_s=0.0)
    if already_ready.get("ok"):
        return {**already_ready, "preserved_visible_surface": True}
    _reset_before_ready(shot_dir, profile)
    return wait_for_spawn_ready(shot_dir, profile)


def _wait_ready_preserve_only(shot_dir: Path, profile: Any) -> dict[str, Any]:
    """接口场景 surface_policy=preserve_only（手工探索，docs §五）：当前页就绪则保留，
    **未就绪绝不自动复位**——返回 ok=false 交测试者决策（取消 / 回首页 / 就地挂载）。

    与 _reset_then_wait_ready 的区别：那个是 recover_home（保留失败即 reset_to_home），
    这个是保留优先且不擅自复位，避免手工测试者停的子页被静默拨回首页（Finding 5）。"""
    already_ready = wait_for_spawn_ready(shot_dir, profile, timeout_s=0.0)
    if already_ready.get("ok"):
        return {**already_ready, "preserved_visible_surface": True}
    return {
        "ok": False,
        "reason": "surface-not-ready",
        "detail": ("当前页未确认就绪，且 surface_policy=preserve_only 禁止自动复位；"
                   "请选择 回首页(recover_home) / 就地挂载(attach_here) / 取消"),
        "preserved_visible_surface": False,
    }


def wait_for_spawn_ready(
    shot_dir: Path,
    profile: Any,
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    request_fn: Any | None = None,
    allow_ax: bool = True,
    ocr_runner: Any | None = None,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> dict[str, Any]:
    """Poll screenshots after Frida spawn-gate until the app has rendered a usable screen.

    Fixed warmup only proves "some time passed"; this proves the UI surface is no
    longer a black/empty cold-start frame before runner/login probes can tap it.
    """
    if request_fn is None:
        from autophone.device.uds import request as request_fn
    if ocr_runner is None:
        from autophone.device.ocr_target import run_ocr as ocr_runner

    timeout = _ready_timeout(profile) if timeout_s is None else max(0.0, float(timeout_s))
    poll = _ready_poll() if poll_s is None else max(0.1, float(poll_s))
    markers = _ready_markers(profile)
    out = shot_dir / READY_REL
    out.mkdir(parents=True, exist_ok=True)

    deadline = monotonic_fn() + timeout
    attempt = 0
    last_detail = "尚未截屏"
    last_ref = ""
    ax_provider: Any | None = None
    ax_attempted = False
    try:
        while True:
            attempt += 1
            try:
                obj, _sample = request_fn({"t": "screenshot"}, timeout=5)
                img = obj.get("image")
                if not img:
                    last_detail = f"screenshot returned no image: {obj.get('error')}"
                else:
                    shot = out / f"probe-{attempt:02d}.jpg"
                    shot.write_bytes(base64.b64decode(img))
                    last_ref = str(shot)
                    ocr = ocr_runner(shot)
                    texts = [str(b.get("text") or "").strip() for b in ocr.get("boxes", []) if b.get("text")]
                    joined = "\n".join(texts)
                    hit = next((m for m in markers if m in joined), "")
                    if hit:
                        return {"ok": True, "reason": "marker", "marker": hit, "attempts": attempt, "evidence_ref": last_ref}
                    if texts and not markers:
                        return {"ok": True, "reason": "ocr_text", "attempts": attempt, "evidence_ref": last_ref}

                    # Compact Chinese menu surfaces can yield an empty OCR result
                    # even while the app is fully interactive.  For profiles that
                    # explicitly opted into AX, use one live accessibility tree as
                    # a read-only ready proof rather than treating it as a black
                    # screen.  Do not try AX for ordinary profiles.
                    if allow_ax and markers and not ax_attempted:
                        ax_attempted = True
                        from autophone.device.ax_session import UnsupportedAxBackendError
                        try:
                            ax_provider = build_run_ax_session(profile, shot_dir.parent)
                            if ax_provider is not None:
                                ax_provider.open()
                        except UnsupportedAxBackendError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - keep OCR gate conservative
                            ax_provider = None
                            logger.debug("capture ready AX fallback unavailable: %s", exc)
                    if markers and ax_provider is not None:
                        from autophone.device.ax_session import UnsupportedAxBackendError
                        try:
                            from autophone.device.ax_target import find_node

                            dump = ax_provider.refresh()
                            hit = next((m for m in markers if find_node(dump, m, enabled_only=False)), "")
                            if hit:
                                return {"ok": True, "reason": "ax_marker", "marker": hit,
                                        "attempts": attempt, "evidence_ref": last_ref}
                        except UnsupportedAxBackendError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - retain OCR timeout wording
                            logger.debug("capture ready AX refresh unavailable: %s", exc)
                    last_detail = (
                        f"OCR/AX 未命中 ready marker {markers!r}; texts={texts[:8]}"
                        if markers else "OCR 未识别到文本（疑似黑屏/启动中）"
                    )
            except Exception as exc:  # noqa: BLE001 ready gate 只负责判定，不让探针异常外冒
                from autophone.device.ax_session import UnsupportedAxBackendError
                if isinstance(exc, UnsupportedAxBackendError):
                    raise
                last_detail = f"{type(exc).__name__}: {exc}"
            if monotonic_fn() >= deadline:
                return {
                    "ok": False,
                    "reason": "timeout",
                    "attempts": attempt,
                    "detail": last_detail,
                    "evidence_ref": last_ref,
                    "timeout_s": timeout,
                }
            sleep_fn(poll)
    finally:
        if ax_provider is not None:
            try:
                ax_provider.close()
            except Exception:  # noqa: BLE001 - readiness probing never fails during cleanup
                pass


def probe_app_foreground_support(request_fn: Any | None = None) -> bool | None:
    """一次性探当前 guest 二进制认不认识 UDS ``app_foreground``。

    spawn 后黑屏常和 guest 启动/前台能力降级同源——这个三态信号挂进 ready-gate
    warning，一眼能分清是「撞到老 guest build」还是别的原因（与 foreground.py 同口径：
    ``unknown`` 错误 = 不支持）。

    返回：True=支持；False=guest 不认该命令（降级）；None=探测本身失败（设备不可达/协议错），
    无从判定。本探针只读不写，绝不外抛。
    """
    if request_fn is None:
        from autophone.device.uds import request as request_fn
    try:
        resp, _sample = request_fn({"t": "app_foreground"}, timeout=5)
    except Exception:  # noqa: BLE001 探针失败不拖累 warning
        return None
    if resp.get("ok"):
        return True
    return False if "unknown" in str(resp.get("error") or "").lower() else None


def describe_app_foreground_support(supported: bool | None) -> str:
    return {True: "yes", False: "no(guest 降级)"}.get(supported, "unknown(探测失败)")


def _capture_mode(profile: Any) -> str:
    """抓取启动模型：``spawn``（spawn-gate）｜``attach``（uiopen+attach）。

    spawn-gate 在某些 guest build 上 spawn 后黑屏、且 spawn 出的 pid ≠ 前台 UI pid（假绿），
    那台环境把 profile 的 ``api_capture.mode`` 设 ``attach`` 即切到 uiopen+attach。缺省 ``spawn``。"""
    env = os.environ.get("AUTOPHONE_CAPTURE_MODE")
    if env:
        return env.strip().lower()
    raw = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {}).get("api_capture") or {})
    return str(raw.get("mode") or "spawn").strip().lower()


def _bounded_capture_rpc(fn: Any, prefix: str) -> Any:
    """Cancel a stalled capture RPC without detaching its owned session early."""
    import frida

    expired = Event()
    cancellable = frida.Cancellable()

    def cancel():
        expired.set()
        cancellable.cancel()

    timer = Timer(EARLY_LIFECYCLE_TIMEOUT_S, cancel)
    timer.daemon = True
    try:
        with cancellable:
            timer.start()
            result = fn()
    except Exception as exc:
        code = prefix + ('_timeout' if expired.is_set() else '_rpc_failed')
        raise RuntimeError(code) from exc
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
    if expired.is_set():
        raise RuntimeError(prefix + '_timeout')
    return result


def _install_early_lifecycle(exports: Any) -> None:
    """Install only explicitly named hooks; cancellation bounds a paused RPC wait."""
    fn = getattr(exports, 'install_capture_lifecycle_hooks', None) or getattr(
        exports, 'installCaptureLifecycleHooks', None)
    if fn is None:
        raise RuntimeError('early_capture_lifecycle_rpc_missing')
    result = _bounded_capture_rpc(fn, 'early_capture_lifecycle')
    if (not isinstance(result, dict) or result.get('ok') is not True
            or result.get('resume_mode') != 'passive'
            or type(result.get('creation_hook_count')) is not int or result['creation_hook_count'] < 1
            or type(result.get('creation_hook_errors')) is not int or result['creation_hook_errors'] != 0
            or result.get('scope') != 'named_foundation_task_creation'
            or result.get('class_enumeration') is not False):
        raise RuntimeError('early_capture_lifecycle_coverage_unavailable')


def _capture_on_loaded(profile: Any) -> Any | None:
    """Configure profile-owned body capture domains before passive hooks start.

    Requests outside these URL substrings remain visible as metadata, while
    their response bodies are not copied inside the App process.
    """
    raw = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {}).get("api_capture") or {})
    early_lifecycle = raw.get('early_lifecycle', False)
    if type(early_lifecycle) is not bool:
        raise ValueError('early_lifecycle must be boolean')
    domains = [
        str(value).strip().lower()
        for value in (raw.get("domains") or [])
        if str(value).strip()
    ]
    if not domains and not early_lifecycle:
        return None

    capture_filter = {"body_url_substrings": domains}

    def configure(exports: Any) -> None:
        if domains:
            fn = getattr(exports, "configure_capture_filter", None) or getattr(
                exports, "configureCaptureFilter", None
            )
            if fn is None:
                raise RuntimeError("capture agent 缺少 configureCaptureFilter；请重建 net_tap bundle")
            result = fn(capture_filter)
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(f"capture filter 配置失败: {result}")
        if early_lifecycle:
            _install_early_lifecycle(exports)

    return configure


def build_session(profile: Any, spec: dict[str, Any], shot_dir: Path | None = None) -> Any | None:
    """构造抓取会话；controlled 前置失败抛错，自然采集前置失败返回 None。

    `mode=spawn`（默认）→ `CaptureSession`（spawn-gate）；`mode=attach` → `AttachSession`
    （uiopen+attach，attach 前的 OCR ready gate 绑定 `shot_dir`+`profile`）。两者都是
    上下文管理器 + `.handle` + `.merged()`。"""
    if ((spec.get("planes") or {}).get("api") or {}).get("mode") == "controlled":
        from autophone.observation.controlled_capture import build_session as build_controlled
        return build_controlled(profile, spec, shot_dir)
    try:
        from autophone.observation.frida import net_capture
        from autophone.observation.frida.capture_session import AttachSession, CaptureSession
    except Exception as exc:  # noqa: BLE001 frida/依赖缺失 → 不抓取
        logger.warning(
            "API 抓取面已请求但不可用：frida/phase4 依赖导入失败（%s），退回普通启动、不采接口数据", exc
        )
        return None

    agent = Path(net_capture.AGENT_JS)
    if not agent.exists():  # agent bundle 未 build → 没法抓
        logger.warning(
            "API 抓取面已请求但不可用：agent bundle 未 build（%s 不存在），退回普通启动、不采接口数据", agent
        )
        return None

    host = _frida_host()
    mode = _capture_mode(profile)
    on_loaded = _capture_on_loaded(profile)
    if mode == "attach":
        import functools

        # attach 前的 ready gate：绑 shot_dir+profile；缺 shot_dir 时退化为无 OCR gate（仅 pid 校验）。
        # 先 reset_to_home 拨掉上个 run 遗留的子页，再轮询 OCR home marker（见 _reset_before_ready）。
        ready_fn = (
            functools.partial(_reset_then_wait_ready, shot_dir, profile)
            if shot_dir is not None else None
        )
        logger.info(
            "API 抓取会话就绪(attach 模式)：frida=%s bundle=%s ready_timeout=%ss"
            "（spawn-gate not usable on this guest, using uiopen+attach capture）",
            host, profile.bundle_id, _ready_timeout(profile),
        )
        return AttachSession(
            host,
            str(profile.bundle_id),
            str(agent),
            ready_fn=ready_fn,
            on_loaded=on_loaded,
            identity_path=(shot_dir.parent / "build-identity.json") if shot_dir is not None else None,
            run_id=str(spec.get("run_id") or "") or None,
            app=str(spec.get("app") or "") or None,
        )

    logger.info(
        "API 抓取会话就绪(spawn 模式)：frida=%s bundle=%s warmup=%ss ready_timeout=%ss",
        host, profile.bundle_id, _warmup(), _ready_timeout(profile),
    )
    return CaptureSession(
        host,
        str(profile.bundle_id),
        str(agent),
        warmup=_warmup(),
        on_loaded=on_loaded,
        identity_path=(shot_dir.parent / "build-identity.json") if shot_dir is not None else None,
        run_id=str(spec.get("run_id") or "") or None,
        app=str(spec.get("app") or "") or None,
    )


def _resolve_profile_ax_backend(profile_or_backend: Any) -> str:
    if profile_or_backend is None:
        return DEFAULT_AX_BACKEND
    if isinstance(profile_or_backend, str):
        return profile_or_backend.strip()
    backend_attr = getattr(profile_or_backend, "ax_backend", None)
    if backend_attr is not None:
        return str(backend_attr).strip()
    raw = (((getattr(profile_or_backend, "raw", {}) or {}).get("capabilities") or {}).get("ax") or {})
    raw_backend = raw.get("backend")
    return DEFAULT_AX_BACKEND if raw_backend is None else str(raw_backend).strip()


def ensure_ax_backend_supported(profile_or_backend: Any) -> AxBackend:
    """Validate and freeze the AX backend at Run preflight (Decision D8).

    Fails closed with ``UnsupportedAxBackendError`` (code ``ax_backend_unsupported``)
    for any unknown/unsupported backend; never falls back silently.
    """
    from autophone.device.ax_session import SUPPORTED_AX_BACKENDS, UnsupportedAxBackendError

    backend = _resolve_profile_ax_backend(profile_or_backend)
    if backend not in SUPPORTED_AX_BACKENDS:
        raise UnsupportedAxBackendError(
            backend,
            f"ax_backend {backend!r} is not supported; "
            f"refusing to fall back to {DEFAULT_AX_BACKEND!r}",
        )
    return cast(AxBackend, backend)


def _ax_enabled(profile: Any) -> bool:
    """profile 是否开 AX 结构寻址（capabilities.ax.enabled）。缺省关；开启时默认使用免 Frida 的
    ``guest_axruntime`` 后端（亦可显式 opt-in ``frida_inproc``）。
    AUTOPHONE_AX 环境变量可强制 on/off 覆盖（排障用）。"""
    env = os.environ.get("AUTOPHONE_AX")
    if env is not None:
        return env.strip().lower() in ("1", "on", "true", "yes")
    raw = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {}).get("ax") or {})
    return bool(raw.get("enabled"))


def build_ax_session(profile: Any, *, attach_timeout_s: float | None = None) -> Any | None:
    """构造（未 open 的）AX 寻址会话；不具备条件（未开 / 依赖导入失败）→ None（fail-open）。

    支持 ``guest_axruntime``（默认，进程外免 Frida 注入）与 ``frida_inproc``（显式 opt-in，
    进程内 Frida + dylib）。若 Profile 声明了未知或未支持的 ``ax_backend``，立即抛出
    ``UnsupportedAxBackendError``（fail-closed，严禁静默回退）。

    返回值是 `AxSession`（context manager，``guest_axruntime`` 时经 UDS 调度进程外 AXRuntime
    probe；``frida_inproc`` 时 __enter__ attach+注入 dylib+激活 accessibility、__exit__ detach）。
    service 在 capture 会话内或普通执行路径 with 它、把 `.` 作为 ctx.ax 传给 run()，
    让 tap_ax 走结构寻址（OCR 读不出的门店名/紧凑中文按钮）；AX 建不起来时 tap_ax 自动退
    fallback_xy/fail-closed。``attach_timeout_s`` 给定时限制 ``frida_inproc`` 单次 frida attach
    的等待时长（见 ``AxSession``）；缺省不限制，回归 Run 使用缺省值。"""
    ax_backend = ensure_ax_backend_supported(profile)
    if not _ax_enabled(profile):
        return None
    try:
        from autophone.device.ax_session import AxSession
    except Exception as exc:  # noqa: BLE001 frida/依赖缺失 → 不走 AX
        logger.warning("AX 寻址已请求但不可用：ax_session 导入失败（%s），tap_ax 退 fallback_xy", exc)
        return None
    if ax_backend == "guest_axruntime":
        logger.info("AX 寻址会话待开：backend=%s bundle=%s（guest_axruntime 进程外免注入）",
                    ax_backend, profile.bundle_id)
        return AxSession(str(profile.bundle_id), ax_backend=ax_backend)
    logger.info("AX 寻址会话待开：backend=%s frida=%s bundle=%s（tap_ax 走结构寻址，OCR 兜底）",
                ax_backend, _frida_host(), profile.bundle_id)
    if attach_timeout_s is None:
        return AxSession(str(profile.bundle_id), host=_frida_host(), ax_backend=ax_backend)
    return AxSession(
        str(profile.bundle_id),
        host=_frida_host(),
        attach_timeout_s=attach_timeout_s,
        ax_backend=ax_backend,
    )


def build_run_ax_session(
    profile: Any, run_dir: Path, *, attach_timeout_s: float | None = None,
    capture_session: Any = None,
) -> Any | None:
    """把 Run 的 AX 临时输出绑定到已有的私有证据目录。"""
    ax_backend = ensure_ax_backend_supported(profile)
    kwargs = {} if attach_timeout_s is None else {"attach_timeout_s": attach_timeout_s}
    if getattr(capture_session, "_controlled_identity", None) is not None:
        from autophone.device.ax_session import AxSession, AxUnavailable
        lifetime = getattr(capture_session, "_lifetime", None)
        pid = getattr(capture_session, "pid", None)
        if (not _ax_enabled(profile)
                or type(pid) is not int or pid <= 0
                or pid != getattr(capture_session, "_owned_pid", None)
                or str(profile.bundle_id) != capture_session.bundle_id):
            raise AxUnavailable("controlled_ax_owner_unavailable")
        if ax_backend == "guest_axruntime":
            provider = AxSession(
                str(profile.bundle_id),
                target_pid=pid,
                ax_backend=ax_backend,
            )
        else:
            if lifetime is None or lifetime.closed or lifetime.closing:
                raise AxUnavailable("controlled_ax_owner_unavailable")
            provider = AxSession(
                str(profile.bundle_id),
                borrowed_lifetime=lifetime,
                ax_backend=ax_backend,
            )
    else:
        provider = build_ax_session(profile, **kwargs)
    if provider is None:
        return None
    out = run_dir / "planes/ax/private/latest.json"
    if out.is_symlink() or not out.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("escaped AX artifact path")
    out.parent.mkdir(parents=True, exist_ok=True)
    provider.out = str(out)
    return provider


def write_capture_jsonl(run_dir: Path, merged: list[dict]) -> Path:
    """合并后的调用 → run 目录 planes/api/capture.jsonl（一行一条，原始 body 不脱敏，仅本机）。"""
    path = run_dir / CAPTURE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in merged:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def finalize_api_plane(
    run_dir: Path,
    merged: list[dict],
    result: Any,
    *,
    app: str | None = None,
    expectation_statuses: tuple[str, ...] = ("active",),
    cases: Sequence[Mapping[str, Any]] | None = None,
    profile: Any | None = None,
    api_fixtures_path: Path | None = None,
    api_captured: bool = True,
) -> Any:
    """落 capture.jsonl，并返回 API Plane 摘要与新的合成结果。

    纯 host 逻辑（api_oracle/governance 均无 frida 依赖）→ 注入假 merged + RunResult 即可测。
    缺 expectations 文件时 `evaluate_case` 返回空 → 写 jsonl、不改用例态（advisory 旁路）。

    对于声明了 `entity_binding` 的用例（专项二）：
    - 解析绑定业务实体 (`resolve_case_entity`)，在病例交互窗口上评估 `api_window`
      (`api_oracle.evaluate_entity_api_assertions`) 并与既有 `api_expectations.yaml` 断言合并；
    - 通过 `joint_verdict.arbitrate_joint_verdict` 执行 UI + API + Readback 联合裁决，
      fail-closed 回写 `outcome.joint_verdict` 与 `outcome.status`/`fail_kind`/`detail`。
    """
    from autophone.assets.entity_binding import case_has_entity_binding, resolve_case_entity
    from autophone.execution.engine.runner import _redacted_resolved_entity
    from autophone.observation.composer import ApiPlaneFinalization, compose_phase2_api_plane
    from autophone.observation.plane_evaluation import (
        ApiAssertionResult,
        CasePlaneInput,
        evaluate_api_plane,
    )
    from autophone.observation.frida import api_oracle
    from autophone.observation.frida.governance import redact_assertion, redact_summary

    jsonl: Path | None = write_capture_jsonl(run_dir, merged) if api_captured else None

    resolved_profile = profile
    if resolved_profile is None and app:
        try:
            from autophone.assets.apps import load_app_profile

            resolved_profile = load_app_profile(app)
        except Exception:  # noqa: BLE001
            resolved_profile = None

    env_ctx = api_oracle.fixture_context_vars_from_env()
    if profile is not None:
        expectations = api_oracle.load_expectations(
            profile.api_expectations_path(), statuses=expectation_statuses
        )
    elif app:
        expectations = api_oracle.load_app_expectations(app, statuses=expectation_statuses)
    else:
        expectations = api_oracle.load_expectations()
    app_fixtures = api_fixtures_path
    if app_fixtures is None and resolved_profile is not None:
        try:
            app_fixtures = resolved_profile.api_fixtures_path()
        except Exception:  # noqa: BLE001 缺 fixtures 文件 → 逐 case 退化为空表（缺 ${var} 由 oracle fail-closed）
            app_fixtures = None

    cases_by_id: dict[str, Mapping[str, Any]] = {}
    if cases is not None:
        for item in cases:
            if isinstance(item, Mapping) and item.get("id"):
                cases_by_id[str(item["id"]).upper()] = item
    elif resolved_profile is not None:
        try:
            from autophone.execution.engine.cases_loader import load_cases

            for item in load_cases(resolved_profile.cases_path()):
                if isinstance(item, Mapping) and item.get("id"):
                    cases_by_id[str(item["id"]).upper()] = item
        except Exception:  # noqa: BLE001
            pass

    resolved_entities_by_id: dict[str, dict[str, Any]] = {}
    case_inputs: list[CasePlaneInput] = []
    for outcome in getattr(result, "outcomes", []) or []:
        case_id = str(getattr(outcome, "id", "") or "")
        if not case_id:
            continue
        fixture_ctx = (
            {**env_ctx, **api_oracle.flat_fixture_vars(app_fixtures, case_id)}
            if (app or app_fixtures is not None)
            else dict(env_ctx)
        )
        # A case's action-time binding takes precedence over external/static fixtures.
        # Build a new dict so neither adjacent cases nor env_ctx inherit this identity.
        runtime_ctx = dict(getattr(outcome, "api_assertion_context", None) or {})
        ctx = {**fixture_ctx, **runtime_ctx}
        case_def = cases_by_id.get(case_id.upper())
        entity_binding: Mapping[str, Any] | None = None
        resolved_entity: Any | None = None
        if case_def is not None and case_has_entity_binding(case_def):
            entity_binding = case_def.get("entity_binding")
            resolved_entity = resolve_case_entity(
                case_def,
                fixture_vars=fixture_ctx,
                runtime_context=runtime_ctx,
            )
            if resolved_entity is not None:
                tvars = getattr(resolved_entity, "template_vars", None) or {}
                ctx = {**ctx, **dict(tvars), **runtime_ctx}
                # Ensure entity.* template variables stay authoritative over raw runtime keys.
                for key, val in dict(tvars).items():
                    if str(key).startswith("entity."):
                        ctx[str(key)] = val
                redacted = _redacted_resolved_entity(resolved_entity)
                if redacted is not None:
                    resolved_entities_by_id[case_id.upper()] = redacted
        case_inputs.append(
            CasePlaneInput(
                case_id,
                str(getattr(outcome, "ui_status", "") or outcome.status),
                ctx,
                entity_binding=entity_binding,
                readback_record=getattr(outcome, "entity_readback", None),
                resolved_entity=resolved_entity,
                ui_fail_kind=str(getattr(outcome, "fail_kind", "") or ""),
            )
        )

    class _OracleAdapter:
        def evaluate(self, case_id, calls, context):
            assertions = list(
                api_oracle.evaluate_case(
                    case_id,
                    list(calls),
                    expectations,
                    ctx=context,
                )
            )
            case_def = cases_by_id.get(str(case_id).upper())
            if case_def is not None and case_has_entity_binding(case_def):
                binding = case_def.get("entity_binding")
                api_window = binding.get("api_window") if isinstance(binding, Mapping) else None
                if isinstance(api_window, Mapping):
                    entity_assertions = api_oracle.evaluate_entity_api_assertions(
                        case_id,
                        list(calls),
                        api_window,
                        ctx=context,
                    )
                    assertions.extend(entity_assertions)
            return ApiAssertionResult(
                status=api_oracle.api_status(assertions),
                assertions=tuple(redact_assertion(item) for item in assertions),
            )

        def summarize_call(self, call):
            return redact_summary(call)

    evaluation = evaluate_api_plane(
        case_inputs,
        merged,
        _OracleAdapter(),
        api_captured=api_captured,
    )
    composed = compose_phase2_api_plane(
        result,
        evaluation,
        jsonl=str(jsonl) if jsonl is not None else None,
    )
    for outcome in getattr(composed, "outcomes", []) or []:
        cid = str(getattr(outcome, "id", "") or "").upper()
        if getattr(outcome, "resolved_entity", None) is None and cid in resolved_entities_by_id:
            outcome.resolved_entity = resolved_entities_by_id[cid]
    summary = {
        "jsonl": str(jsonl) if jsonl is not None else None,
        "calls": evaluation.calls,
        "attributed": evaluation.attributed,
        "asserted_cases": evaluation.asserted_cases,
        "assertions": evaluation.assertions,
        "fail": evaluation.failed_assertions,
    }
    return ApiPlaneFinalization(run_result=composed, summary=summary)


class LazyAxSession:
    """首次使用时才 open 的 AX 会话代理。

    operation run 用它代替执行前预开：Operation 可能先冷启动 App，预开的会话会 attach 到
    即将被杀的旧进程（2026-09-24 vm op1a、op1c 各因此阻塞约 25s）。``refresh`` /
    ``snapshot`` 在未打开时先 open；``reopen`` 在未打开时只 open 一次。open 失败后，后续
    ``refresh`` / ``snapshot`` 直接抛 ``AxUnavailable``，不重复 attach；``reopen`` 重新尝试。
    open 可能在 attach 成功后才失败（脚本加载、dylib 注入），因此失败时、下次尝试 open 前与
    ``close`` 时都调用底层 ``close``，不留下未 detach 的会话。其他属性转发给被代理的会话。"""

    def __init__(self, session: Any) -> None:
        self._ax_backend = ensure_ax_backend_supported(
            getattr(session, "ax_backend", DEFAULT_AX_BACKEND)
        )
        self._session = session
        self._opened = False
        self._attempted = False
        self._failure: Exception | None = None

    @property
    def ax_backend(self) -> AxBackend:
        return self._ax_backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_session"], name)

    @property
    def opened(self) -> bool:
        return self._opened

    def _release(self) -> None:
        """尽力关闭底层会话（open 半途失败时可能已 attach）。"""
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - 收尾失败不改变 open 的结论
            logger.debug("AX 会话收尾失败", exc_info=True)

    def _open(self) -> None:
        logger.info("AX 会话首次使用，现在打开（延迟打开）")
        if self._attempted:
            self._release()
        self._attempted = True
        try:
            self._session.open()
        except Exception as exc:
            self._failure = exc
            logger.warning("AX 会话延迟打开失败：%s: %s", type(exc).__name__, exc)
            self._release()
            raise
        self._opened, self._failure = True, None

    def _ensure_open(self) -> None:
        if self._opened:
            return
        if self._failure is not None:
            from autophone.device.ax_session import AxUnavailable, UnsupportedAxBackendError
            if isinstance(self._failure, UnsupportedAxBackendError):
                raise self._failure
            raise AxUnavailable(
                f"AX session open failed earlier: {type(self._failure).__name__}: {self._failure}"
            ) from self._failure
        self._open()

    def open(self) -> "LazyAxSession":
        self._ensure_open()
        return self

    def refresh(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._session.refresh(*args, **kwargs)

    def snapshot(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._session.snapshot(*args, **kwargs)

    def reopen(self) -> "LazyAxSession":
        if self._opened:
            try:
                self._session.reopen()
            except Exception as exc:
                self._opened, self._failure = False, exc
                raise
        else:
            self._open()
        return self

    def close(self) -> None:
        if not self._attempted:
            return
        self._opened = self._attempted = False
        self._session.close()


@contextmanager
def lazy_ax_scope(ax_session: Any):
    """yield 未打开的 ``LazyAxSession``（None 原样 yield），退出时关闭已打开的会话。

    与 ``ax_scope`` 的差异：不在进入时 attach/probe，open 失败在首次使用处以 ``AxUnavailable``
    暴露，由调用方按能力缺口处理。对未知或未支持的 ``ax_backend`` 立即 fail-closed 抛出
    ``UnsupportedAxBackendError``。"""
    if ax_session is None:
        yield None
        return
    ensure_ax_backend_supported(getattr(ax_session, "ax_backend", DEFAULT_AX_BACKEND))
    lazy = LazyAxSession(ax_session)
    try:
        yield lazy
    finally:
        try:
            lazy.close()
        except Exception:  # noqa: BLE001 收尾失败不影响已跑结果
            pass


@contextmanager
def ax_scope(ax_session: Any):
    """开启 AX 寻址会话（fail-open）：``frida_inproc`` 进程内 attach+注入 dylib，
    ``guest_axruntime`` 走进程外 AXRuntime probe（零 Frida 注入）。

    None（未开 AX / 构造失败）或常规 open 抛错（无 frida / 注入被拒 / app 未跑）→ yield None，
    tap_ax 自动退 fallback_xy/fail-closed，run 照跑；成功则 yield 已 open 的 provider，退出时 close。
    但对 ``UnsupportedAxBackendError``（未知或未支持的 ``ax_backend``）严禁吞掉，必须立即 fail-closed 外抛。"""
    if ax_session is None:
        yield None
        return
    from autophone.device.ax_session import UnsupportedAxBackendError

    ensure_ax_backend_supported(getattr(ax_session, "ax_backend", DEFAULT_AX_BACKEND))
    try:
        ax_session.open()
    except UnsupportedAxBackendError:
        raise
    except Exception as exc:  # noqa: BLE001 无 frida / 注入被拒 / app 未跑
        logger.warning("AX 寻址会话开启失败（%s），tap_ax 退 fallback_xy，run 照跑", exc)
        yield None
        return
    try:
        yield ax_session
    finally:
        try:
            ax_session.close()
        except Exception:  # noqa: BLE001 收尾失败不影响已跑结果
            pass
