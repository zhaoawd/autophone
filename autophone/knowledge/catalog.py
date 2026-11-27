"""Read model for existing execution assets plus Knowledge Claims."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.assets.apps import load_app_profile
from autophone.knowledge.inventory import inventory
from autophone.knowledge.store import load_claims
from autophone.execution.engine.cases_loader import load_cases


def _yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


class KnowledgeCatalog:
    def __init__(
        self,
        app: str,
        *,
        root: Path = APP_ASSET_ROOT,
        state_root: Path | None = None,
        snapshot: bool = False,
    ) -> None:
        """``snapshot=True`` parses each registry file once for this instance.

        Meant for one read-only pass over many Cases (the App knowledge view);
        the default re-reads files on every call.  Callers must not mutate the
        returned rows of a snapshot catalog.
        """
        self.app = app
        self.root = root
        self.profile = load_app_profile(app, root=root, state_root=state_root)
        self._memo: dict[str, list[dict[str, Any]]] | None = {} if snapshot else None

    def _rows(self, key: str, load: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        if self._memo is None:
            return load()
        if key not in self._memo:
            self._memo[key] = load()
        return list(self._memo[key])

    def cases(self) -> list[dict[str, Any]]:
        return self._rows("cases", lambda: load_cases(self.profile.cases_path()))

    def case(self, case_id: str) -> dict[str, Any] | None:
        return next((row for row in self.cases() if str(row.get("id")) == case_id), None)

    def recipe(self, case_id: str, *, draft: bool = False) -> dict[str, Any] | None:
        base = self.profile.recipes_dir() / "drafts" if draft else self.profile.recipes_dir()
        return _yaml(base / f"{case_id.lower()}.yaml") or None

    def operations(self) -> list[dict[str, Any]]:
        return self._rows("operations", lambda: list(
            _yaml(self.profile.operations_path()).get("operations") or []))

    def operation(self, name: str) -> dict[str, Any] | None:
        return next(
            (
                row for row in self.operations()
                if str(row.get("name")) == name or str(row.get("id")) == name
            ),
            None,
        )

    def screens(self) -> list[dict[str, Any]]:
        return self._rows("screens", self._load_screens)

    def _load_screens(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        screen_dir = self.profile.profile_dir / str(self.profile.raw.get("screens_dir") or "screens")
        for path in sorted(screen_dir.glob("*.yaml")):
            doc = _yaml(path)
            for row in doc.get("screens") or []:
                rows.append({**row, "build_label": (doc.get("meta") or {}).get("build")})
        return rows

    def screen(self, screen_id: str) -> dict[str, Any] | None:
        return next((row for row in self.screens() if str(row.get("id")) == screen_id), None)

    def api_expectations(self) -> list[dict[str, Any]]:
        return self._rows("api_expectations", lambda: list(
            _yaml(self.profile.api_expectations_path()).get("expectations") or []))

    def api_oracle(self, case_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.api_expectations() if str(row.get("id")) == case_id),
            None,
        )

    def claims(self) -> list[dict[str, Any]]:
        return list(load_claims(self.app, root=self.root).get("claims") or [])

    def inventory(self) -> dict[str, Any]:
        return inventory(
            self.app,
            root=self.root,
            state_root=self.profile.host_state_root,
        )
