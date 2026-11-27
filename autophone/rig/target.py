"""Explicit identity and local endpoints for one VM-backed Rig."""
from __future__ import annotations

import json
import os
from tempfile import NamedTemporaryFile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from autophone.logging import get_logger


log = get_logger(__name__)


class TargetConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceTarget:
    host_id: str
    rig_id: str
    rig_generation: int
    cli_dir: Path
    vm_dir: Path
    socket: Path
    expected_usb_udid: str
    ssh_host: str
    ssh_port: int
    frida_host: str
    frida_port: int
    frida_guest_port: int
    log_dir: Path
    temp_dir: Path
    package_cache: Path
    # 运行时目录（health.json / health.pid / health.log 的落点）。省略时仍派生成
    # `temp_dir/"runtime"`，与本字段引入之前的文档完全等价；显式给出是为了让
    # `scripts/start_vphone.sh` 能把它对准健康守护实际写入的
    # `AUTOPHONE_RIG_RUNTIME_DIR`——两者不一致时 agent 子进程读到的
    # VPHONE_HEALTH_STATE_FILE 是一个没人写的路径，健康读数恒 missing。
    runtime_dir: Path | None = None
    protocol: str = "legacy"
    core_bundle: Path | None = None

    def __post_init__(self) -> None:
        if self.protocol not in {"legacy", "upstream_v1"}:
            raise TargetConfigurationError("unsupported vphone protocol")
        if self.protocol == "upstream_v1" and (self.core_bundle is None or not self.core_bundle.is_absolute()):
            raise TargetConfigurationError("upstream_v1 requires absolute core_bundle")
        if self.runtime_dir is None:
            object.__setattr__(self, "runtime_dir", self.temp_dir / "runtime")
        if (
            not isinstance(self.host_id, str)
            or not isinstance(self.rig_id, str)
            or not self.host_id.strip()
            or not self.rig_id.strip()
        ):
            raise TargetConfigurationError("host_id and rig_id are required")
        if not isinstance(self.expected_usb_udid, str) or not self.expected_usb_udid.strip():
            raise TargetConfigurationError("expected_usb_udid is required")
        if self.rig_generation <= 0:
            raise TargetConfigurationError("rig_generation must be positive")
        for name in (
            "cli_dir", "vm_dir", "socket", "log_dir", "temp_dir", "package_cache", "runtime_dir",
        ):
            value = getattr(self, name)
            if not value.is_absolute():
                raise TargetConfigurationError(f"{name} must be an absolute path: {value}")
        for name in ("ssh_port", "frida_port", "frida_guest_port"):
            value = int(getattr(self, name))
            if value < 1 or value > 65535:
                raise TargetConfigurationError(f"{name} must be in 1..65535")
        if self.ssh_host == self.frida_host and self.ssh_port == self.frida_port:
            raise TargetConfigurationError("SSH and Frida endpoints must not share one port")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DeviceTarget":
        path_fields = {
            "cli_dir", "vm_dir", "socket", "log_dir", "temp_dir", "package_cache", "runtime_dir", "core_bundle",
        }
        values = dict(raw)
        for field in path_fields:
            if values.get(field) is not None and field in values:
                values[field] = Path(str(values[field])).expanduser()
        if values.get("protocol") == "upstream_v1" and values.get("cli_dir") is None:
            bundle = values.get("core_bundle")
            if isinstance(bundle, Path):
                values["cli_dir"] = bundle / "Contents" / "MacOS"
        try:
            values["rig_generation"] = int(values["rig_generation"])
            values["ssh_port"] = int(values["ssh_port"])
            values["frida_port"] = int(values["frida_port"])
            values["frida_guest_port"] = int(values["frida_guest_port"])
            return cls(**values)
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetConfigurationError(f"invalid DeviceTarget: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> "DeviceTarget":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TargetConfigurationError(f"cannot load DeviceTarget {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise TargetConfigurationError("DeviceTarget document must be an object")
        return cls.from_mapping(raw)

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.host_id, self.rig_id, self.rig_generation

    @property
    def frida_endpoint(self) -> str:
        return f"{self.frida_host}:{self.frida_port}"

    @property
    def config_path(self) -> Path:
        return self.vm_dir / "config.plist"

    @property
    def health_state_file(self) -> Path:
        return self.runtime_dir / "health.json"

    @property
    def health_pid_file(self) -> Path:
        return self.runtime_dir / "health.pid"

    @property
    def health_log_file(self) -> Path:
        return self.runtime_dir / "health.log"

    def public_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        for key, value in tuple(raw.items()):
            if isinstance(value, Path):
                raw[key] = str(value)
        return raw

    @property
    def runtime_target_file(self) -> Path:
        return self.runtime_dir / f"target-{self.rig_generation}.json"

    def ensure_local_directories(self) -> None:
        for path in (self.log_dir, self.temp_dir, self.package_cache, self.runtime_dir):
            path.mkdir(parents=True, exist_ok=True)
        if self.protocol == "upstream_v1":
            content = json.dumps(self.public_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            with NamedTemporaryFile(mode="w", dir=self.runtime_dir, encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                try:
                    os.link(stream.name, self.runtime_target_file)
                except FileExistsError:
                    if self.runtime_target_file.read_text(encoding="utf-8") != content:
                        raise TargetConfigurationError("target generation configuration changed")

    def process_environment(self) -> dict[str, str]:
        """Compatibility environment for one target-bound child process."""
        return {
            "AUTOPHONE_VPHONE_PROTOCOL": self.protocol,
            **({"AUTOPHONE_RIG_TARGET": str(self.runtime_target_file)} if self.protocol == "upstream_v1" else {}),
            **({"AUTOPHONE_CORE_BUNDLE": str(self.core_bundle)} if self.core_bundle else {}),
            "VPHONE_CLI_DIR": str(self.cli_dir),
            "VM_DIR": str(self.vm_dir),
            "VPHONE_SOCK": str(self.socket),
            "AP_DEVICE": self.rig_id,
            "AUTOPHONE_RIG_ID": self.rig_id,
            "AUTOPHONE_RIG_GENERATION": str(self.rig_generation),
            "AUTOPHONE_EXPECTED_USB_UDID": self.expected_usb_udid,
            "AUTOPHONE_RIG_RUNTIME_DIR": str(self.runtime_dir),
            "AUTOPHONE_RIG_LOG_DIR": str(self.log_dir),
            "AUTOPHONE_RIG_TEMP_DIR": str(self.temp_dir),
            "AUTOPHONE_RIG_PACKAGE_CACHE": str(self.package_cache),
            "VPHONE_LOG_DIR": str(self.log_dir),
            "VPHONE_HEALTH_STATE_FILE": str(self.health_state_file),
            "VPHONE_HEALTH_PID_FILE": str(self.health_pid_file),
            "VPHONE_HEALTH_LOG": str(self.health_log_file),
            "AP_FRIDA_HOST": self.frida_endpoint,
            "AUTOPHONE_FRIDA_HOST": self.frida_endpoint,
            "AUTOPHONE_FRIDA_HOST_PORT": str(self.frida_port),
            "AUTOPHONE_FRIDA_GUEST_PORT": str(self.frida_guest_port),
            "AUTOPHONE_SSH_HOST": self.ssh_host,
            "AUTOPHONE_SSH_PORT": str(self.ssh_port),
        }


def served_device_keys(target: Any) -> list[str]:
    """本 rig 服务的设备键：``rig_id`` 与 VM 目录名（交互通道路由与接单设备核对共用）。

    提交侧（web 进程）按它自己 `resolve_target().device` 得到的键点名设备，而 rig 侧
    `AP_DEVICE` 被绑成 `rig_id`——两者在本机默认布局下不一定同名（web 进程没设
    `AP_DEVICE` 时取 `vm_dir.name`）。两个都注册成别名，路由就不依赖这层巧合。
    与控制面 `resolve_device_rig` 的匹配规则（`rig_id` 或 VM 目录名）一致（BL-217）。
    """
    keys = {str(target.rig_id).strip()}
    try:
        from autophone.device.target import resolve_target

        keys.add(resolve_target(environ={"VM_DIR": str(target.vm_dir)}).device)
    except Exception as exc:  # noqa: BLE001 - 解析不出别名不影响 rig_id 这条
        log.warning("device session alias resolution failed: %s: %s", type(exc).__name__, exc)
    return sorted(key for key in keys if key)
