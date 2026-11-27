"""测试模拟点 / 送餐地址注册表（seed ∪ overlay）。

- seed（tracked，只读）：``autophone/_data/location-points.yaml``——随仓库走，评审可见。
- overlay（untracked，本机可变）：``config/location-points.local.yaml``——页面收藏点写这里，各机自留。

读取语义是**纯并集**：seed 与 overlay 的 ``id`` 严格不相交，重复即 fail-closed
（不做「overlay 覆盖 seed」的隐式合并，见 docs/domains/location.md；
原始设计在 docs/archive/domain-location-202609/platform-location-move-page.md §3.1）。
真实门店由 :mod:`autophone.assets.store_catalog` 提供；本模块不再承担门店
编码解析，以免测试模拟值冒充基础数据。坐标一律 GCJ-02。
"""

from __future__ import annotations

from pathlib import Path

from autophone.paths import STATE_ROOT
from typing import Any

import yaml

_SEED_PATH = Path(__file__).resolve().parent.parent / "_data" / "location-points.yaml"
_OVERLAY_PATH = STATE_ROOT / "config" / "location-points.local.yaml"

# ``store`` is accepted only while reading legacy local overlays so upgrades do
# not brick the whole registry. New writes reject it; canonical stores live in
# store-catalog and legacy rows are never used as store anchors.
VALID_KINDS = {"custom", "delivery", "store"}


class PointRegistryError(ValueError):
    """注册表配置错误（id 重复 / 缺坐标 / 非法 kind）——加载期 fail-closed。"""


class PointExistsError(ValueError):
    """收藏点 id 已存在于 seed 或 overlay（api 转 409）。"""


class PointReadonlyError(ValueError):
    """试图删除 seed 只读点（api 转 409 readonly_seed_point）。"""


class PointNotFoundError(KeyError):
    """删除的 overlay 点不存在（api 转 404）。"""


_POINT_FIELDS = ("id", "kind", "label", "app", "city", "lat", "lng")


def _read_points(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    points = doc.get("points") or []
    if not isinstance(points, list):
        raise PointRegistryError(f"{path}: 'points' must be a list")
    return [p for p in points if isinstance(p, dict)]


def _validate(point: dict[str, Any], *, source: Path) -> None:
    pid = point.get("id")
    if not pid:
        raise PointRegistryError(f"{source}: point missing 'id'")
    kind = point.get("kind")
    if kind not in VALID_KINDS:
        raise PointRegistryError(f"{source}: point {pid!r} kind={kind!r} not in {sorted(VALID_KINDS)}")
    for axis in ("lat", "lng"):
        try:
            float(point[axis])
        except (KeyError, TypeError, ValueError) as exc:
            raise PointRegistryError(f"{source}: point {pid!r} bad {axis}={point.get(axis)!r}") from exc


def load_points(
    *,
    seed_path: Path | None = None,
    overlay_path: Path | None = None,
) -> list[dict[str, Any]]:
    """合并 seed ∪ overlay，返回校验过的点列表。id 冲突 fail-closed。"""
    seed_path = seed_path or _SEED_PATH
    overlay_path = overlay_path or _OVERLAY_PATH
    merged: dict[str, dict[str, Any]] = {}
    for source, is_overlay in ((seed_path, False), (overlay_path, True)):
        for point in _read_points(source):
            _validate(point, source=source)
            pid = str(point["id"])
            if pid in merged:
                raise PointRegistryError(
                    f"duplicate point id {pid!r} across seed/overlay; ids must be disjoint"
                )
            merged[pid] = {**point, "id": pid, "_overlay": is_overlay}
    return list(merged.values())


def get_point(
    pid: str,
    *,
    seed_path: Path | None = None,
    overlay_path: Path | None = None,
) -> dict[str, Any]:
    """按 id 取单个点（seed ∪ overlay）；不存在 → KeyError。"""
    pid = str(pid)
    for point in load_points(seed_path=seed_path, overlay_path=overlay_path):
        if str(point["id"]) == pid:
            return point
    raise KeyError(pid)


def _normalized_point(point: dict[str, Any]) -> dict[str, Any]:
    out = {k: point[k] for k in _POINT_FIELDS if k in point and point[k] not in (None, "")}
    out["id"] = str(out["id"])
    out["lat"] = float(out["lat"])
    out["lng"] = float(out["lng"])
    return out


def _write_overlay(path: Path, points: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [{k: p[k] for k in _POINT_FIELDS if k in p} for p in points]
    path.write_text(
        yaml.safe_dump({"points": body}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_point(
    point: dict[str, Any],
    *,
    seed_path: Path | None = None,
    overlay_path: Path | None = None,
) -> dict[str, Any]:
    """收藏一个点：只写 overlay；id 已存在于 seed 或 overlay → :class:`PointExistsError`。"""
    seed_path = seed_path or _SEED_PATH
    overlay_path = overlay_path or _OVERLAY_PATH
    if point.get("kind") == "store":
        raise PointRegistryError(
            "kind=store is managed by store-catalog; save simulations as kind=custom"
        )
    _validate(point, source=overlay_path)
    normalized = _normalized_point(point)
    pid = normalized["id"]
    existing = {p["id"] for p in load_points(seed_path=seed_path, overlay_path=overlay_path)}
    if pid in existing:
        raise PointExistsError(f"point id {pid!r} already exists")
    overlay = [_normalized_point(p) for p in _read_points(overlay_path)]
    overlay.append(normalized)
    _write_overlay(overlay_path, overlay)
    return normalized


def remove_point(
    pid: str,
    *,
    seed_path: Path | None = None,
    overlay_path: Path | None = None,
) -> None:
    """删点：只删 overlay。seed 点只读 → :class:`PointReadonlyError`；不存在 → :class:`PointNotFoundError`。"""
    seed_path = seed_path or _SEED_PATH
    overlay_path = overlay_path or _OVERLAY_PATH
    pid = str(pid)
    seed_ids = {str(p["id"]) for p in _read_points(seed_path)}
    if pid in seed_ids:
        raise PointReadonlyError(f"point id {pid!r} is a read-only seed point")
    overlay = [_normalized_point(p) for p in _read_points(overlay_path)]
    kept = [p for p in overlay if p["id"] != pid]
    if len(kept) == len(overlay):
        raise PointNotFoundError(pid)
    _write_overlay(overlay_path, kept)
