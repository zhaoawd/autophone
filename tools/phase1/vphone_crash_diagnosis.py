"""Classify USB evidence captured when a vphone VM goes down.

The macOS unified log prints the usbmuxd destination port in host byte order.
For example, ``32498 (f2, 7e)`` is the little-endian representation of iOS
lockdownd's port 62078 (``0xf27e``).  Keeping that conversion and the exact
``IOUSBHostCIExceptionType*`` variant structured prevents a collector summary
from silently collapsing distinct VM failure signatures.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Iterable

from autophone.usb_diagnosis import (
    LOCKDOWN_PORT,
    UsbException,
    UsbIncidentDiagnosis,
    UsbmuxConnect,
    byte_swap_u16,
    diagnose_usb_incident,
)


def _run_text(args: list[str], cwd: Path | None = None) -> str | None:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    result = completed.stdout.strip()
    return result or None


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_environment(*, vphone_cli_dir: Path | None, vm_dir: Path | None) -> dict[str, object]:
    """Capture the host/VM dimensions required for cross-machine comparison."""
    macos = {
        "product_name": _run_text(["sw_vers", "-productName"]),
        "product_version": _run_text(["sw_vers", "-productVersion"]),
        "build_version": _run_text(["sw_vers", "-buildVersion"]),
    }
    result: dict[str, object] = {
        "host": platform.node() or None,
        "macos": macos,
        "vphone_cli": {
            "path": str(vphone_cli_dir) if vphone_cli_dir else None,
            "commit": _run_text(["git", "rev-parse", "HEAD"], cwd=vphone_cli_dir)
            if vphone_cli_dir
            else None,
            "dirty": bool(_run_text(["git", "status", "--porcelain"], cwd=vphone_cli_dir))
            if vphone_cli_dir
            else None,
        },
    }

    config = vm_dir / "config.plist" if vm_dir else None
    firmware_dirs = (
        sorted(path.name for path in vm_dir.glob("*_Restore") if path.is_dir())
        if vm_dir and vm_dir.is_dir()
        else []
    )
    result["vm"] = {
        "path": str(vm_dir) if vm_dir else None,
        "config_sha256": _sha256(config) if config else None,
        "firmware_directories": firmware_dirs,
    }
    return result


def _event_time(event: UsbException | UsbmuxConnect) -> str:
    return event.timestamp or "9999-99-99 99:99:99.999"


def render_timeline(diagnosis: UsbIncidentDiagnosis) -> str:
    timeline: list[tuple[str, str]] = []
    for event in diagnosis.exceptions:
        timeline.append(
            (
                _event_time(event),
                f"{event.timestamp or '?'} USB_EXCEPTION type={event.kind} :: {event.line}",
            )
        )
    for event in diagnosis.usbmux_connects:
        target = "lockdownd" if event.is_lockdownd else "other"
        timeline.append(
            (
                _event_time(event),
                f"{event.timestamp or '?'} USBMUX_CONNECT raw_port={event.raw_port} "
                f"target_port={event.target_port} target={target} :: {event.line}",
            )
        )
    return "\n".join(line for _, line in sorted(timeline)) + ("\n" if timeline else "")


def render_summary(diagnosis: UsbIncidentDiagnosis, environment: dict[str, object]) -> str:
    counts = diagnosis.exception_counts
    count_text = " ".join(
        f"{kind}={counts.get(kind, 0)}"
        for kind in ("CommandTimeout", "Terminated")
    )
    other_count = sum(
        count for kind, count in counts.items() if kind not in {"CommandTimeout", "Terminated"}
    )
    count_text = f"{count_text} Other={other_count}"

    first = diagnosis.first_exception
    first_text = (
        f"{first.timestamp or '?'} {first.kind}"
        if first
        else "(采集窗口内无带类型的 IOUSBHostCIException)"
    )
    macos = environment["macos"]
    assert isinstance(macos, dict)
    vphone_cli = environment["vphone_cli"]
    assert isinstance(vphone_cli, dict)
    vm = environment["vm"]
    assert isinstance(vm, dict)
    config_sha = vm.get("config_sha256")
    config_sha_short = config_sha[:12] if isinstance(config_sha, str) else "-"
    firmware_dirs = vm.get("firmware_directories")
    firmware_text = ", ".join(firmware_dirs) if isinstance(firmware_dirs, list) and firmware_dirs else "-"

    return "\n".join(
        [
            f"USB_EXCEPTION_TOTAL: {len(diagnosis.exceptions)}",
            "USB_EXCEPTION_COUNTS: " + count_text,
            "首个 USB 异常：" + first_text,
            "62078 候选 ConnectByPort："
            f"{diagnosis.lockdownd_connect_count}（usbmux 原始端口 32498 / bytes f2,7e 经字节序还原为 62078）",
            "关联时间线：usb_timeline.log（仅实际 ConnectByPort；不把 Bonjour PortNumber 当作连接）",
            "主机："
            f"{macos.get('product_name') or '-'} {macos.get('product_version') or '-'} "
            f"({macos.get('build_version') or '-'}) host={environment.get('host') or '-'}",
            "vphone-cli："
            f"commit={vphone_cli.get('commit') or '-'} dirty={vphone_cli.get('dirty')}",
            f"VM：firmware={firmware_text} config_sha256={config_sha_short}",
        ]
    ) + "\n"


def _diagnosis_dict(diagnosis: UsbIncidentDiagnosis, environment: dict[str, object]) -> dict[str, object]:
    return {
        "environment": environment,
        "exception_counts": diagnosis.exception_counts,
        "exceptions": [asdict(event) for event in diagnosis.exceptions],
        "first_exception": asdict(diagnosis.first_exception) if diagnosis.first_exception else None,
        "usbmux_connects": [asdict(event) for event in diagnosis.usbmux_connects],
        "lockdownd_connect_count": diagnosis.lockdownd_connect_count,
        "lockdownd_port": LOCKDOWN_PORT,
        "raw_usbmux_port_for_lockdownd": byte_swap_u16(LOCKDOWN_PORT),
    }


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-fatal", type=Path, required=True)
    parser.add_argument("--usbmux", type=Path, required=True)
    parser.add_argument("--vphone-cli-dir", type=Path)
    parser.add_argument("--vm-dir", type=Path)
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)

    diagnosis = diagnose_usb_incident(
        vm_fatal_text=_read_text(args.vm_fatal),
        usbmux_text=_read_text(args.usbmux),
    )
    environment = collect_environment(
        vphone_cli_dir=args.vphone_cli_dir,
        vm_dir=args.vm_dir,
    )

    if args.json_path:
        args.json_path.write_text(
            json.dumps(_diagnosis_dict(diagnosis, environment), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.timeline:
        args.timeline.write_text(render_timeline(diagnosis), encoding="utf-8")
    summary = render_summary(diagnosis, environment)
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    if not any((args.json_path, args.timeline, args.summary)):
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
