"""Hatch hook shared by workspace builds and standalone component sdists."""
from pathlib import Path
import runpy
import subprocess
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_rules = Path(__file__).with_name("ownership.py")
if not _rules.exists():
    _rules = Path(__file__).resolve().parents[1] / "ownership.py"
if not _rules.exists():
    _rules = Path(__file__).resolve().parent / "pkgs" / "ownership.py"
_ownership = runpy.run_path(str(_rules))
COMPONENTS = _ownership["COMPONENTS"]
owner = _ownership["owner"]
source_files = _ownership["source_files"]


def _tracked_git_files(source: Path) -> set[str] | None:
    """Return tracked repository paths when building from a git checkout."""
    if not (source / ".git").exists():
        return None
    output = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "-z", "--cached"],
    )
    return {
        item
        for item in output.decode("utf-8", errors="replace").split("\0")
        if item
    }


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        component = self.metadata.name.removeprefix("autophone-")
        if component not in COMPONENTS:
            raise ValueError(f"unknown component: {component}")
        # Component sdist has local sources; workspace sources live two levels up.
        local = (root / "autophone").is_dir()
        source = root if local else root.parents[1]
        for legal_file in ("LICENSE", "NOTICE"):
            if not local and (root / legal_file).read_bytes() != (source / legal_file).read_bytes():
                raise ValueError(f"{component}: copy the repository {legal_file} into the component before building")
        from pathspec import GitIgnoreSpec
        matchers = {}

        def excluded(path):
            current = path.parent
            while current == source or source in current.parents:
                if current not in matchers:
                    ignore = current / ".gitignore"
                    matchers[current] = GitIgnoreSpec.from_lines(ignore.read_text().splitlines() if ignore.is_file() else [])
                if matchers[current].match_file(path.relative_to(current).as_posix()):
                    return True
                current = current.parent
            return False

        tracked = None if local else _tracked_git_files(source)
        selected = {}
        for path in source_files(source):
            relative = path.relative_to(source).as_posix()
            if owner(relative) != component:
                continue
            if relative.startswith(("tools/phase4/", "tools/phase5/tests/")):
                continue
            if tracked is not None and relative not in tracked:
                continue
            if not local and excluded(path):
                continue
            selected[str(path)] = relative
        if not any(p.startswith("autophone/") for p in selected.values()):
            raise ValueError(f"no sources for {component}")
        build_data["force_include"].update(selected)
        if self.target_name == "sdist":
            support = root / "build_support.py" if local else source / "packaging_support.py"
            build_data["force_include"][str(support)] = "build_support.py"
            build_data["force_include"][str(_rules)] = "ownership.py"
        elif version == "editable":
            # All workspace members use the same source tree; no source copies.
            build_data["force_include"].clear()
            self.build_config.target_config["dev-mode-dirs"] = [str(source)]
