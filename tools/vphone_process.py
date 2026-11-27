"""Identify running vphone VMs by executable and exact configuration argv."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path
import subprocess
import sys

from autophone.rig.process_guard import AgentProcessError, process_arguments


VM_NAMES = {"vphone-cli", "vphone-vm"}


def vm_processes(pids: list[int]) -> Iterator[tuple[int, Path, list[Path]]]:
    """Yield (pid, executable, --config values) for each vphone host process."""
    for pid in pids:
        arguments = process_arguments(pid)
        if not arguments or Path(arguments[0]).name not in VM_NAMES:
            continue
        configs = []
        for index, arg in enumerate(arguments[1:], 1):
            if arg == "--config" and index + 1 < len(arguments):
                configs.append(Path(arguments[index + 1]))
            elif arg.startswith("--config="):
                configs.append(Path(arg.split("=", 1)[1]))
        yield pid, Path(arguments[0]), configs


def process_state(binary: Path, config: Path, pids: list[int]) -> str:
    expected_dir, expected_config = binary.resolve().parent, config.resolve()
    other = False
    target = False
    for pid, executable, configs in vm_processes(pids):
        if not configs:
            other = True
            continue
        if len(configs) != 1 or not executable.is_absolute() or not configs[0].is_absolute():
            raise ValueError(f"cannot verify vphone config for PID {pid}")
        if executable.resolve().parent == expected_dir and configs[0].resolve() == expected_config:
            target = True
        else:
            other = True
    return "target" if target else "other" if other else "none"


def attribution_rows(target_vm_dir: Path, pids: list[int]) -> list[str]:
    """One `pid<TAB>name<TAB>vm_dir<TAB>is_target` row per vphone host process.

    For diagnostics: an unverifiable config does not raise; its vm_dir is `-` and it is
    never marked as target. vm_dir is the resolved config directory, compared against the
    resolved target_vm_dir.
    """
    expected = target_vm_dir.resolve()
    rows = []
    for pid, executable, configs in vm_processes(pids):
        vm_dir, is_target = "-", "0"
        if len(configs) == 1 and configs[0].is_absolute():
            resolved = configs[0].resolve().parent
            vm_dir, is_target = str(resolved), "1" if resolved == expected else "0"
        rows.append("\t".join((str(pid), executable.name, vm_dir, is_target)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--list", action="store_true",
                        help="list every vphone host process instead of classifying one target")
    parser.add_argument("--target-vm-dir", type=Path)
    args = parser.parse_args()
    if args.list:
        if args.target_vm_dir is None:
            parser.error("--list requires --target-vm-dir")
    elif args.binary is None or args.config is None:
        parser.error("--binary and --config are required")
    try:
        result = subprocess.run(
            ["pgrep", "-x", "vphone-cli|vphone-vm"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "pgrep failed")
        pids = [int(value) for value in result.stdout.split()]
        if args.list:
            for row in attribution_rows(args.target_vm_dir, pids):
                print(row)
        else:
            print(process_state(args.binary, args.config, pids))
    except (AgentProcessError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
