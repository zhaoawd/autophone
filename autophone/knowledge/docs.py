"""Machine-owned Markdown views for the App knowledge inventory."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from autophone.paths import CODE_ROOT
from autophone.assets.apps import AppProfile, list_app_profiles
from autophone.assets.policy import assert_assets_writable
from autophone.knowledge.inventory import inventory
from autophone.util import write_text_atomic


def render_inventory(profiles: Iterable[AppProfile], *, root: Path = CODE_ROOT) -> str:
    lines = [
        "# App 知识库存（自动生成）",
        "",
        "> 此文件由 `autophone knowledge generate` 整文件生成。人工说明写入同目录的非 generated 文档。",
        "",
    ]
    for profile in sorted(profiles, key=lambda item: item.app):
        view = inventory(profile.app, root=root)
        lines.extend([
            f"## {profile.app}",
            "",
            f"- Bundle ID：`{view['bundle_id']}`",
            f"- Default slot：`{view['default_slot'] or 'unknown'}`",
            f"- Profile build label：`{view['profile_build_label'] or 'unknown'}`",
            "",
            "| Slot | Default | Version | Short version | Build version | Registry IPA SHA-256 |",
            "|---|---:|---|---|---|---|",
        ])
        for build in view["builds"]:
            lines.append(
                f"| {build.get('slot') or 'unknown'} | {'yes' if build.get('default') else 'no'} "
                f"| {build.get('version') or 'unknown'} | {build.get('short_version') or 'unknown'} "
                f"| {build.get('build_version') or 'unknown'} | `{build.get('sha256') or 'unknown'}` |"
            )
        if not view["builds"]:
            lines.append("| unknown | no | unknown | unknown | unknown | `unknown` |")
        assets = view["assets"]
        lines.extend([
            "",
            f"资产计数：Case {assets['cases']}，Screen {assets['screens']}，Operation {assets['operations']}，"
            f"Recipe {assets['recipes']}，API Expectation 文件 {assets['api_expectations']}，Claim 文件 {assets['claims']}，"
            f"Coverage Cell {assets['coverage_cells']}。",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def generate_inventory(
    output: Path | None = None,
    *,
    root: Path = CODE_ROOT,
) -> Path:
    target = output or (root / "docs" / "app-knowledge" / "inventory.generated.md")
    assert_assets_writable("generate App knowledge inventory", path=target)
    return write_text_atomic(target, render_inventory(list_app_profiles(root=root), root=root))
