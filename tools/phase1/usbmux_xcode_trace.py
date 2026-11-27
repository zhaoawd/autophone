#!/usr/bin/env python3
"""Low-overhead fallback recorder for short-lived host Xcode processes.

The preferred source is DTrace's ``proc:::exec-success``.  Some hosts deny it
under SIP, and Endpoint Security's ``eslogger`` can separately require Full
Disk Access.  This module is the deliberately explicit fallback: it samples
the process table, writes JSONL only when a matching PID first appears, and
captures the parent chain from that same snapshot.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Iterable


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    started_at: str
    command: str


def parse_ps_snapshot(text: str) -> dict[int, ProcessRow]:
    """Parse ``ps -ww -axo pid=,ppid=,lstart=,command=`` output."""
    rows: dict[int, ProcessRow] = {}
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) != 8:
            continue
        pid_text, ppid_text, weekday, month, day, clock, year, command = parts
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
        except ValueError:
            continue
        rows[pid] = ProcessRow(
            pid=pid,
            ppid=ppid,
            started_at=" ".join((weekday, month, day, clock, year)),
            command=command,
        )
    return rows


def _argv0(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return argv[0] if argv else ""


def matches_target(row: ProcessRow, target: str) -> bool:
    """Match only the executable basename, never its bundle path or arguments."""
    argv0 = _argv0(row.command)
    return Path(argv0).name == target


def parent_chain(rows: dict[int, ProcessRow], ppid: int, *, max_depth: int = 8) -> list[ProcessRow]:
    chain: list[ProcessRow] = []
    seen: set[int] = set()
    while ppid > 0 and ppid not in seen and len(chain) < max_depth:
        seen.add(ppid)
        parent = rows.get(ppid)
        if parent is None:
            break
        chain.append(parent)
        ppid = parent.ppid
    return chain


def _snapshot(ps_bin: str) -> dict[int, ProcessRow]:
    result = subprocess.run(
        [ps_bin, "-ww", "-axo", "pid=,ppid=,lstart=,command="],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return parse_ps_snapshot(result.stdout)


def poll_forever(*, target: str, interval: float, ps_bin: str) -> None:
    """Emit one JSON object per observed matching process incarnation."""
    seen: set[tuple[int, str, str]] = set()
    while True:
        try:
            rows = _snapshot(ps_bin)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(
                json.dumps(
                    {
                        "event": "snapshot_error",
                        "observed_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(interval)
            continue

        current = {(row.pid, row.started_at, row.command) for row in rows.values()}
        for row in rows.values():
            identity = (row.pid, row.started_at, row.command)
            if identity in seen or not matches_target(row, target):
                continue
            print(
                json.dumps(
                    {
                        "event": "ps_sample",
                        "observed_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "pid": row.pid,
                        "ppid": row.ppid,
                        "started_at": row.started_at,
                        "command": row.command,
                        "parent_chain": [asdict(parent) for parent in parent_chain(rows, row.ppid)],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        seen.intersection_update(current)
        seen.update(current)
        time.sleep(interval)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    poll = subcommands.add_parser("poll", help="record newly observed target processes as JSONL")
    poll.add_argument("--target", default="Xcode")
    poll.add_argument("--interval", type=float, default=0.2)
    poll.add_argument("--ps-bin", default="/bin/ps")
    args = parser.parse_args(argv)

    if args.command == "poll":
        if args.interval <= 0:
            parser.error("--interval must be positive")
        poll_forever(target=args.target, interval=args.interval, ps_bin=args.ps_bin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
