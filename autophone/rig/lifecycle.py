"""Exact-target VM lifecycle for one :class:`DeviceTarget`."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from autophone.device.stage_guard import OperationClass, guarded_device_operation

from .process_guard import AgentProcessError, process_arguments
from .target import DeviceTarget, TargetConfigurationError


class RigLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command: str
    parent_pid: int | None = None


@dataclass(frozen=True)
class LifecycleReceipt:
    action: str
    status: str
    host_id: str
    rig_id: str
    rig_generation: int
    affected_pids: tuple[int, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["affected_pids"] = list(self.affected_pids)
        return value


_CAFFEINATE = "/usr/bin/caffeinate"
_VPHONE_EXECUTABLE_RE = re.compile(r"(?:^|/)(?:vphone-cli|vphone-vm)(?:\s|$)")

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ListProcesses = Callable[[], Sequence[ProcessInfo]]
SendSignal = Callable[[int, int], None]
ProcessAlive = Callable[[int], bool]


def _local_processes() -> tuple[ProcessInfo, ...]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    rows: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            pid, command = int(fields[0]), fields[2]
            if _VPHONE_EXECUTABLE_RE.search(command):
                try:
                    arguments = process_arguments(pid)
                except AgentProcessError as exc:
                    raise RigLifecycleError(f"cannot inspect VM argv for PID {pid}") from exc
                if not arguments:
                    continue
                # ps flattens argv without quoting paths containing spaces.
                command = shlex.join(arguments)
            rows.append(ProcessInfo(pid, command, parent_pid=int(fields[1])))
    return tuple(rows)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_config(command: str) -> Path | None:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    for index, argument in enumerate(arguments):
        if argument == "--config" and index + 1 < len(arguments):
            return Path(arguments[index + 1]).expanduser().resolve()
        if argument.startswith("--config="):
            return Path(argument.partition("=")[2]).expanduser().resolve()
    return None


def _command_arguments(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return ()


def _option_value(arguments: Sequence[str], *names: str) -> str | None:
    for index, argument in enumerate(arguments[:-1]):
        if argument in names:
            return arguments[index + 1]
    return None


def _has_option_pair(arguments: Sequence[str], name: str, value: str) -> bool:
    return any(
        argument == name and arguments[index + 1] == value
        for index, argument in enumerate(arguments[:-1])
    )


def _iproxy_endpoint(arguments: Sequence[str]) -> tuple[int, int] | None:
    """Parse both iproxy argument shapes, same as tools.phase1.ssh_bringup.

    A helper left behind by the legacy ``iproxy HOST_PORT GUEST_PORT`` form has
    to stay recognizable here, otherwise stop() walks past it and the Rig keeps
    an SSH forwarder pointed at a guest that is gone.
    """

    if not arguments or Path(arguments[0]).name != "iproxy":
        return None
    for argument in arguments[1:]:
        local, separator, guest = argument.partition(":")
        if separator and local.isdigit() and guest.isdigit():
            return int(local), int(guest)
    if len(arguments) >= 3:
        local, guest = arguments[-2:]
        if local.isdigit() and guest.isdigit():
            return int(local), int(guest)
    return None


def _read_predicted_udid(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RigLifecycleError(f"cannot read predicted USB identity {path}: {exc}") from exc
    for line in lines:
        key, separator, value = line.replace("=", ":", 1).partition(":")
        if separator and key.strip() == "UDID" and value.strip():
            return value.strip()
    raise RigLifecycleError(f"UDID is missing from {path}")


class RigLifecycle:
    """Start, inspect and stop one Rig without process-name-wide mutations."""

    def __init__(
        self,
        target: DeviceTarget,
        *,
        repository: Path | None = None,
        run_command: RunCommand = subprocess.run,
        list_processes: ListProcesses = _local_processes,
        send_signal: SendSignal = os.kill,
        process_alive: ProcessAlive = _process_alive,
        sleep: Callable[[float], None] = time.sleep,
        start_process: Callable = subprocess.Popen,
        probe_uds: Callable | None = None,
    ) -> None:
        self.target = target
        self.repository = (repository or Path(__file__).resolve().parents[2]).resolve()
        self._run_command = run_command
        self._list_processes = list_processes
        self._send_signal = send_signal
        self._process_alive = process_alive
        self._sleep = sleep
        self._start_process = start_process
        self._probe_uds = probe_uds

    @guarded_device_operation(OperationClass.VM, identity=lambda a: {
        "rig_id": a["self"].target.rig_id,
        "generation": a["self"].target.rig_generation,
        "socket_path": a["self"].target.socket,
    })
    def start(
        self,
        *,
        allow_multi_vm: bool = False,
        timeout_s: float = 900.0,
        headless: bool = False,
    ) -> LifecycleReceipt:
        self._validate_target_files()
        self.target.ensure_local_directories()
        if getattr(self.target, "protocol", "legacy") == "upstream_v1":
            return self._start_upstream(allow_multi_vm=allow_multi_vm, timeout_s=timeout_s, headless=headless)
        script = self.repository / "scripts" / "start_vphone.sh"
        if not script.is_file():
            raise RigLifecycleError(f"start script does not exist: {script}")
        environment = {
            **os.environ,
            **self.target.process_environment(),
            "AUTOPHONE_EXPERIMENTAL_MULTI_VM": "1" if allow_multi_vm else "0",
        }
        try:
            completed = self._run_command(
                [str(script)],
                cwd=str(self.repository),
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RigLifecycleError(
                f"Rig start timed out for {self.target.rig_id} after {timeout_s}s"
            ) from exc
        except OSError as exc:
            raise RigLifecycleError(
                f"cannot start Rig {self.target.rig_id}: {exc}"
            ) from exc
        if completed.returncode != 0:
            streams = (
                ("stdout", completed.stdout),
                ("stderr", completed.stderr),
            )
            detail = "\n".join(
                f"[{name}]\n{value.strip()}"
                for name, value in streams
                if value and value.strip()
            )[-2000:]
            raise RigLifecycleError(
                f"Rig start failed for {self.target.rig_id} "
                f"(rc={completed.returncode}): {detail}"
            )
        return self._receipt("start", "running", detail=completed.stdout.strip()[-2000:])

    @guarded_device_operation(OperationClass.READ, identity=lambda a: {
        "rig_id": a["self"].target.rig_id,
        "generation": a["self"].target.rig_generation,
        "socket_path": a["self"].target.socket,
    })
    def status(self) -> LifecycleReceipt:
        pids = tuple(process.pid for process in self._target_vphone_processes())
        status = "running" if pids and self.target.socket.exists() else "stopped"
        return self._receipt("status", status, affected_pids=pids)

    @guarded_device_operation(OperationClass.VM, identity=lambda a: {
        "rig_id": a["self"].target.rig_id,
        "generation": a["self"].target.rig_generation,
        "socket_path": a["self"].target.socket,
    })
    def stop(self, *, timeout_s: float = 20.0) -> LifecycleReceipt:
        try:
            health = self._run_command(
                [
                    os.environ.get("PYTHON_BIN", sys.executable),
                    "-m",
                    "tools.phase1.health_daemon",
                    "stop",
                    "--sock",
                    str(self.target.socket),
                    "--pid-file",
                    str(self.target.health_pid_file),
                ],
                cwd=str(self.repository),
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RigLifecycleError(
                f"cannot stop health daemon for {self.target.rig_id}: {exc}"
            ) from exc
        if health.returncode != 0:
            raise RigLifecycleError(
                f"cannot stop health daemon for {self.target.rig_id}: "
                f"{(health.stderr or health.stdout).strip()}"
            )

        affected: list[int] = []
        processes = self._process_snapshot()
        for process in processes:
            if self._is_target_helper(process.command):
                if self._send_term(process.pid):
                    affected.append(process.pid)

        vphones = self._target_vphone_processes(processes)
        if getattr(self.target, "protocol", "legacy") == "upstream_v1":
            command = self._upstream_command("stop", timeout=max(1, int(timeout_s)))
            result = self._run_command(command, cwd=str(self.repository),
                                       env={**os.environ, **self.target.process_environment()},
                                       capture_output=True, text=True, timeout=timeout_s + 15)
            if result.returncode != 0:
                raise RigLifecycleError(f"upstream stop failed: {result.stderr or result.stdout}")
            affected.extend(process.pid for process in vphones)
            return self._receipt("stop", "stopped", affected_pids=tuple(affected))
        for process in vphones:
            if self._send_term(process.pid):
                affected.append(process.pid)

        deadline = time.monotonic() + timeout_s
        remaining = {process.pid for process in vphones}
        while remaining and time.monotonic() < deadline:
            remaining = {pid for pid in remaining if self._process_alive(pid)}
            if remaining:
                self._sleep(0.2)
        if remaining:
            raise RigLifecycleError(f"target vphone processes did not stop: {sorted(remaining)}")
        return self._receipt(
            "stop",
            "stopped",
            affected_pids=tuple(sorted(set(affected))),
        )

    def _upstream_command(self, action: str, *, timeout: int = 20) -> list[str]:
        from .upstream import lifecycle_command
        return lifecycle_command(action, bundle=self.target.core_bundle,
                                 vm_dir=self.target.vm_dir, timeout=timeout)

    def _start_upstream(self, *, allow_multi_vm: bool, timeout_s: float, headless: bool) -> LifecycleReceipt:
        from autophone.device.session import probe_uds_state
        environment = {**os.environ, **self.target.process_environment()}
        environment.setdefault("AUTOPHONE_CONTROL_PY", sys.executable)
        preflight = self._run_command(self._upstream_command("preflight"),
                                      cwd=str(self.repository), env=environment,
                                      capture_output=True, text=True, timeout=15)
        if preflight.returncode != 0:
            raise RigLifecycleError(f"upstream preflight failed: {preflight.stderr or preflight.stdout}")
        processes = self._process_snapshot()
        target = self._target_vphone_processes(processes)
        others = [p for p in processes if _command_arguments(p.command)
                  and Path(_command_arguments(p.command)[0]).name in {"vphone-cli", "vphone-vm"}
                  and _command_config(p.command) is not None and p not in target]
        if others and not allow_multi_vm:
            raise RigLifecycleError("another VM is running; enable allow_multi_vm explicitly")
        launcher = None
        if not target:
            command = [sys.executable, "-m", "autophone.rig.upstream", "launch", "--bundle",
                       str(self.target.core_bundle), "--vm-dir", str(self.target.vm_dir)]
            if headless:
                command.append("--headless")
            with (self.target.log_dir / "vphone.log").open("ab") as log:
                launcher = self._start_process(command, cwd=str(self.repository), env=environment,
                                               stdin=subprocess.DEVNULL, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
        probe = self._probe_uds or probe_uds_state
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if launcher is not None and launcher.poll() is not None:
                    raise RigLifecycleError(f"upstream launcher exited: {launcher.returncode}")
                state = probe(self.target.socket, protocol="upstream_v1", timeout=min(3, max(.01, deadline - time.monotonic())))
                if state.get("state") == "guest_ready":
                    health = self._run_command(
                        [sys.executable, "-m", "tools.phase1.health_daemon", "ensure",
                         "--sock", str(self.target.socket), "--state-file", str(self.target.health_state_file),
                         "--pid-file", str(self.target.health_pid_file), "--log-file", str(self.target.health_log_file),
                         "--on-down", str(self.repository / "scripts/on_vphone_down.sh")],
                        cwd=str(self.repository), env=environment, capture_output=True, text=True, timeout=20,
                    )
                    if health.returncode != 0:
                        raise RigLifecycleError(f"health daemon startup failed: {health.stderr or health.stdout}")
                    self._hold_host_idle_sleep(environment)
                    return self._receipt("start", "running")
                self._sleep(.2)
            raise RigLifecycleError("upstream guest readiness timed out")
        except (Exception, KeyboardInterrupt) as exc:
            cleanup_error = None
            if launcher is not None:
                try:
                    self._cleanup_upstream_launch(launcher)
                except (OSError, subprocess.SubprocessError) as stop_exc:
                    cleanup_error = str(stop_exc)
            if isinstance(exc, KeyboardInterrupt) and cleanup_error is None:
                raise
            detail = str(exc) or type(exc).__name__
            if cleanup_error:
                detail += f"; owned VM cleanup failed: {cleanup_error}"
            raise RigLifecycleError(detail) from exc

    def _hold_host_idle_sleep(self, environment) -> None:
        """Keep macOS from idle-sleeping under this VM; the hold ends when the VM exits.

        Upstream vphone-vm holds no power assertion, so an unattended host can sleep
        and stop every Rig on it.
        """
        processes = self._process_snapshot()
        vms = [process.pid for process in self._target_vphone_processes(processes)
               if Path(_command_arguments(process.command)[0]).name == "vphone-vm"]
        if not vms:
            raise RigLifecycleError("cannot hold host idle sleep: guest is ready but no vphone-vm process was found")
        held = {tuple(process.command.split()) for process in processes}
        for pid in vms:
            command = [_CAFFEINATE, "-i", "-w", str(pid)]
            if tuple(command) in held:
                continue
            try:
                self._start_process(command, env=environment, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            except OSError as exc:
                raise RigLifecycleError(f"cannot hold host idle sleep for pid {pid}: {exc}") from exc

    def _cleanup_upstream_launch(self, launcher) -> None:
        # The wrapper execs vphone-cli, retaining its PID. A rejected launcher
        # owns no VM; directory-based stop could terminate a concurrent winner.
        if launcher.poll() is not None:
            return
        executable = self.target.core_bundle / "Contents/MacOS/vphone-vm"
        owned = tuple(
            process for process in self._target_vphone_processes()
            if process.parent_pid == launcher.pid
            and Path(_command_arguments(process.command)[0]).resolve() == executable.resolve()
        )
        if not owned:
            launcher.terminate()
            launcher.wait(timeout=5)
            return
        for process in owned:
            self._send_signal(process.pid, signal.SIGINT)
        deadline = time.monotonic() + 20
        while True:
            current = {process.pid: process for process in self._process_snapshot()}
            remaining = [process for process in owned if current.get(process.pid) == process]
            if not remaining:
                break
            if time.monotonic() >= deadline:
                # Recheck ancestry and command before escalation; never target
                # another instance that replaced this launch under the same name.
                for process in remaining:
                    self._send_signal(process.pid, signal.SIGKILL)
                break
            self._sleep(.05)
        launcher.wait(timeout=5)

    def _validate_target_files(self) -> None:
        if not self.target.config_path.is_file():
            raise RigLifecycleError(f"VM config does not exist: {self.target.config_path}")
        predicted = _read_predicted_udid(self.target.vm_dir / "udid-prediction.txt")
        if predicted != self.target.expected_usb_udid:
            raise RigLifecycleError(
                "DeviceTarget expected_usb_udid does not match the VM prediction: "
                f"target={self.target.expected_usb_udid} predicted={predicted}"
            )

    def _target_vphone_processes(
        self,
        processes: Iterable[ProcessInfo] | None = None,
    ) -> tuple[ProcessInfo, ...]:
        expected = self.target.config_path.resolve()
        return tuple(
            process
            for process in (processes if processes is not None else self._process_snapshot())
            if _command_arguments(process.command)
            and Path(_command_arguments(process.command)[0]).name in {"vphone-cli", "vphone-vm"}
            and _command_config(process.command) == expected
        )

    def _process_snapshot(self) -> tuple[ProcessInfo, ...]:
        try:
            return tuple(self._list_processes())
        except (OSError, subprocess.SubprocessError) as exc:
            raise RigLifecycleError(
                f"cannot inspect processes for {self.target.rig_id}: {exc}"
            ) from exc

    def _send_term(self, pid: int) -> bool:
        try:
            self._send_signal(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise RigLifecycleError(
                f"cannot stop process {pid} for {self.target.rig_id}: {exc}"
            ) from exc
        return True

    def _is_target_helper(self, command: str) -> bool:
        arguments = _command_arguments(command)
        endpoint = _iproxy_endpoint(arguments)
        iproxy = (
            endpoint is not None
            and endpoint[0] == self.target.ssh_port
            and _option_value(arguments, "-u", "--udid")
            == self.target.expected_usb_udid
        )
        frida_mapping = f"{self.target.frida_port}:127.0.0.1:{self.target.frida_guest_port}"
        frida = (
            frida_mapping in arguments
            and _has_option_pair(arguments, "-p", str(self.target.ssh_port))
        )
        return iproxy or frida

    def _receipt(
        self,
        action: str,
        status: str,
        *,
        affected_pids: tuple[int, ...] = (),
        detail: str = "",
    ) -> LifecycleReceipt:
        return LifecycleReceipt(
            action=action,
            status=status,
            host_id=self.target.host_id,
            rig_id=self.target.rig_id,
            rig_generation=self.target.rig_generation,
            affected_pids=affected_pids,
            detail=detail,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--allow-multi-vm", action="store_true")
    args = parser.parse_args(argv)
    try:
        lifecycle = RigLifecycle(DeviceTarget.load(args.target))
        if args.action == "start":
            receipt = lifecycle.start(allow_multi_vm=args.allow_multi_vm)
        elif args.action == "stop":
            receipt = lifecycle.stop()
        else:
            receipt = lifecycle.status()
    except (RigLifecycleError, TargetConfigurationError) as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
