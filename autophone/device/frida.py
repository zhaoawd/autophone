"""Bounded Frida work performed outside the runner process.

Frida's native calls may block indefinitely.  A Python thread cannot cancel
such a call, so the only reliable timeout boundary is a child process.  This
module deliberately exposes a few small operations:

* a capture session which stays alive until its caller closes it; and
* one-shot contract, infrastructure and enumeration probes.

The parent only receives JSON-safe evidence.  No Frida handle crosses the
process seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import hashlib
import multiprocessing as mp
from multiprocessing.connection import Connection
import threading
import time
from typing import Any, Mapping

from autophone.device.frida_script import script_exports
from autophone.device.stage_process import device_process
from autophone.device.stage_guard import StageGuard, OperationClass


def _stage_frida(function):
    @wraps(function)
    def call(*args, **kwargs):
        binding = getattr(args[0], "_stage_binding", None) if args else None
        guard = binding.guard if binding is not None else StageGuard.for_device_call()
        if guard is None:
            return function(*args, **kwargs)
        if args and function.__name__ in {"close", "detach"} and getattr(args[0], "_closed", False):
            return function(*args, **kwargs)
        # A worker proxy explicitly carries its creation token to callers on
        # other threads. Never replace an already-bound foreign authority.
        with StageGuard.bind(StageGuard.binding() or binding):
            with guard.operation(
                OperationClass.FRIDA,
                frida_endpoint=kwargs.get("host") or kwargs.get("frida_host"),
                socket_path=kwargs.get("sock_path") or None,
            ):
                return function(*args, **kwargs)
    return call


DEFAULT_READY_TIMEOUT_S = 5.0
DEFAULT_SCRIPT_LOAD_TIMEOUT_S = 10.0
DEFAULT_TEARDOWN_TIMEOUT_S = 2.0
DEFAULT_COMMAND_TIMEOUT_S = 2.0
_IDENTITY_PROBE_SOURCE = r"""
rpc.exports = {
  readidentity(path) {
    const open = new NativeFunction(
      Module.getGlobalExportByName("open"), "int", ["pointer", "int"]
    );
    const read = new NativeFunction(
      Module.getGlobalExportByName("read"), "long", ["int", "pointer", "ulong"]
    );
    const close = new NativeFunction(
      Module.getGlobalExportByName("close"), "int", ["int"]
    );
    const fd = open(Memory.allocUtf8String(path), 0);
    if (fd < 0) {
      throw new Error("identity file is unreadable: " + path);
    }
    try {
      const capacity = 4096;
      const buffer = Memory.alloc(capacity);
      const count = Number(read(fd, buffer, capacity));
      if (count < 0) {
        throw new Error("identity file read failed: " + path);
      }
      if (count === capacity) {
        throw new Error("identity file exceeds 4095 bytes: " + path);
      }
      return buffer.readUtf8String(count);
    } finally {
      close(fd);
    }
  }
};
"""


class FridaWorkerError(RuntimeError):
    """A bounded Frida operation failed before returning usable evidence."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = dict(result)
        super().__init__(
            f"{self.result.get('stage', 'FRIDA_FAILED')}: "
            f"{self.result.get('error_class', 'FridaWorkerError')}: "
            f"{self.result.get('detail', '')}"
        )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _failure(stage: str, started: float, exc: BaseException) -> dict[str, Any]:
    detail = str(exc) or repr(exc)
    lower = detail.lower()
    if "probing dyld" in lower:
        stage = "DYLD_PROBE_FAILED"
    elif "timeout" in lower:
        stage = {
            "ATTACH_FAILED": "ATTACH_TIMEOUT",
            "RPC_FAILED": "RPC_TIMEOUT",
            "SCRIPT_LOAD_FAILED": "SCRIPT_LOAD_TIMEOUT",
            "CONFIGURE_FAILED": "CONFIGURE_TIMEOUT",
        }.get(stage, f"{stage}_TIMEOUT")
    return {
        "ok": False,
        "stage": stage,
        "elapsed_ms": _elapsed_ms(started),
        "error_class": type(exc).__name__,
        "detail": detail,
    }


def _capture_worker(conn: Connection, params: dict[str, Any]) -> None:
    """Own one read-only session and expose poll/close over a pipe.

    Multiple scripts deliberately share one Frida session.  Heavy consumers
    such as Amap can therefore merge their discovery lifecycle, while the
    parent retains a hard process boundary around attach, load and teardown.
    """
    started = time.monotonic()
    session = None
    loaded: list[Any] = []
    event_mu = threading.Lock()
    event_ready = threading.Condition(event_mu)
    events: list[dict[str, Any]] = []
    try:
        import frida

        def on_message(
            label: str,
            message: dict[str, Any],
            _data: Any,
        ) -> None:
            if message.get("type") == "send":
                event = message.get("payload")
            elif message.get("type") == "error":
                event = {
                    "ev": "js_error",
                    "desc": message.get("description"),
                }
            else:
                event = None
            if isinstance(event, dict):
                value = dict(event)
                value.setdefault("_script", label)
                value.setdefault("_pid", pid)
                value.setdefault("_wall", time.time())
                with event_ready:
                    value.setdefault("_event_id", len(events) + 1)
                    events.append(value)
                    event_ready.notify_all()

        try:
            device = frida.get_device_manager().add_remote_device(
                str(params["frida_host"]),
            )
        except Exception as exc:  # noqa: BLE001
            conn.send(_failure("TUNNEL_DOWN", started, exc))
            return
        pid = int(params.get("pid") or 0)
        if pid <= 0:
            bundle_id = str(params.get("bundle_id") or "")
            try:
                pid = next(
                    (
                        int(app.pid)
                        for app in device.enumerate_applications()
                        if getattr(app, "identifier", None) == bundle_id
                        and getattr(app, "pid", 0)
                    ),
                    0,
                )
            except Exception as exc:  # noqa: BLE001
                conn.send(_failure("PID_RESOLUTION_FAILED", started, exc))
                return
        if pid <= 0:
            conn.send({
                "ok": False,
                "stage": "APP_NOT_RUNNING",
                "elapsed_ms": _elapsed_ms(started),
                "error_class": "AppNotRunning",
                "detail": str(params.get("bundle_id") or "target app"),
            })
            return
        try:
            session = device.attach(pid)
        except Exception as exc:  # noqa: BLE001
            conn.send(_failure("ATTACH_FAILED", started, exc))
            return
        conn.send({
            "ok": True,
            "stage": "ATTACHED",
            "elapsed_ms": _elapsed_ms(started),
            "pid": pid,
        })
        try:
            configured = params.get("scripts")
            if not isinstance(configured, dict) or not configured:
                configured = {"capture": str(params["agent_source"])}
            for label, source in configured.items():
                script = session.create_script(str(source))
                script.on(
                    "message",
                    lambda message, data, script_label=str(label): on_message(
                        script_label, message, data,
                    ),
                )
                script.load()
                loaded.append(script)
                exports = script_exports(script)
                if str(label) != "capture":
                    continue
                capture_filter = params.get("capture_filter")
                if isinstance(capture_filter, dict):
                    configure_filter = (
                        getattr(exports, "configure_capture_filter", None)
                        or getattr(exports, "configureCaptureFilter", None)
                    )
                    if not callable(configure_filter):
                        raise RuntimeError(
                            "capture script does not export configure_capture_filter"
                        )
                    configure_filter(dict(capture_filter))
                install = (
                    getattr(exports, "install_capture_hooks", None)
                    or getattr(exports, "installCaptureHooks", None)
                )
                if callable(install):
                    delegate_class_names = [
                        str(value)
                        for value in params.get("delegate_class_names") or []
                        if str(value).strip()
                    ]
                    if delegate_class_names:
                        install({
                            "delegate_class_names": delegate_class_names,
                        })
                    else:
                        install()
                elif params.get("delegate_class_names"):
                    raise RuntimeError(
                        "capture script does not export install_capture_hooks"
                    )
        except Exception as exc:  # noqa: BLE001
            conn.send(_failure("SCRIPT_LOAD_FAILED", started, exc))
            return

        expected_ready = params.get("ready_event")
        matched_ready = None
        if isinstance(expected_ready, dict) and expected_ready:
            with event_ready:
                while matched_ready is None:
                    matched_ready = next(
                        (
                            dict(event)
                            for event in events
                            if all(event.get(key) == value for key, value in expected_ready.items())
                        ),
                        None,
                    )
                    if matched_ready is None:
                        event_ready.wait(0.2)

        conn.send({
            "ok": True,
            "stage": "READY",
            "elapsed_ms": _elapsed_ms(started),
            "pid": pid,
            "ready_event": matched_ready,
        })
        while True:
            if not conn.poll(0.2):
                continue
            command = conn.recv()
            StageGuard.check_if_bound(OperationClass.FRIDA)
            operation = (
                str(command.get("op") or "")
                if isinstance(command, dict)
                else str(command or "")
            )
            if operation == "poll":
                with event_mu:
                    batch = events[:]
                    del events[:len(batch)]
                conn.send({"ok": True, "events": batch})
                continue
            if operation == "close":
                conn.send({"ok": True})
                return
            conn.send({
                "ok": False,
                "stage": "UNKNOWN_COMMAND",
                "detail": operation,
            })
    except (EOFError, BrokenPipeError):
        return
    except Exception as exc:  # noqa: BLE001
        try:
            conn.send(_failure("WORKER_FAILED", started, exc))
        except (EOFError, BrokenPipeError):
            pass
    finally:
        for script in reversed(loaded):
            try:
                script.unload()
            except Exception:  # noqa: BLE001
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:  # noqa: BLE001
                pass
        conn.close()


@dataclass
class CaptureSession:
    """Parent-side proxy for a child-owned, bounded Frida capture session."""

    _process: Any
    _conn: Connection
    evidence: dict[str, Any]
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    teardown_timeout_s: float = DEFAULT_TEARDOWN_TIMEOUT_S
    _closed: bool = False
    _stage_binding: Any = field(default_factory=StageGuard.binding, repr=False, init=False)
    _mu: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _terminate(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(self.teardown_timeout_s)

    @_stage_frida
    def poll(self) -> list[dict[str, Any]]:
        with self._mu:
            if self._closed:
                return []
            try:
                self._conn.send({"op": "poll"})
                if not self._conn.poll(self.command_timeout_s):
                    self._terminate()
                    self._closed = True
                    self._conn.close()
                    raise FridaWorkerError({
                        "ok": False,
                        "stage": "POLL_TIMEOUT",
                        "elapsed_ms": round(self.command_timeout_s * 1000),
                        "error_class": "TimeoutError",
                        "detail": "capture worker did not answer poll",
                    })
                result = dict(self._conn.recv())
            except FridaWorkerError:
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                self._terminate()
                self._closed = True
                self._conn.close()
                raise FridaWorkerError({
                    "ok": False,
                    "stage": "WORKER_EXITED",
                    "elapsed_ms": 0,
                    "error_class": type(exc).__name__,
                    "detail": str(exc),
                }) from exc
            if not result.get("ok"):
                raise FridaWorkerError(result)
            return [
                dict(event)
                for event in result.get("events") or []
                if isinstance(event, dict)
            ]

    @_stage_frida
    def close(self) -> None:
        with self._mu:
            if self._closed:
                return
            self._closed = True
            try:
                if self._process.is_alive():
                    self._conn.send({"op": "close"})
                    if self._conn.poll(self.command_timeout_s):
                        self._conn.recv()
                    self._process.join(self.teardown_timeout_s)
            except (EOFError, BrokenPipeError, OSError):
                pass
            finally:
                self._terminate()
                self._conn.close()


@_stage_frida
def start_capture_session(
    *,
    pid: int = 0,
    bundle_id: str = "",
    frida_host: str,
    agent_source: str | None = None,
    scripts: Mapping[str, str] | None = None,
    ready_event: Mapping[str, Any] | None = None,
    capture_filter: dict[str, Any] | None = None,
    delegate_class_names: tuple[str, ...] | list[str] = (),
    attach_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    load_timeout_s: float = DEFAULT_SCRIPT_LOAD_TIMEOUT_S,
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    teardown_timeout_s: float = DEFAULT_TEARDOWN_TIMEOUT_S,
) -> CaptureSession:
    """Start one persistent capture with a hard, process-enforced ready bound.

    ``agent_source`` preserves the single-script interface.  ``scripts`` is the
    deeper multi-script interface: all sources share one attach/session and one
    lifecycle.  ``ready_event`` makes the load bound include the agent's own
    semantic readiness instead of treating ``script.load()`` as sufficient.
    """
    if (agent_source is None) == (scripts is None):
        raise ValueError("provide exactly one of agent_source or scripts")
    configured_scripts = (
        {str(label): str(source) for label, source in scripts.items()}
        if scripts is not None
        else {"capture": str(agent_source)}
    )
    if not configured_scripts:
        raise ValueError("scripts must not be empty")
    configured_delegate_names = [
        str(value)
        for value in delegate_class_names
        if str(value).strip()
    ]
    if (
        scripts is not None
        and "capture" not in configured_scripts
        and (capture_filter is not None or configured_delegate_names)
    ):
        raise ValueError(
            "capture_filter/delegate_class_names require a 'capture' script label"
        )
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = device_process(context,
        target=_capture_worker,
        args=(child, {
            "pid": int(pid),
            "bundle_id": str(bundle_id),
            "frida_host": str(frida_host),
            "scripts": configured_scripts,
            "ready_event": dict(ready_event or {}),
            "capture_filter": (
                dict(capture_filter)
                if isinstance(capture_filter, dict)
                else None
            ),
            "delegate_class_names": configured_delegate_names,
        }),
        daemon=True,
    )
    process.start()
    child.close()
    if not parent.poll(attach_timeout_s):
        process.terminate()
        process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError({
            "ok": False,
            "stage": "ATTACH_TIMEOUT",
            "elapsed_ms": round(attach_timeout_s * 1000),
            "error_class": "TimeoutError",
            "detail": "capture worker did not attach",
        })
    try:
        attached = dict(parent.recv())
    except EOFError as exc:
        process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError({
            "ok": False,
            "stage": "WORKER_EXITED",
            "elapsed_ms": 0,
            "error_class": "WorkerExited",
            "detail": "capture worker exited before attach",
        }) from exc
    if not attached.get("ok") or attached.get("stage") != "ATTACHED":
        process.join(teardown_timeout_s)
        if process.is_alive():
            process.terminate()
            process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError(attached)
    if not parent.poll(load_timeout_s):
        process.terminate()
        process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError({
            "ok": False,
            "stage": "SCRIPT_LOAD_TIMEOUT",
            "elapsed_ms": round(load_timeout_s * 1000),
            "error_class": "TimeoutError",
            "detail": "capture script did not become ready after attach",
        })
    try:
        result = dict(parent.recv())
    except EOFError as exc:
        process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError({
            "ok": False,
            "stage": "WORKER_EXITED",
            "elapsed_ms": 0,
            "error_class": "WorkerExited",
            "detail": "capture worker exited during script load",
        }) from exc
    if not result.get("ok"):
        process.join(teardown_timeout_s)
        if process.is_alive():
            process.terminate()
            process.join(teardown_timeout_s)
        parent.close()
        raise FridaWorkerError(result)
    return CaptureSession(
        process,
        parent,
        result,
        command_timeout_s,
        teardown_timeout_s,
    )


def _probe_worker(conn: Connection, host: str, app_name: str,
                  bundle_id: str | None = None) -> None:
    started = time.monotonic()
    try:
        from autophone.device.frida_contract_probe import probe

        matrix = probe(host=host, app_name=app_name, bundle_id=bundle_id)
        if matrix.get("attach_error"):
            result = _failure("ATTACH_FAILED", started, RuntimeError(str(matrix["attach_error"])))
        elif not matrix.get("session"):
            result = {
                "ok": False, "stage": "APP_NOT_ATTACHABLE", "elapsed_ms": _elapsed_ms(started),
                "error_class": "FridaProbeFailed", "detail": str(matrix.get("error") or "no session"),
            }
        elif not matrix.get("rpc_ping"):
            result = {
                "ok": False, "stage": "RPC_FAILED", "elapsed_ms": _elapsed_ms(started),
                "error_class": "FridaProbeFailed", "detail": str(matrix.get("rpc_ping_error") or "rpc ping failed"),
            }
        else:
            result = {"ok": True, "stage": "READY", "elapsed_ms": _elapsed_ms(started)}
        result["matrix"] = matrix
        conn.send(result)
    except Exception as exc:  # noqa: BLE001
        conn.send(_failure("PROBE_FAILED", started, exc))
    finally:
        conn.close()


@_stage_frida
def probe_contract(*, host: str, app_name: str, timeout_s: float = 8.0,
                   bundle_id: str | None = None) -> dict[str, Any]:
    """Run the existing full contract probe in a killable child process."""
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = device_process(context,target=_probe_worker, args=(child, host, app_name, bundle_id), daemon=True)
    process.start()
    child.close()
    if not parent.poll(timeout_s):
        process.terminate()
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
        return {
            "ok": False, "stage": "PROBE_TIMEOUT", "elapsed_ms": round(timeout_s * 1000),
            "error_class": "TimeoutError", "detail": "Frida contract probe timed out",
        }
    try:
        result = dict(parent.recv())
    except EOFError:
        result = {
            "ok": False, "stage": "WORKER_EXITED", "elapsed_ms": 0,
            "error_class": "WorkerExited", "detail": "Frida probe worker exited without a result",
        }
    finally:
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        if process.is_alive():
            process.terminate()
            process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
    return result


def _infrastructure_probe_worker(
    conn: Connection,
    host: str,
    process_name: str,
    identity_file: str,
    expected_identity_sha256: str,
) -> None:
    """Probe transport, process enumeration and attach on the target process.

    No business hook is ever installed. When ``identity_file`` is given the
    probe is no longer read-only either: it loads the tiny identity script into
    the attached process to read that file back, which is what binds the verdict
    to one guest instead of "some reachable Frida server".
    """
    started = time.monotonic()
    session = None
    identity_script = None
    matrix: dict[str, Any] = {
        "enum": False,
        "attach": False,
        "target": process_name,
        "pid": None,
        "identity_file": identity_file or None,
        "identity_sha256": None,
    }
    try:
        import frida

        try:
            device = frida.get_device_manager().add_remote_device(host)
        except Exception as exc:  # noqa: BLE001
            result = _failure("TUNNEL_DOWN", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        try:
            processes = list(device.enumerate_processes())
            matrix["enum"] = True
        except Exception as exc:  # noqa: BLE001
            result = _failure("ENUM_FAILED", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        target = next(
            (
                process
                for process in processes
                if str(getattr(process, "name", "") or "") == process_name
            ),
            None,
        )
        if target is None:
            conn.send({
                "ok": False,
                "stage": "TARGET_NOT_FOUND",
                "elapsed_ms": _elapsed_ms(started),
                "error_class": "ProcessNotFound",
                "detail": f"{process_name!r} was not enumerated",
                "matrix": matrix,
            })
            return
        pid = int(getattr(target, "pid", 0) or 0)
        matrix["pid"] = pid or None
        try:
            session = device.attach(pid)
            matrix["attach"] = True
        except Exception as exc:  # noqa: BLE001
            result = _failure("ATTACH_FAILED", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        if identity_file:
            try:
                identity_script = session.create_script(_IDENTITY_PROBE_SOURCE)
                identity_script.load()
                value = identity_script.exports_sync.readidentity(identity_file)
                actual_identity_sha256 = hashlib.sha256(
                    str(value).encode("utf-8")
                ).hexdigest()
                matrix["identity_sha256"] = actual_identity_sha256
                if actual_identity_sha256 != expected_identity_sha256:
                    result = _failure(
                        "IDENTITY_MISMATCH",
                        started,
                        RuntimeError(
                            "Frida identity marker does not match the target Rig"
                        ),
                    )
                    result["matrix"] = matrix
                    conn.send(result)
                    return
            except Exception as exc:  # noqa: BLE001
                result = _failure("IDENTITY_PROBE_FAILED", started, exc)
                result["matrix"] = matrix
                conn.send(result)
                return
            finally:
                if identity_script is not None:
                    try:
                        identity_script.unload()
                    except Exception:  # noqa: BLE001
                        pass
                    identity_script = None
        try:
            session.detach()
            session = None
        except Exception as exc:  # noqa: BLE001
            result = _failure("DETACH_FAILED", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        conn.send({
            "ok": True,
            "stage": "READY",
            "elapsed_ms": _elapsed_ms(started),
            "matrix": matrix,
        })
    except Exception as exc:  # noqa: BLE001
        result = _failure("PROBE_FAILED", started, exc)
        result["matrix"] = matrix
        try:
            conn.send(result)
        except (EOFError, BrokenPipeError):
            pass
    finally:
        if identity_script is not None:
            try:
                identity_script.unload()
            except Exception:  # noqa: BLE001
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:  # noqa: BLE001
                pass
        conn.close()


@_stage_frida
def probe_infrastructure(
    *,
    host: str,
    process_name: str = "SpringBoard",
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    identity_file: str = "",
    expected_identity_sha256: str = "",
) -> dict[str, Any]:
    """Return a bounded, non-business Frida attach capability verdict.

    With ``identity_file``/``expected_identity_sha256`` the probe additionally
    loads the identity script inside the attached process, so each call injects
    a short-lived script into that process (SpringBoard by default).

    只供人工诊断使用：平台自动入口（``start_vphone.sh``、``doctor``、自动恢复、常规预检）
    改用 ``probe_enumeration``，不再 attach SpringBoard。attach 并 detach SpringBoard 后
    会留下 Frida agent 映射残留，之后的安装会被安装前残留检查拒绝，直到 SpringBoard 重启。
    """
    if bool(identity_file) != bool(expected_identity_sha256):
        raise ValueError(
            "identity_file and expected_identity_sha256 must be supplied together"
        )
    normalized_identity_sha256 = str(expected_identity_sha256).lower()
    if normalized_identity_sha256 and (
        len(normalized_identity_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_identity_sha256)
    ):
        raise ValueError("expected_identity_sha256 must be a lowercase SHA-256 hex digest")
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = device_process(context,
        target=_infrastructure_probe_worker,
        args=(
            child,
            str(host),
            str(process_name),
            str(identity_file),
            normalized_identity_sha256,
        ),
        daemon=True,
    )
    process.start()
    child.close()
    if not parent.poll(timeout_s):
        process.terminate()
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
        return {
            "ok": False,
            "stage": "PROBE_TIMEOUT",
            "elapsed_ms": round(timeout_s * 1000),
            "error_class": "TimeoutError",
            "detail": "Frida infrastructure attach probe timed out",
            "matrix": {
                "enum": None,
                "attach": None,
                "target": process_name,
                "pid": None,
                "identity_file": identity_file or None,
                "identity_sha256": None,
            },
        }
    try:
        result = dict(parent.recv())
    except EOFError:
        result = {
            "ok": False,
            "stage": "WORKER_EXITED",
            "elapsed_ms": 0,
            "error_class": "WorkerExited",
            "detail": "Frida infrastructure probe exited without a result",
            "matrix": {
                "enum": None,
                "attach": None,
                "target": process_name,
                "pid": None,
                "identity_file": identity_file or None,
                "identity_sha256": None,
            },
        }
    finally:
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        if process.is_alive():
            process.terminate()
            process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
    return result


ENUMERATE_ONLY_MODE = "enumerate_only"


def _enumeration_probe_worker(conn: Connection, host: str, process_name: str) -> None:
    """Probe transport and process enumeration only; never attaches any process.

    BL-232 阶段 1（R1）：判据为 frida-server 能列出进程，且列表中有 ``process_name``。
    本 worker 不调用 ``device.attach``，不向任何进程注入脚本。
    """
    started = time.monotonic()
    matrix: dict[str, Any] = {
        "mode": ENUMERATE_ONLY_MODE,
        "enum": False,
        "attach": None,
        "target": process_name,
        "pid": None,
        "processes_listed": None,
    }
    try:
        import frida

        try:
            device = frida.get_device_manager().add_remote_device(host)
        except Exception as exc:  # noqa: BLE001
            result = _failure("TUNNEL_DOWN", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        try:
            processes = list(device.enumerate_processes())
            matrix["enum"] = True
            matrix["processes_listed"] = len(processes)
        except Exception as exc:  # noqa: BLE001
            result = _failure("ENUM_FAILED", started, exc)
            result["matrix"] = matrix
            conn.send(result)
            return
        target = next(
            (
                process
                for process in processes
                if str(getattr(process, "name", "") or "") == process_name
            ),
            None,
        )
        if target is None:
            conn.send({
                "ok": False,
                "stage": "TARGET_NOT_FOUND",
                "elapsed_ms": _elapsed_ms(started),
                "error_class": "ProcessNotFound",
                "detail": f"{process_name!r} was not enumerated",
                "matrix": matrix,
            })
            return
        matrix["pid"] = int(getattr(target, "pid", 0) or 0) or None
        conn.send({
            "ok": True,
            "stage": "READY",
            "elapsed_ms": _elapsed_ms(started),
            "matrix": matrix,
        })
    except Exception as exc:  # noqa: BLE001
        result = _failure("PROBE_FAILED", started, exc)
        result["matrix"] = matrix
        try:
            conn.send(result)
        except (EOFError, BrokenPipeError):
            pass


@_stage_frida
def probe_enumeration(
    *,
    host: str,
    process_name: str = "SpringBoard",
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
) -> dict[str, Any]:
    """Return a bounded Frida reachability verdict without attaching any process.

    BL-232 阶段 1（R1，评估文档 §4.5）：``start_vphone.sh``（含自动恢复）、
    ``doctor --fast/--full`` 与常规预检只证明 frida-server 可达且能列出
    ``process_name``，不再证明注入能力。注入能力由 Rig 准备对被测 App 的契约探针
    （``rig/maintenance.py`` ``_probe_frida``）证明。结果 ``matrix.mode`` 为
    ``enumerate_only``，``matrix.attach`` 恒为 ``None``。
    """
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = device_process(context,
        target=_enumeration_probe_worker,
        args=(child, str(host), str(process_name)),
        daemon=True,
    )
    process.start()
    child.close()
    unfinished_matrix = {
        "mode": ENUMERATE_ONLY_MODE,
        "enum": None,
        "attach": None,
        "target": process_name,
        "pid": None,
        "processes_listed": None,
    }
    if not parent.poll(timeout_s):
        process.terminate()
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
        return {
            "ok": False,
            "stage": "PROBE_TIMEOUT",
            "elapsed_ms": round(timeout_s * 1000),
            "error_class": "TimeoutError",
            "detail": "Frida enumeration probe timed out",
            "matrix": unfinished_matrix,
        }
    try:
        result = dict(parent.recv())
    except EOFError:
        result = {
            "ok": False,
            "stage": "WORKER_EXITED",
            "elapsed_ms": 0,
            "error_class": "WorkerExited",
            "detail": "Frida enumeration probe exited without a result",
            "matrix": unfinished_matrix,
        }
    finally:
        process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        if process.is_alive():
            process.terminate()
            process.join(DEFAULT_TEARDOWN_TIMEOUT_S)
        parent.close()
    return result
