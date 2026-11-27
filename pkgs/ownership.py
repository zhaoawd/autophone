"""Source ownership and self-contained Hatch builds for the core distributions."""
from __future__ import annotations

from pathlib import Path

COMPONENTS = ("kernel", "rig", "domain", "console", "delivery")
DEPENDENCIES = {
    "kernel": (),
    "rig": ("kernel",),
    "domain": ("kernel", "rig"),
    "delivery": ("kernel", "rig", "domain", "console"),
    "console": ("kernel", "rig", "domain"),
}


def owner(path: str) -> str | None:
    if path == "autophone/device/app_uninstall_helper.py":
        return "delivery"
    dashboard = "autophone/adapters/http/static/dashboard"
    if path == "autophone/adapters/http/static/README.md" or (
        path.startswith(dashboard) and path != f"{dashboard}/device-console.js"
    ):
        return "delivery"
    if path in {
        "autophone/adapters/delivery.py",
        "autophone/adapters/http/requirements.py",
        "autophone/adapters/http/stores.py",
    } or path.startswith(("autophone/workflow/", "autophone/application/delivery_")):
        return "delivery"
    if path.startswith("autophone/"):
        if path in {"autophone/cli.py", "autophone/application/release_manager.py"} or path.startswith("autophone/adapters/"):
            return "console"
        if path in {"autophone/application/__init__.py"}:
            return "kernel"
        if path in {
            "autophone/host_anomaly.py", "autophone/vm_fatal.py", "autophone/usb_diagnosis.py",
            "autophone/application/device_session.py",
            "autophone/application/control_plane.py",
            "autophone/recovery_observation.py",
        }:
            return "rig"
        if path == "autophone/rig/maintenance.py":
            return "domain"
        if path.startswith(("autophone/rig/", "autophone/control/", "autophone/device/")):
            return "rig"
        if path.startswith(("autophone/_data/", "autophone/assets/")) or path.count("/") == 1:
            return "kernel"
        return "domain"
    if path == "tools/__init__.py" or path.startswith("tools/phase1/"):
        return "rig"
    if path.startswith("tools/"):
        return "console"
    if path.startswith("migrations/"):
        return "rig"
    if path.startswith("scripts/") and not (
        path.startswith("scripts/lib/") or path in {
            "scripts/collect_vphone_crash.sh", "scripts/on_vphone_down.sh",
            "scripts/start_vphone.sh", "scripts/prune_runs.sh", "scripts/trace_usbmux_xcode.sh",
        }
    ):
        return None
    if path.startswith(("config/", "scripts/")):
        return "console"
    return None


def source_files(root: Path):
    """Enumerate source roots; the build hook applies VCS exclusions before inclusion."""
    for directory in ("autophone", "tools", "migrations", "config", "scripts"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not any(x in {"__pycache__", "node_modules", ".venv"} for x in path.parts):
                yield path
