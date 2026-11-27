"""Classify how the vphone host process (vphone-cli / vphone-vm) exited.

A VM is destroyed when its host process exits, and that leaves no USB exception
or jetsam record: the 2026-09-30 incident on two VMs showed only ``client
process exited`` in the Virtualization helper log.  The unified log does record
the host side: AppKit's ``terminate:`` sequence, runningboardd's ``termination
reported by`` line, launchd's ``service inactive`` line and crash reports.

Verdicts state what the records show and nothing more:

``app_terminate``              the process went through ``-[NSApplication terminate:]``
``exit_without_app_terminate`` an exit record exists but no ``terminate:`` line;
                               a signal and a direct ``exit`` are indistinguishable here
``crash_reported``             a crash reporter line names the process
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Iterable

SCHEMA_VERSION = "vphone-host-exit.v1"
HOST_PROCESS_NAMES = ("vphone-cli", "vphone-vm")
# A UI action this close before ``terminate:`` is reported as the trigger.
UI_ACTION_WINDOW_S = 2.0

_HOST = r"vphone-(?:cli|vm)"
LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\S+\s+"
    r"(?P<process>[^\s\[]+)\[(?P<pid>\d+):[0-9a-fA-F]+\]\s+(?P<message>.*)$"
)
DIRECT_TERMINATION_RE = re.compile(
    rf"\[anon<(?P<name>{_HOST})>\(\d+\):(?P<pid>\d+)\] termination reported by (?P<reporter>.+)$"
)
HELPER_TERMINATION_RE = re.compile(
    rf"VirtualMachine\(\[anon<{_HOST}>\(\d+\):(?P<pid>\d+)\]\)\(\d+\)>.*"
    r":(?P<helper>\d+):\d+\] termination reported by (?P<reporter>.+)$"
)
LAUNCHD_INACTIVE_RE = re.compile(
    rf"service inactive: com\.apple\.xpc\.launchd\.unmanaged\.(?P<name>{_HOST})\.(?P<pid>\d+)"
)
QUITTING_RE = re.compile(r"QUITTING: pid=(?P<pid>\d+)")
CRASH_PID_RE = re.compile(rf"{_HOST}[\[. ](?P<pid>\d+)")
CRASH_REPORTERS = ("ReportCrash", "osanalyticsd")


@dataclass(frozen=True)
class HostExit:
    pid: int
    process: str | None
    exit_at: str | None
    verdict: str
    trigger: str | None
    terminate_at: str | None
    ui_action_gap_s: float | None
    reporter: str | None
    vz_helper_terminations: list[str]
    is_target: bool
    evidence: list[str]


@dataclass(frozen=True)
class HostExitDiagnosis:
    target_pid: int | None
    exits: list[HostExit]
    unattributed_crash_lines: list[str]


@dataclass
class _Record:
    process: str | None = None
    ui_actions: list[tuple[str, str]] = field(default_factory=list)
    terminate_at: str | None = None
    approved_at: str | None = None
    termination_at: str | None = None
    reporter: str | None = None
    inactive_at: str | None = None
    crash_at: str | None = None
    helpers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


def _seconds_between(earlier: str, later: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return (datetime.strptime(later, fmt) - datetime.strptime(earlier, fmt)).total_seconds()


def diagnose_host_exits(log_text: str, *, target_pid: int | None = None) -> HostExitDiagnosis:
    records: dict[int, _Record] = {}
    quitting: list[tuple[int, str]] = []
    unattributed_crash_lines: list[str] = []

    def record(pid: int) -> _Record:
        return records.setdefault(pid, _Record())

    for line in log_text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        timestamp = match.group("timestamp")
        process = match.group("process")
        message = match.group("message")

        if process in HOST_PROCESS_NAMES:
            own = record(int(match.group("pid")))
            own.process = own.process or process
            if "sendAction" in message or "trackMouse" in message:
                own.ui_actions.append((timestamp, line))
            elif message.endswith("[com.apple.AppKit:Application] terminate:"):
                if own.terminate_at is None:
                    own.terminate_at = timestamp
                    own.evidence.append(line)
            elif message.endswith("App termination approved"):
                if own.approved_at is None:
                    own.approved_at = timestamp
                    own.evidence.append(line)
            continue

        if process == "runningboardd":
            direct = DIRECT_TERMINATION_RE.search(message)
            if direct:
                found = record(int(direct.group("pid")))
                found.process = found.process or direct.group("name")
                if found.termination_at is None:
                    found.termination_at = timestamp
                    found.reporter = direct.group("reporter").strip()
                    found.evidence.append(line)
                continue
            helper = HELPER_TERMINATION_RE.search(message)
            if helper:
                found = record(int(helper.group("pid")))
                found.helpers.append(
                    f"{helper.group('helper')}: {helper.group('reporter').strip()}"
                )
            continue

        if process == "launchd":
            inactive = LAUNCHD_INACTIVE_RE.search(message)
            if inactive:
                found = record(int(inactive.group("pid")))
                found.process = found.process or inactive.group("name")
                if found.inactive_at is None:
                    found.inactive_at = timestamp
                    found.evidence.append(line)
            continue

        if process == "launchservicesd":
            quit_ = QUITTING_RE.search(message)
            if quit_:
                quitting.append((int(quit_.group("pid")), line))
            continue

        if process in CRASH_REPORTERS and any(name in message for name in HOST_PROCESS_NAMES):
            crashed = CRASH_PID_RE.search(message)
            if crashed:
                found = record(int(crashed.group("pid")))
                found.crash_at = found.crash_at or timestamp
                found.evidence.append(line)
            else:
                unattributed_crash_lines.append(line)

    # launchservicesd names only a pid, so it is evidence solely for known host pids.
    for pid, line in quitting:
        if pid in records:
            records[pid].evidence.append(line)

    exits: list[HostExit] = []
    for pid, found in records.items():
        exit_at = found.termination_at or found.inactive_at or found.crash_at or found.approved_at
        if exit_at is None:
            continue  # still running: UI activity alone is not an exit
        trigger: str | None = None
        gap: float | None = None
        evidence = list(found.evidence)
        if found.crash_at is not None:
            verdict = "crash_reported"
        elif found.terminate_at is not None:
            verdict = "app_terminate"
            trigger = "unknown"
            preceding = [
                (timestamp, line) for timestamp, line in found.ui_actions
                if 0 <= _seconds_between(timestamp, found.terminate_at) <= UI_ACTION_WINDOW_S
            ]
            if preceding:
                closest_at, closest_line = max(preceding)
                trigger = "ui_action"
                gap = round(_seconds_between(closest_at, found.terminate_at), 3)
                evidence.append(closest_line)
        else:
            verdict = "exit_without_app_terminate"
        exits.append(HostExit(
            pid=pid,
            process=found.process,
            exit_at=exit_at,
            verdict=verdict,
            trigger=trigger,
            terminate_at=found.terminate_at,
            ui_action_gap_s=gap,
            reporter=found.reporter,
            vz_helper_terminations=list(found.helpers),
            is_target=target_pid is not None and pid == target_pid,
            evidence=sorted(evidence),
        ))
    exits.sort(key=lambda item: (item.exit_at or "", item.pid))
    return HostExitDiagnosis(
        target_pid=target_pid,
        exits=exits,
        unattributed_crash_lines=unattributed_crash_lines,
    )


def _exit_line(item: HostExit) -> str:
    parts = [
        f"HOST_EXIT pid={item.pid}",
        f"process={item.process or '-'}",
        f"exit_at={item.exit_at or '-'}",
        f"verdict={item.verdict}",
    ]
    if item.trigger is not None:
        parts.append(f"trigger={item.trigger}")
    if item.ui_action_gap_s is not None:
        parts.append(f"ui_action_gap_s={item.ui_action_gap_s}")
    parts.append(f"reporter={item.reporter or '-'}")
    if item.is_target:
        parts.append("<== target")
    return " ".join(parts)


def _target_lines(diagnosis: HostExitDiagnosis) -> list[str]:
    if diagnosis.target_pid is None:
        return ["HOST_EXIT_TARGET: (未提供目标 pid)"]
    target = next((item for item in diagnosis.exits if item.is_target), None)
    if target is None:
        return [f"HOST_EXIT_TARGET: pid={diagnosis.target_pid} (采集窗口内无该进程的退出记录)"]
    head = f"HOST_EXIT_TARGET: pid={target.pid} verdict={target.verdict}"
    if target.trigger is not None:
        head += f" trigger={target.trigger}"
    if target.verdict == "app_terminate":
        detail = "目标宿主进程经 -[NSApplication terminate:] 退出（应用内退出流程）；"
        if target.trigger == "ui_action":
            detail += f"terminate: 之前 {target.ui_action_gap_s}s 有 AppKit 界面动作记录。"
        else:
            detail += (
                f"terminate: 之前 {UI_ACTION_WINDOW_S:g}s 内无 AppKit 界面动作记录，触发来源未识别。"
            )
    elif target.verdict == "crash_reported":
        detail = "目标宿主进程有崩溃上报记录，见 crash_reports_index.log。"
    else:
        detail = (
            "目标宿主进程有退出记录但无 terminate: 记录；"
            "信号终止与直接 exit 在这些记录中无法区分。"
        )
    return [head, detail]


def render_summary(diagnosis: HostExitDiagnosis) -> str:
    lines = [f"HOST_EXIT_TOTAL: {len(diagnosis.exits)}"]
    if diagnosis.exits:
        lines.extend(_exit_line(item) for item in diagnosis.exits)
    else:
        lines.append("(采集窗口内无宿主进程退出记录)")
    lines.extend(_target_lines(diagnosis))
    if diagnosis.unattributed_crash_lines:
        lines.append(
            f"未归属到 pid 的崩溃上报行：{len(diagnosis.unattributed_crash_lines)}（见 host_exit.json）"
        )
    return "\n".join(lines) + "\n"


def _diagnosis_dict(diagnosis: HostExitDiagnosis) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target_pid": diagnosis.target_pid,
        "exits": [asdict(item) for item in diagnosis.exits],
        "unattributed_crash_lines": diagnosis.unattributed_crash_lines,
    }


def _target_pid(raw: str | None) -> int | None:
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--target-pid", default="")
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)

    text = args.log.read_text(encoding="utf-8", errors="replace") if args.log.is_file() else ""
    diagnosis = diagnose_host_exits(text, target_pid=_target_pid(args.target_pid))
    if args.json_path:
        args.json_path.write_text(
            json.dumps(_diagnosis_dict(diagnosis), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    summary = render_summary(diagnosis)
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    if not any((args.json_path, args.summary)):
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
