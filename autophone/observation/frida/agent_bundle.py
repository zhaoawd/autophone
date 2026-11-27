"""Phase 4 frida agent 包的按需构建。

`autophone/observation/frida/agent/_build/*.js` 是 `frida-compile` 产物，被 `.gitignore` 忽略
（不入库）。运行期构建必须落到 ``STATE_ROOT/runs/.cache``，不能污染可校验、
可回滚的 release 代码树。

这里把「缺了就构建一次」收成一个幂等入口：state bundle 存在直接返回；缺了就用
`.venv/bin/frida-compile`（frida uv group 提供）跑对应 `npm run build:<target>`，
构建前把源码同步到 state cache，若 cache ``node_modules`` 缺失先 `npm install`。
构建仍失败或产物仍不在则 fail-closed，
抛出带构建输出的清晰错误（含手动命令），绝不静默返回不存在的路径。
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from autophone.paths import CODE_ROOT, STATE_ROOT

_REPO_ROOT = CODE_ROOT
_AGENT_DIR = CODE_ROOT / "autophone" / "observation" / "frida" / "agent"
_CACHE_ROOT = STATE_ROOT / "runs" / ".cache" / "phase4-agent"

_BUILD_TARGET_BY_OUTPUT = {
    "objc_bridge_probe.js": "bridge",
    "net_discovery.js": "discovery",
    "net_tap.js": "net",
    "corelocation_discovery.js": "gps",
    "corelocation_control.js": "gps-control",
    "scanner_observe.js": "scanner-observe",
    "crash_trap.js": "crash-trap",
    "metadata_port_probe.js": "metadata-port-probe",
    "native_typography_probe.js": "native-typography-probe",
}


def agent_build_path(name: str) -> Path:
    """Return the logical code-tree path for one generated Agent bundle."""

    if not name or Path(name).name != name:
        raise ValueError(f"agent bundle name must be a basename: {name!r}")
    return _AGENT_DIR / "_build" / name


def agent_bundle_target(agent_js: str | Path) -> str:
    """Return the package-script target for one known generated bundle."""

    name = Path(agent_js).name
    try:
        return _BUILD_TARGET_BY_OUTPUT[name]
    except KeyError as exc:
        raise FileNotFoundError(
            f"agent bundle {name!r} has no registered build target"
        ) from exc


@contextmanager
def _cache_build_lock() -> Iterator[None]:
    """Serialize cache sync, dependency install, bundle output and marker writes."""

    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    path = _CACHE_ROOT / ".build.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _newest_source_mtime_ns(source: Path) -> int | None:
    """入口源 + 同目录共享模块（`src/*.mjs`）里最新的 mtime。

    入口 `.js` 只 import 同目录的 `.mjs` 共享模块（契约/注入体等）。只比入口的
    mtime 会漏：改了共享模块而没动入口时 bundle 判定为「新鲜」，设备上跑的还是
    旧逻辑——同 ax_dump.dylib 那次陈旧构建，症状是静默的行为不符而不是报错。
    """
    candidates = [source] if source.is_file() else []
    if source.parent.is_dir():
        candidates.extend(sorted(source.parent.glob("*.mjs")))
    mtimes = [path.stat().st_mtime_ns for path in candidates if path.is_file()]
    return max(mtimes) if mtimes else None


def _bundle_is_fresh(agent_js: Path, source: Path) -> bool:
    if not agent_js.exists():
        return False
    newest = _newest_source_mtime_ns(source)
    return newest is None or agent_js.stat().st_mtime_ns >= newest


def _compile_direct(
    agent_js: Path,
    *,
    source: Path | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Compile with the Python API when frida-tools' CLI lags libfrida.

    Frida 17.16 removed the ``externals`` BuildOptions member while the pinned
    frida-tools 14.10 CLI still always supplies it.  The CLI can consequently
    exit without producing a bundle.  The underlying compiler remains usable
    when that optional field is omitted, so keep the on-demand build path
    functional without weakening source bundling or type checking.
    """
    source = source or (agent_js.parent.parent / "src" / agent_js.name)
    project_root = project_root or agent_js.parent.parent
    if not source.is_file():
        return f"source missing: {source}"
    try:
        import frida

        bundles: list[str] = []
        diagnostics: list[object] = []
        compiler = frida.Compiler()
        compiler.on("output", bundles.append)
        compiler.on("diagnostics", diagnostics.extend)
        compiler.build(
            str(source.resolve()),
            project_root=str(project_root),
            output_format="unescaped",
            # Compiler 17 的 ESM ``unescaped`` 输出是以 📦 开头的多模块
            # package，不是 Session.create_script() 可直接解析的 JavaScript。
            # 所有 _build/*.js 都由 create_script 加载，必须产出自执行 IIFE。
            bundle_format="iife",
            type_check="full",
            source_maps="included",
            compression="none",
            platform="gum",
        )
        if diagnostics or not bundles:
            return f"diagnostics={diagnostics!r}; output_count={len(bundles)}"
        agent_js.parent.mkdir(parents=True, exist_ok=True)
        agent_js.write_text(bundles[-1], encoding="utf-8")
        return None
    except Exception as exc:  # noqa: BLE001 - returned as build diagnostics
        return f"{type(exc).__name__}: {exc}"


def _resolve_frida_compile(project_root: Path = _AGENT_DIR) -> str | None:
    """按优先级找 frida-compile：环境变量 → repo .venv → agent node_modules → PATH。"""
    env = os.environ.get("FRIDA_COMPILE")
    if env and Path(env).exists():
        return env
    for cand in (
        _REPO_ROOT / ".venv" / "bin" / "frida-compile",
        project_root / "node_modules" / ".bin" / "frida-compile",
        _AGENT_DIR / "node_modules" / ".bin" / "frida-compile",
    ):
        if cand.exists():
            return str(cand)
    return shutil.which("frida-compile")


def _managed_bundle_paths(requested: Path) -> tuple[Path, Path] | None:
    """Return ``(source, state_output)`` for one code/cache bundle path."""

    code_build = (_AGENT_DIR / "_build").resolve(strict=False)
    cache_build = (_CACHE_ROOT / "_build").resolve(strict=False)
    parent = requested.parent.resolve(strict=False)
    if parent not in {code_build, cache_build}:
        return None
    return _AGENT_DIR / "src" / requested.name, _CACHE_ROOT / "_build" / requested.name


def _copy_if_changed(source: Path, destination: Path) -> bool:
    try:
        unchanged = destination.is_file() and destination.read_bytes() == source.read_bytes()
    except OSError:
        unchanged = False
    if unchanged:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _dependency_digest() -> str:
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        path = _AGENT_DIR / name
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _project_digest() -> str:
    digest = hashlib.sha256()
    inputs = [
        path
        for name in ("package.json", "package-lock.json")
        if (path := _AGENT_DIR / name).is_file()
    ]
    source_root = _AGENT_DIR / "src"
    if source_root.is_dir():
        inputs.extend(path for path in source_root.rglob("*") if path.is_file())
    for path in sorted(inputs):
        digest.update(str(path.relative_to(_AGENT_DIR)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_marker(output: Path) -> Path:
    return output.with_name(f".{output.name}.inputs.sha256")


def _cached_bundle_is_fresh(output: Path, project_digest: str) -> bool:
    if not output.is_file():
        return False
    try:
        recorded_input, recorded_output = _bundle_marker(output).read_text(
            encoding="utf-8",
        ).strip().split(" ", 1)
        return (
            recorded_input == project_digest
            and recorded_output == _file_digest(output)
        )
    except (OSError, ValueError):
        return False


def _record_cached_bundle(output: Path, project_digest: str) -> None:
    _bundle_marker(output).write_text(
        f"{project_digest} {_file_digest(output)}\n",
        encoding="utf-8",
    )


def _sync_cache_project() -> tuple[Path, bool]:
    """Mirror build inputs into mutable state and report dependency drift."""

    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        source = _AGENT_DIR / name
        if source.is_file():
            _copy_if_changed(source, _CACHE_ROOT / name)
        else:
            (_CACHE_ROOT / name).unlink(missing_ok=True)
    source_root = _AGENT_DIR / "src"
    cached_source_root = _CACHE_ROOT / "src"
    if cached_source_root.is_dir():
        for cached in cached_source_root.rglob("*"):
            if cached.is_file() and not (
                source_root / cached.relative_to(cached_source_root)
            ).is_file():
                cached.unlink()
    if source_root.is_dir():
        for source in source_root.rglob("*"):
            if source.is_file():
                _copy_if_changed(source, _CACHE_ROOT / "src" / source.relative_to(source_root))
    expected = _dependency_digest()
    marker = _CACHE_ROOT / ".dependencies.sha256"
    try:
        installed = marker.read_text(encoding="utf-8").strip()
    except OSError:
        installed = ""
    return marker, installed != expected or not (_CACHE_ROOT / "node_modules").is_dir()


def ensure_agent_bundle(
    agent_js: str | Path,
    npm_target: str | None = None,
) -> Path:
    """Return a fresh bundle path, using mutable state for repository agents.

    Callers must load the returned path; a requested ``CODE_ROOT/.../_build``
    path is a logical source identifier, not the runtime output location.
    """
    requested = Path(agent_js)
    managed = _managed_bundle_paths(requested)
    project_digest: str | None = None
    if managed is None:
        source = requested.parent.parent / "src" / requested.name
        output = requested
        project_root = requested.parent.parent
    else:
        source, output = managed
        project_root = _CACHE_ROOT
        project_digest = _project_digest()
    if (
        _bundle_is_fresh(output, source)
        if project_digest is None
        else _cached_bundle_is_fresh(output, project_digest)
    ):
        return output

    scope = _cache_build_lock() if managed is not None else nullcontext()
    with scope:
        # Another process may have completed the same build while this process
        # waited for the cache lease. Recompute the code digest and recheck.
        if managed is not None:
            refreshed = _managed_bundle_paths(requested)
            if refreshed is None:  # pragma: no cover - constants cannot change under the lock
                raise RuntimeError(f"managed agent path changed during build: {requested}")
            source, output = refreshed
            project_root = _CACHE_ROOT
            project_digest = _project_digest()
        if (
            _bundle_is_fresh(output, source)
            if project_digest is None
            else _cached_bundle_is_fresh(output, project_digest)
        ):
            return output

        # Developer checkouts may already have a fresh ignored bundle. Seed the
        # state cache from it without ever writing back into the code tree.
        if managed is not None and _bundle_is_fresh(requested, source):
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(requested, output)
            _record_cached_bundle(output, project_digest)
            return output

        dependency_marker: Path | None = None
        install_dependencies = not (project_root / "node_modules").is_dir()
        if managed is not None:
            dependency_marker, install_dependencies = _sync_cache_project()
            source = project_root / "src" / requested.name

        target = npm_target or agent_bundle_target(requested)
        manual = (
            f"cd {project_root} && "
            f"FRIDA_COMPILE={_REPO_ROOT / '.venv' / 'bin' / 'frida-compile'} "
            f"npm run build:{target}"
        )

        npm = shutil.which("npm")
        if npm is None:
            raise FileNotFoundError(
                f"agent bundle missing: {agent_js}; 且未找到 npm 无法自动构建。手动构建：\n{manual}"
            )

        frida_compile = _resolve_frida_compile(project_root)
        if frida_compile is None:
            raise FileNotFoundError(
                f"agent bundle missing: {agent_js}; 未找到 frida-compile（装 frida uv "
                f"group：`uv sync --group frida`）。手动构建：\n{manual}"
            )

        env = dict(os.environ, FRIDA_COMPILE=frida_compile)

        # node_modules 缺失或依赖清单变化时，只在 state cache 安装。
        if install_dependencies:
            install = subprocess.run(
                [npm, "install"], cwd=project_root, env=env,
                capture_output=True, text=True,
            )
            if install.returncode != 0:
                raise FileNotFoundError(
                    f"agent bundle missing: {agent_js}; `npm install` 失败：\n"
                    f"{install.stderr.strip() or install.stdout.strip()}\n手动构建：\n{manual}"
                )
            if dependency_marker is not None:
                dependency_marker.write_text(_dependency_digest() + "\n", encoding="utf-8")

        build = subprocess.run(
            [npm, "run", f"build:{target}"], cwd=project_root, env=env,
            capture_output=True, text=True,
        )
        if build.returncode != 0 or not _bundle_is_fresh(output, source):
            direct_error = _compile_direct(output, source=source, project_root=project_root)
            if direct_error is None and _bundle_is_fresh(output, source):
                if project_digest is not None:
                    _record_cached_bundle(output, project_digest)
                return output
            raise FileNotFoundError(
                f"agent bundle missing: {output}; 自动构建 build:{target} 失败：\n"
                f"{build.stderr.strip() or build.stdout.strip()}\n"
                f"Python compiler fallback: {direct_error}\n手动构建：\n{manual}"
            )
        if project_digest is not None:
            _record_cached_bundle(output, project_digest)
        return output
