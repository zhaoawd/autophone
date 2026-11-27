"""Storage adapters for immutable package artifacts.

The package registry owns Build identity and metadata.  ArtifactStore owns only
the byte locator.  Build identity deliberately excludes this locator, so a
verified copy can move between stores without changing historical references.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from autophone.paths import APP_ASSET_ROOT, CODE_ROOT, STATE_ROOT
from autophone.assets.apps import load_app_profile


class ArtifactStoreError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactStore:
    store_id: str
    root: Path

    def path(self, key: str) -> Path:
        base = self.root.resolve(strict=False)
        target = (base / str(key)).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ArtifactStoreError(
                f"artifact key escapes store {self.store_id!r}: {key!r}"
            ) from exc
        return target


def central_store(app: str, *, state_root: Path) -> ArtifactStore:
    return ArtifactStore("central", Path(state_root) / "packages" / app)


def configured_store(
    app: str,
    *,
    state_root: Path = STATE_ROOT,
    code_root: Path | None = None,
) -> ArtifactStore | None:
    profile_root = code_root or (APP_ASSET_ROOT if state_root == STATE_ROOT else state_root)
    try:
        profile = load_app_profile(app, root=profile_root)
    except FileNotFoundError:
        return None
    config = profile.artifact_store
    if not config:
        return None
    if config.get("kind") != "project_local":
        raise ArtifactStoreError(
            f"unsupported artifact store kind {config.get('kind')!r} for app {app!r}"
        )
    project_root: Path | None = None
    # 相对的产品项目路径按源码 checkout 解析：App Profile 从 App 资产根读出时
    # （AUTOPHONE_APP_ASSET_DIR 开启），产品项目的位置不随资产副本移动。
    project_base = CODE_ROOT if profile_root == APP_ASSET_ROOT else profile_root
    env_name = str(config.get("project_root_env") or "").strip()
    if env_name and os.environ.get(env_name):
        configured_root = Path(os.environ[env_name]).expanduser()
        project_root = (
            configured_root if configured_root.is_absolute()
            else project_base / configured_root
        )
    elif config.get("project_root"):
        configured_root = Path(str(config["project_root"])).expanduser()
        project_root = (
            configured_root if configured_root.is_absolute()
            else project_base / configured_root
        )
    if project_root is None:
        raise ArtifactStoreError(
            f"artifact store for app {app!r} has no usable project root"
        )
    project_root = project_root.resolve(strict=False)
    relative_root = Path(str(config.get("relative_root") or ".autophone/artifacts"))
    artifact_root = (project_root / relative_root).resolve(strict=False)
    try:
        artifact_root.relative_to(project_root)
    except ValueError as exc:
        raise ArtifactStoreError(
            f"artifact relative_root escapes project for app {app!r}: {relative_root}"
        ) from exc
    return ArtifactStore(str(config["store_id"]), artifact_root)


def resolve_artifact_store(
    app: str,
    store_id: str | None = None,
    *,
    state_root: Path = STATE_ROOT,
    code_root: Path | None = None,
) -> ArtifactStore:
    central = central_store(app, state_root=state_root)
    configured = configured_store(app, state_root=state_root, code_root=code_root)
    if store_id is None:
        return configured or central
    if store_id == central.store_id:
        return central
    if configured is not None and store_id == configured.store_id:
        return configured
    raise ArtifactStoreError(f"unknown artifact store {store_id!r} for app {app!r}")
