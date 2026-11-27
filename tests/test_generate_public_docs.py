"""Verify auto-generated public reference docs stay in sync with code truth."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import generate_public_docs as gen  # noqa: E402


def test_generated_reference_docs_match_disk() -> None:
    rendered = gen.generate_all()
    assert len(rendered) == 4
    for path, expected in rendered.items():
        assert path.is_file(), f"missing generated doc {path}"
        assert path.read_text(encoding="utf-8") == expected, (
            f"{path.relative_to(ROOT)} drifted from code; run scripts/generate_public_docs.py --write"
        )


def test_verbs_doc_covers_all_registered_actions() -> None:
    from autophone.execution.engine.recipe import ACTIONS

    content = gen.render_verbs_doc()
    for verb in ACTIONS:
        assert f"`{verb}`" in content


def test_cli_doc_excludes_experimental_and_delivery_commands() -> None:
    from autophone.cli import EXPERIMENTAL_CLI_COMMANDS

    content = gen.render_cli_doc()
    for core_cmd in ("serve", "doctor", "rig", "location", "app", "run"):
        assert f"`autophone {core_cmd}`" in content
    for hidden in EXPERIMENTAL_CLI_COMMANDS:
        assert f"`autophone {hidden}`" not in content
    assert "`autophone workflow`" not in content
    top_level = {line for line in content.splitlines() if line.startswith("### `autophone ") and line.count(" ") == 2}
    expected = ('ai', 'app', 'control', 'device', 'doctor', 'location', 'operation', 'record', 'replay', 'report', 'rig', 'run', 'schedule', 'screens', 'self-update', 'serve')
    assert top_level == {f"### `autophone {name}`" for name in expected}
