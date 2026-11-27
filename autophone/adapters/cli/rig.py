"""Manage upstream Rig target configs and run local stacks in one terminal."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx2

from autophone.rig.lifecycle import RigLifecycle, RigLifecycleError, _read_predicted_udid
from autophone.rig.target import DeviceTarget, TargetConfigurationError
from autophone.rig.region_probe import stage_region_probe

DEFAULT_SSH_PORT_BASE = 2222
DEFAULT_FRIDA_PORT_BASE = 27042


def _default_vm_library_root() -> Path:
    configured = os.getenv("VPHONE_LIBRARY_ROOT", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".vphone" / "machines"


def _used_host_ports(library_root: Path, *, exclude_target: Path | None = None) -> tuple[set[int], set[int]]:
    ssh_ports: set[int] = set()
    frida_ports: set[int] = set()
    if not library_root.is_dir():
        return ssh_ports, frida_ports
    excluded = exclude_target.resolve() if exclude_target is not None else None
    for candidate in sorted(library_root.glob("*/autophone/target.json")):
        if excluded is not None and candidate.resolve() == excluded:
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            if isinstance(data.get("ssh_port"), int):
                ssh_ports.add(data["ssh_port"])
            if isinstance(data.get("frida_port"), int):
                frida_ports.add(data["frida_port"])
    return ssh_ports, frida_ports


def _allocate_ports(
    library_root: Path,
    *,
    slot_index: int | None = None,
    ssh_port: int | None = None,
    frida_port: int | None = None,
    exclude_target: Path | None = None,
) -> tuple[int, int]:
    if slot_index is not None:
        if slot_index < 0:
            raise TargetConfigurationError("--slot-index must be non-negative")
        return (
            ssh_port if ssh_port is not None else DEFAULT_SSH_PORT_BASE + slot_index,
            frida_port if frida_port is not None else DEFAULT_FRIDA_PORT_BASE + slot_index,
        )
    used_ssh, used_frida = _used_host_ports(library_root, exclude_target=exclude_target)
    if ssh_port is not None and frida_port is not None:
        return ssh_port, frida_port
    offset = 0
    while True:
        cand_ssh = ssh_port if ssh_port is not None else DEFAULT_SSH_PORT_BASE + offset
        cand_frida = frida_port if frida_port is not None else DEFAULT_FRIDA_PORT_BASE + offset
        if cand_ssh != cand_frida and (
            (ssh_port is not None or cand_ssh not in used_ssh)
            and (frida_port is not None or cand_frida not in used_frida)
        ):
            return cand_ssh, cand_frida
        offset += 1


def init_target(
    rig_id: str,
    *,
    bundle: Path,
    vm_dir: Path | None = None,
    out: Path | None = None,
    host_id: str | None = None,
    udid: str | None = None,
    slot_index: int | None = None,
    ssh_port: int | None = None,
    frida_port: int | None = None,
    frida_guest_port: int = DEFAULT_FRIDA_PORT_BASE,
    rig_generation: int = 1,
) -> tuple[DeviceTarget, Path]:
    """Derive, validate, and atomically write an upstream_v1 DeviceTarget JSON."""
    clean_rig = str(rig_id or "").strip()
    if not clean_rig:
        raise TargetConfigurationError("rig_id must be non-empty")
    bundle_path = Path(bundle).expanduser().resolve()
    vm_path = (
        Path(vm_dir).expanduser().resolve()
        if vm_dir is not None
        else (_default_vm_library_root() / clean_rig).resolve()
    )
    runtime_dir = vm_path / "autophone"
    out_path = Path(out).expanduser().resolve() if out is not None else (runtime_dir / "target.json")

    expected_udid = (udid or "").strip()
    if not expected_udid:
        prediction_file = vm_path / "udid-prediction.txt"
        try:
            expected_udid = _read_predicted_udid(prediction_file)
        except RigLifecycleError as exc:
            raise TargetConfigurationError(
                f"could not read UDID from {prediction_file}: {exc}; "
                "ensure the VM directory exists or pass --udid explicitly"
            ) from exc

    resolved_ssh, resolved_frida = _allocate_ports(
        vm_path.parent,
        slot_index=slot_index,
        ssh_port=ssh_port,
        frida_port=frida_port,
        exclude_target=out_path,
    )
    resolved_host = (host_id or os.getenv("AUTOPHONE_HOST_ID") or socket.gethostname() or "mac-host").strip()
    values = {
        "protocol": "upstream_v1",
        "core_bundle": str(bundle_path),
        "cli_dir": str(bundle_path / "Contents" / "MacOS"),
        "host_id": resolved_host,
        "rig_id": clean_rig,
        "rig_generation": rig_generation,
        "vm_dir": str(vm_path),
        "socket": str(vm_path / "vphone.sock"),
        "expected_usb_udid": expected_udid,
        "ssh_host": "127.0.0.1",
        "ssh_port": resolved_ssh,
        "frida_host": "127.0.0.1",
        "frida_port": resolved_frida,
        "frida_guest_port": frida_guest_port,
        "runtime_dir": str(runtime_dir),
        "log_dir": str(runtime_dir / "logs"),
        "temp_dir": str(runtime_dir / "tmp"),
        "package_cache": str(runtime_dir / "tmp" / "package-cache"),
    }
    target = DeviceTarget.from_mapping(values)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(target.public_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return target, out_path


def configured_targets(paths: list[Path]) -> list[DeviceTarget]:
    targets = [DeviceTarget.load(path) for path in paths]
    if any(target.protocol != "upstream_v1" for target in targets):
        raise ValueError("rig up requires upstream_v1 targets")
    if len({target.host_id for target in targets}) != 1:
        raise ValueError("rig up targets must share a host ID")
    for field in ("rig_id", "vm_dir", "socket", "expected_usb_udid", "runtime_dir", "log_dir", "temp_dir",
                  "package_cache"):
        values = [getattr(target, field) for target in targets]
        if values and isinstance(values[0], Path):
            values = [value.resolve() for value in values]
        if len(set(values)) != len(targets):
            raise ValueError(f"rig up requires distinct target {field} values")
    endpoints = [(t.ssh_host, t.ssh_port) for t in targets] + [(t.frida_host, t.frida_port) for t in targets]
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("rig up requires distinct host endpoints")
    return targets


def _stop_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_until(processes, ready, description, timeout=30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for label, process in processes.items():
            if process.poll() is not None:
                raise RuntimeError(f"{label} exited with status {process.returncode}")
        if ready():
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {description}")


def _require_free_ports(*ports: int) -> None:
    if len(set(ports)) != len(ports) or any(not 1 <= port <= 65535 for port in ports):
        raise ValueError("choose distinct Control Plane and dashboard TCP ports")
    for port in ports:
        try:
            with socket.socket() as test:
                test.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError(f"loopback port {port} is unavailable") from exc


def _start_ephemeral_postgres(stack: ExitStack) -> str:
    pg_config = shutil.which("pg_config")
    if not pg_config:
        raise ValueError("--ephemeral-db requires PostgreSQL tools (pg_config, initdb, pg_ctl) on PATH")
    bindir = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
    initdb = bindir / "initdb"
    pg_ctl = bindir / "pg_ctl"
    if not initdb.is_file() or not pg_ctl.is_file():
        raise ValueError(f"--ephemeral-db requires initdb and pg_ctl in {bindir}")
    work = Path(stack.enter_context(TemporaryDirectory(prefix="autophone-pg-")))
    db_dir = work / "db"
    subprocess.run(
        [str(initdb), "-D", str(db_dir), "-U", "autophone_local",
         "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    subprocess.run(
        [str(pg_ctl), "-D", str(db_dir), "-l", str(work / "server.log"),
         "-o", f"-c listen_addresses='' -k {work}", "-w", "start"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )

    def stop_pg() -> None:
        if (db_dir / "postmaster.pid").is_file():
            subprocess.run(
                [str(pg_ctl), "-D", str(db_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )

    stack.callback(stop_pg)
    return f"dbname=postgres host={work} user=autophone_local"


def _should_decline_usb_trust(target: DeviceTarget, explicit: bool) -> bool:
    if explicit:
        return True
    runtime_dir = getattr(target, "runtime_dir", None)
    return bool(runtime_dir is not None and (Path(runtime_dir) / "decline-usb-trust").is_file())


def _run_usb_trust_check(
    target: DeviceTarget,
    *,
    wait_s: float,
    control_http: str | None = None,
    dashboard_http: str | None = None,
    dashboard_auth: tuple[str, str] | None = None,
) -> dict:
    from autophone.application.device_session import RemoteDeviceSessions
    from tools.phase1.dismiss_usb_trust_prompt import (
        DashboardDeviceSessions,
        ensure_usb_trust_checked,
    )

    if control_http:
        client = RemoteDeviceSessions(control_http)
    elif dashboard_http:
        client = DashboardDeviceSessions(dashboard_http, auth=dashboard_auth)
    else:
        raise ValueError("USB trust check requires either control_http or dashboard_http")
    result = ensure_usb_trust_checked(
        target,
        client,
        wait_s=wait_s,
        control_http=control_http,
    )
    print(
        f"Rig {target.rig_id}: USB trust prompt check {result.get('status')} "
        f"(boot={result.get('boot_uuid')})",
        file=sys.stderr,
        flush=True,
    )
    return result


def _run_local_single_stack(
    target: DeviceTarget,
    dashboard_port: int,
    *,
    allow_multi_vm: bool = False,
    springboard_region_probe: Path | None = None,
    decline_usb_trust: bool = False,
    usb_trust_wait: float = 25.0,
) -> None:
    _require_free_ports(dashboard_port)
    dashboard = f"http://127.0.0.1:{dashboard_port}"
    env = {
        **os.environ,
        "AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1",
        "AUTOPHONE_CORE_BUNDLE": str(target.core_bundle),
        "VPHONE_LIBRARY_ROOT": str(target.vm_dir.parent),
        "VM_DIR": str(target.vm_dir),
        "VPHONE_SOCK": str(target.socket),
        "AP_DEVICE": target.rig_id,
    }
    for key in (
        "AUTOPHONE_DATABASE_URL", "AUTOPHONE_CONTROL_HTTP", "AUTOPHONE_CONTROL_WS",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(key, None)
    auth = os.getenv("AUTOPHONE_AUTH")
    auth_pair = auth.split(",", 1)[0].partition(":") if auth else None
    dashboard_auth = (auth_pair[0].strip(), auth_pair[2].strip()) if auth_pair else None

    lifecycle = RigLifecycle(target)
    if lifecycle.status().affected_pids:
        raise ValueError("rig up requires stopped VMs; use the manual lifecycle for existing processes")
    with ExitStack() as stack, httpx2.Client(timeout=2, trust_env=False) as client:
        processes: dict[str, subprocess.Popen] = {}
        lifecycle.start(allow_multi_vm=allow_multi_vm, timeout_s=300, headless=True)
        stack.callback(lifecycle.stop)
        if springboard_region_probe is not None:
            env.update(stage_region_probe(stack, target, springboard_region_probe))
        process = subprocess.Popen(
            [sys.executable, "-m", "autophone.cli", "serve", "--no-control-plane",
             "--host", "127.0.0.1", "--port", str(dashboard_port)],
            env=env, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        processes["dashboard"] = process
        stack.callback(_stop_child, process)

        def ready() -> bool:
            try:
                response = client.get(dashboard + "/api/platform/targets", auth=dashboard_auth)
                data = response.json() if response.is_success else {}
            except (httpx2.RequestError, ValueError):
                return False
            return data.get("mode") == "local" and any(
                row.get("rig_id") == target.rig_id for row in data.get("targets", [])
            )

        _wait_until(processes, ready, "local dashboard and Rig")
        if _should_decline_usb_trust(target, decline_usb_trust):
            _run_usb_trust_check(
                target,
                wait_s=usb_trust_wait,
                dashboard_http=dashboard,
                dashboard_auth=dashboard_auth,
            )
        print(f"Dashboard (local single-VM mode): {dashboard}", flush=True)
        while True:
            _wait_until(processes, lambda: True, "running dashboard")
            time.sleep(0.5)


def run_stack(
    paths: list[Path],
    control_port: int,
    dashboard_port: int,
    *,
    allow_multi_vm: bool = False,
    springboard_region_probe: Path | None = None,
    ephemeral_db: bool = False,
    decline_usb_trust: bool = False,
    usb_trust_wait: float = 25.0,
) -> None:
    if not 0 <= usb_trust_wait <= 60:
        raise ValueError("--usb-trust-wait must be between 0 and 60 seconds")
    targets = configured_targets(paths)
    if springboard_region_probe is not None:
        springboard_region_probe = springboard_region_probe.resolve(strict=True)
        if not springboard_region_probe.is_file():
            raise ValueError("the SpringBoard region probe must be a regular host file")

    dsn = os.getenv("AUTOPHONE_DATABASE_URL", "").strip()
    if not dsn and not ephemeral_db:
        if len(targets) == 1:
            _run_local_single_stack(
                targets[0],
                dashboard_port,
                allow_multi_vm=allow_multi_vm,
                springboard_region_probe=springboard_region_probe,
                decline_usb_trust=decline_usb_trust,
                usb_trust_wait=usb_trust_wait,
            )
            return
        raise ValueError(
            "multi-Rig stack requires AUTOPHONE_DATABASE_URL "
            "(or pass --ephemeral-db to start a temporary local PostgreSQL instance)"
        )

    from autophone.control._psycopg import require_psycopg

    require_psycopg()

    _require_free_ports(control_port, dashboard_port)
    control = f"http://127.0.0.1:{control_port}"
    dashboard = f"http://127.0.0.1:{dashboard_port}"
    lifecycles = [RigLifecycle(target) for target in targets]
    if any(lifecycle.status().affected_pids for lifecycle in lifecycles):
        raise ValueError("rig up requires stopped VMs; use the manual lifecycle for existing processes")
    with ExitStack() as stack, httpx2.Client(timeout=2, trust_env=False) as client:
        if not dsn and ephemeral_db:
            dsn = _start_ephemeral_postgres(stack)
        env = {
            **os.environ,
            "AUTOPHONE_DATABASE_URL": dsn,
            "AUTOPHONE_CONTROL_HOST": "127.0.0.1",
            "AUTOPHONE_CONTROL_PORT": str(control_port),
            "AUTOPHONE_CONTROL_HTTP": control,
            "AUTOPHONE_CONTROL_WS": f"ws://127.0.0.1:{control_port}/api/control/agent/ws",
        }
        for key in ("AUTOPHONE_AUTH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        # The supervisor binds its own dashboard to loopback. Keep operator-supplied
        # dashboard authentication in that process; use it for readiness probes.
        dashboard_env = {**env}
        if auth := os.getenv("AUTOPHONE_AUTH"):
            dashboard_env["AUTOPHONE_AUTH"] = auth
        auth_pair = auth.split(",", 1)[0].partition(":") if auth else None
        dashboard_auth = (auth_pair[0].strip(), auth_pair[2].strip()) if auth_pair else None

        processes = {}

        def start(label, *module_args, process_env=env):
            process = subprocess.Popen([sys.executable, "-m", *module_args], env=process_env,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            processes[label] = process
            stack.callback(_stop_child, process)
            return process

        def get(url, *, auth=None):
            try:
                response = client.get(url, auth=auth)
                return response.json() if response.is_success else {}
            except (httpx2.RequestError, ValueError):
                return {}

        cp = start("Control Plane", "autophone.control.http")
        _wait_until(processes, lambda: get(control + "/health").get("ok") is True, "Control Plane")
        for index, lifecycle in enumerate(lifecycles):
            lifecycle.start(allow_multi_vm=allow_multi_vm or index > 0, timeout_s=300, headless=True)
            stack.callback(lifecycle.stop)
            if cp.poll() is not None:
                raise RuntimeError("Control Plane exited during VM startup")
        for target, path in zip(targets, paths):
            agent_env = env
            if springboard_region_probe is not None:
                agent_env = {**env, **stage_region_probe(stack, target, springboard_region_probe)}
            start(f"Rig Agent {target.rig_id}", "autophone.rig.main", "--target", str(path.resolve()),
                  "--capability", "vm_uds", "--capability", "app_install", process_env=agent_env)
        start("dashboard", "autophone.cli", "serve", "--host", "127.0.0.1", "--port", str(dashboard_port),
              process_env=dashboard_env)
        expected = {target.rig_id for target in targets}

        def rigs_online():
            status = get(dashboard + "/api/platform/targets", auth=dashboard_auth)
            return status.get("mode") == "control" and expected <= {
                row["rig_id"] for row in status.get("targets", []) if row["status"] == "online"
            }

        _wait_until(processes, rigs_online, "dashboard and all selected Rigs")
        for target in targets:
            if _should_decline_usb_trust(target, decline_usb_trust):
                _run_usb_trust_check(
                    target,
                    wait_s=usb_trust_wait,
                    control_http=control,
                )
        print(f"Dashboard: {dashboard}", flush=True)
        core_services = {label: processes[label] for label in ("Control Plane", "dashboard")}
        agents = {label: process for label, process in processes.items() if label not in core_services}
        while True:
            _wait_until(core_services, lambda: True, "running services")
            for label, process in list(agents.items()):
                if process.poll() is not None:
                    print(f"{label} exited with status {process.returncode}; that Rig is offline", file=sys.stderr, flush=True)
                    del agents[label]
            time.sleep(0.5)


def cmd_init(args: argparse.Namespace) -> int:
    try:
        target, out_path = init_target(
            args.rig_id,
            bundle=args.bundle,
            vm_dir=args.vm_dir,
            out=args.out,
            host_id=args.host_id,
            udid=args.udid,
            slot_index=args.slot_index,
            ssh_port=args.ssh_port,
            frida_port=args.frida_port,
            frida_guest_port=args.frida_guest_port,
            rig_generation=args.rig_generation,
        )
    except (OSError, ValueError, TargetConfigurationError, RigLifecycleError) as exc:
        print(f"rig init: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "target_path": str(out_path),
        "rig_id": target.rig_id,
        "vm_dir": str(target.vm_dir),
        "socket": str(target.socket),
        "expected_usb_udid": target.expected_usb_udid,
        "ssh_port": target.ssh_port,
        "frida_port": target.frida_port,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    def terminate(_signal, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        run_stack(
            args.target,
            args.control_port,
            args.dashboard_port,
            allow_multi_vm=args.allow_multi_vm,
            springboard_region_probe=args.springboard_region_probe,
            ephemeral_db=args.ephemeral_db,
            decline_usb_trust=args.decline_usb_trust,
            usb_trust_wait=args.usb_trust_wait,
        )
    except KeyboardInterrupt:
        return 0
    except (ImportError, OSError, ValueError, RuntimeError, RigLifecycleError, TargetConfigurationError) as exc:
        print(f"rig up: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    rig = sub.add_parser("rig", help="initialize and bring up local upstream Rigs")
    commands = rig.add_subparsers(dest="action", required=True)

    init = commands.add_parser(
        "init",
        help="generate a validated upstream_v1 DeviceTarget JSON for one VM",
    )
    init.add_argument("rig_id", help="Rig identifier (typically the VM directory name under VPHONE_LIBRARY_ROOT)")
    init.add_argument("--bundle", type=Path, required=True, help="path to VPhone.bundle")
    init.add_argument("--vm-dir", type=Path, default=None, help="override VM directory (default: $VPHONE_LIBRARY_ROOT/<rig_id>)")
    init.add_argument("--out", type=Path, default=None, help="output target.json path (default: <vm_dir>/autophone/target.json)")
    init.add_argument("--host-id", default=None, help="host identifier (default: $AUTOPHONE_HOST_ID or local hostname)")
    init.add_argument("--udid", default=None, help="override expected USB UDID (default: read from <vm_dir>/udid-prediction.txt)")
    init.add_argument("--slot-index", type=int, default=None, help="explicit port offset N (ssh=2222+N, frida=27042+N)")
    init.add_argument("--ssh-port", type=int, default=None, help="override host SSH tunnel port")
    init.add_argument("--frida-port", type=int, default=None, help="override host Frida tunnel port")
    init.add_argument("--frida-guest-port", type=int, default=DEFAULT_FRIDA_PORT_BASE, help="guest Frida port (default: 27042)")
    init.add_argument("--rig-generation", type=int, default=1, help="Rig generation number (default: 1)")
    init.set_defaults(func=cmd_init)

    up = commands.add_parser("up", help="start VMs, Control Plane (or local single-VM mode), Rig Agents and the dashboard")
    up.add_argument("--target", type=Path, action="append", required=True)
    up.add_argument("--allow-multi-vm", action="store_true",
                    help="allow the selected VMs to start alongside other running VMs")
    up.add_argument("--ephemeral-db", action="store_true",
                    help="start a disposable local Unix-socket PostgreSQL instance when AUTOPHONE_DATABASE_URL is unset")
    up.add_argument("--control-port", type=int, default=8100)
    up.add_argument("--dashboard-port", type=int, default=8000)
    up.add_argument("--springboard-region-probe", type=Path,
                    help="stage a trusted iOS SpringBoard region probe on each upstream Rig")
    up.add_argument(
        "--decline-usb-trust",
        action="store_true",
        help="check and decline iOS USB trust prompts via the managed console once per guest boot "
             "(also enabled per Rig by <runtime_dir>/decline-usb-trust)",
    )
    up.add_argument(
        "--usb-trust-wait",
        type=float,
        default=25.0,
        help="seconds to watch for an iOS USB trust prompt when USB trust dismissal is enabled (0-60)",
    )
    up.set_defaults(func=cmd_up)
