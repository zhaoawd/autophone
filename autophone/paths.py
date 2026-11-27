"""Resolve immutable code and mutable deployment roots."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


CODE_ROOT = Path(__file__).resolve().parents[1]


def _resolve_root(
    variable: str,
    environ: Mapping[str, str] | None = None,
    *,
    default: Path = CODE_ROOT,
) -> Path:
    env = os.environ if environ is None else environ
    configured = str(env.get(variable) or "").strip()
    if not configured:
        return default
    return Path(configured).expanduser().resolve()


def resolve_asset_root(environ: Mapping[str, str] | None = None) -> Path:
    return _resolve_root("AUTOPHONE_ASSET_DIR", environ)


def resolve_state_root(environ: Mapping[str, str] | None = None) -> Path:
    return _resolve_root("AUTOPHONE_STATE_DIR", environ)


def resolve_artifact_root(environ: Mapping[str, str] | None = None) -> Path:
    return _resolve_root("AUTOPHONE_ARTIFACT_DIR", environ)


def resolve_package_root(environ: Mapping[str, str] | None = None) -> Path:
    return _resolve_root("AUTOPHONE_PACKAGE_DIR", environ)


#: App 资产根开关。指向一个包含 `apps/<app>/` 的目录；不设置时等于 `CODE_ROOT`。
APP_ASSET_ENV = "AUTOPHONE_APP_ASSET_DIR"


def resolve_app_asset_root(environ: Mapping[str, str] | None = None) -> Path:
    """App 资产（`apps/<app>/` 下的 Profile、用例库、Recipe、操作库等）所在的根。

    与 `AUTOPHONE_ASSET_DIR`（部署资产根，ExpectedSnapshot 查找用）是两个变量：
    后者已有部署方在用，改变它的含义会让既有部署的 App 资产根跟着移动。
    """
    return _resolve_root(APP_ASSET_ENV, environ)


ASSET_ROOT = resolve_asset_root()
STATE_ROOT = resolve_state_root()
ARTIFACT_ROOT = resolve_artifact_root()
PACKAGE_ROOT = resolve_package_root()
APP_ASSET_ROOT = resolve_app_asset_root()


class AppAssetRootError(RuntimeError):
    """开关开启时，某个入口仍按 `CODE_ROOT` 解析 App 资产，被拒绝。"""

    code = "app_asset_root_override"

    def __init__(self, operation: str, *, path: str | Path | None = None) -> None:
        self.operation = str(operation or "resolve app assets")
        self.path = Path(path) if path is not None else None
        target = f": {self.path}" if self.path is not None else ""
        super().__init__(
            f"{self.code}: {self.operation} resolves App assets under CODE_ROOT "
            f"while {APP_ASSET_ENV}={APP_ASSET_ROOT}{target}"
        )

    def as_detail(self) -> dict[str, str]:
        detail = {
            "code": self.code,
            "message": str(self),
            "operation": self.operation,
            "app_asset_root": str(APP_ASSET_ROOT),
        }
        if self.path is not None:
            detail["path"] = str(self.path)
        return detail


def app_asset_root_overridden() -> bool:
    """本进程的 App 资产根是否被 `AUTOPHONE_APP_ASSET_DIR` 移出 `CODE_ROOT`。"""
    return APP_ASSET_ROOT != CODE_ROOT


def refuse_code_root_app_assets(
    root: str | Path, operation: str, *, path: str | Path | None = None,
) -> None:
    """开关开启时拒绝以 `CODE_ROOT` 为根读写 App 资产。

    覆盖不到的入口（默认根仍是 `CODE_ROOT`，或调用方显式传了 `CODE_ROOT`）在这里
    fail-closed，而不是静默落回仓库里的 `apps/`。开关关闭时什么也不做。
    """
    if APP_ASSET_ROOT == CODE_ROOT:
        return
    if Path(root).resolve() == CODE_ROOT.resolve():
        raise AppAssetRootError(operation, path=path)


def refuse_app_asset_override(operation: str, *, path: str | Path | None = None) -> None:
    """开关开启时拒绝一个不随开关移动的 App 资产入口。"""
    if APP_ASSET_ROOT != CODE_ROOT:
        raise AppAssetRootError(operation, path=path)


def describe_app_asset_root() -> str:
    """启动日志用的一行：App 资产根及其来源。"""
    if APP_ASSET_ROOT == CODE_ROOT:
        return f"app asset root: {APP_ASSET_ROOT} (default: CODE_ROOT)"
    return f"app asset root: {APP_ASSET_ROOT} (from {APP_ASSET_ENV}; CODE_ROOT={CODE_ROOT})"


def code_path(*parts: str) -> Path:
    return CODE_ROOT.joinpath(*parts)


def state_path(*parts: str) -> Path:
    return STATE_ROOT.joinpath(*parts)


def asset_path(*parts: str) -> Path:
    return ASSET_ROOT.joinpath(*parts)


def artifact_path(*parts: str) -> Path:
    return ARTIFACT_ROOT.joinpath(*parts)


def package_path(*parts: str) -> Path:
    return PACKAGE_ROOT.joinpath(*parts)


def app_asset_path(*parts: str) -> Path:
    return APP_ASSET_ROOT.joinpath(*parts)


def app_state_path(app: str, *parts: str) -> Path:
    return state_path("apps", app, *parts)


def code_root_subprocess_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """子进程环境：把 CODE_ROOT 显式放进 `PYTHONPATH`。

    `apps/<app>/delivery_state_driver.py` 与 `delivery_execution_driver.py` 以
    `import autophone` 取公共运行时，但它们是被当脚本拉起的子进程：Python 把
    **脚本所在目录**（`apps/<app>/`）放进 `sys.path[0]`，不是调用方的工作目录；本仓库
    的 `autophone` 发行版也不往 site-packages 铺目录。因此调用方进程能 import 不代表
    子进程能，靠 rig agent 的工作目录或调用方导出的 `PYTHONPATH` 都不成立
    （`scripts/start_vphone.sh` 重启 agent 时不会带上手工设的那一份）。

    原有 `PYTHONPATH` 保留在后面：调用方声明的搜索路径仍然有效，只是排在仓库根之后。
    """
    env = dict(os.environ if environ is None else environ)
    root = CODE_ROOT.as_posix()
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    env["PYTHONPATH"] = os.pathsep.join([root, *[item for item in existing if item != root]])
    return env


__all__ = [
    "APP_ASSET_ENV",
    "APP_ASSET_ROOT",
    "ARTIFACT_ROOT",
    "ASSET_ROOT",
    "AppAssetRootError",
    "CODE_ROOT",
    "PACKAGE_ROOT",
    "STATE_ROOT",
    "app_asset_path",
    "app_asset_root_overridden",
    "app_state_path",
    "artifact_path",
    "asset_path",
    "code_path",
    "code_root_subprocess_env",
    "describe_app_asset_root",
    "package_path",
    "refuse_app_asset_override",
    "refuse_code_root_app_assets",
    "resolve_app_asset_root",
    "resolve_artifact_root",
    "resolve_asset_root",
    "resolve_package_root",
    "resolve_state_root",
    "state_path",
]
