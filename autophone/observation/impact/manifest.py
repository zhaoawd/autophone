"""后端变更清单摄取与输入能力分级（影响图谱 §十）。

影响定位的质量完全由输入决定，所以这里的任务不是「尽量猜」，而是**如实分级**：

    key + endpoint + JSONPath  → 最高：精确反查 FieldBinding
    key + endpoint             → 中：执行该 endpoint 关联的全部页面状态
    只有 key                   → 低：查人工 catalog；缺 binding 就返回 gap
    只有响应快照               → 中低：JSON diff 后匹配已知 response path
    只有「发布了」             → 不可用：拒绝猜影响面，要求补 manifest

最后一档是有意的 fail-closed。「发布了一个东西，你去看看哪里受影响」不该被翻译成
「把整个 App 跑一遍」——那既跑不完，结论也没有价值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from autophone.contracts import validate_or_raise
from autophone.observation.impact.endpoints import (
    EndpointNormalizeError,
    normalize_endpoint,
    normalize_response_path,
    response_paths as enumerate_response_paths,
)

QUALITY_KEY_ENDPOINT_JSONPATH = "key_endpoint_jsonpath"
QUALITY_KEY_ENDPOINT = "key_endpoint"
QUALITY_KEY_ONLY = "key_only"
QUALITY_RESPONSE_SNAPSHOT = "response_snapshot"
QUALITY_UNUSABLE = "unusable"

# 从最好到最差；plan 层取所有变更里最差的一档，让读者一眼知道结论被什么拖住了
QUALITY_ORDER = (
    QUALITY_KEY_ENDPOINT_JSONPATH,
    QUALITY_KEY_ENDPOINT,
    QUALITY_RESPONSE_SNAPSHOT,
    QUALITY_KEY_ONLY,
    QUALITY_UNUSABLE,
)


class ChangeManifestError(ValueError):
    pass


@dataclass(frozen=True)
class IngestedChange:
    data_keys: tuple[str, ...]
    endpoint_keys: tuple[str, ...]
    response_paths: tuple[str, ...]
    quality: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.data_keys:
            return self.data_keys[0]
        if self.endpoint_keys:
            return self.endpoint_keys[0]
        return "<unlabelled-change>"


@dataclass(frozen=True)
class IngestedManifest:
    change_id: str
    env: str
    app: str | None
    build: str | None
    changes: tuple[IngestedChange, ...]
    quality: str
    raw: dict[str, Any] = field(default_factory=dict)


def load(path: Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ChangeManifestError(f"change manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(doc, dict):
        raise ChangeManifestError("change manifest 顶层必须是对象")
    validate_or_raise("change-manifest.v1.json", doc)
    return doc


def _change_quality(
    data_keys: Iterable[str],
    endpoint_keys: Iterable[str],
    paths: Iterable[str],
    *,
    from_snapshot: bool,
) -> str:
    has_key = bool(list(data_keys))
    has_endpoint = bool(list(endpoint_keys))
    has_path = bool(list(paths))
    if has_key and has_endpoint and has_path:
        return QUALITY_KEY_ENDPOINT_JSONPATH
    if has_key and has_endpoint:
        return QUALITY_KEY_ENDPOINT
    if from_snapshot and has_path:
        return QUALITY_RESPONSE_SNAPSHOT
    if has_endpoint and has_path:
        # 没给 key 但给了接口和字段：定位能力等同 key+endpoint+JSONPath 的下一档
        return QUALITY_KEY_ENDPOINT
    if has_key:
        return QUALITY_KEY_ONLY
    return QUALITY_UNUSABLE


def _worst(qualities: Iterable[str]) -> str:
    order = {q: i for i, q in enumerate(QUALITY_ORDER)}
    items = [q for q in qualities if q in order]
    if not items:
        return QUALITY_UNUSABLE
    return max(items, key=lambda q: order[q])


def ingest(
    doc: dict[str, Any],
    *,
    host_aliases: dict[str, str] | None = None,
) -> IngestedManifest:
    """change manifest → 标准化的反查输入。

    endpoint 一律过标准化：后端给 ``url``、给 ``host+path_template``、或直接给
    ``host_alias+path_template`` 都收敛到同一个键，否则 manifest 与 binding 永远对不上。
    """
    changes: list[IngestedChange] = []
    for raw in doc.get("changes") or []:
        if not isinstance(raw, dict):
            continue
        keys = tuple(
            str(raw[k]) for k in ("data_key", "config_key") if raw.get(k)
        )
        endpoint_keys: list[str] = []
        paths: list[str] = []
        for endpoint in raw.get("endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            try:
                key = normalize_endpoint(
                    endpoint.get("method"),
                    endpoint.get("url"),
                    host=endpoint.get("host") or endpoint.get("host_alias"),
                    path=endpoint.get("path_template"),
                    aliases=host_aliases,
                )
            except EndpointNormalizeError:
                continue
            if key.key not in endpoint_keys:
                endpoint_keys.append(key.key)
            for path in endpoint.get("response_paths") or []:
                normalized = normalize_response_path(path)
                if normalized and normalized not in paths:
                    paths.append(normalized)
        changes.append(
            IngestedChange(
                data_keys=keys,
                endpoint_keys=tuple(endpoint_keys),
                response_paths=tuple(paths),
                quality=_change_quality(keys, endpoint_keys, paths, from_snapshot=False),
                raw=dict(raw),
            )
        )
    if not changes:
        raise ChangeManifestError("change manifest 没有任何可解析的变更条目")
    return IngestedManifest(
        change_id=str(doc.get("change_id")),
        env=str(doc.get("env") or ""),
        app=(str(doc["app"]) if doc.get("app") else None),
        build=(str(doc["build"]) if doc.get("build") else None),
        changes=tuple(changes),
        quality=_worst(c.quality for c in changes),
        raw=dict(doc),
    )


def from_response_snapshots(
    *,
    change_id: str,
    env: str,
    method: str,
    url: str,
    before: Any,
    after: Any,
    host_aliases: dict[str, str] | None = None,
) -> IngestedManifest:
    """降级输入：没有 manifest，只有同一个接口的前后响应快照。

    做结构 diff（只比路径集合与叶子值是否变化，**不保留值**），产出 response_paths；
    定位质量最高只到 ``response_snapshot``——没有后端 key，就说不出「是哪个配置改的」。
    """
    key = normalize_endpoint(method, url, aliases=host_aliases)
    changed = sorted(_changed_paths(before, after))
    change = IngestedChange(
        data_keys=(),
        endpoint_keys=(key.key,),
        response_paths=tuple(changed),
        quality=(QUALITY_RESPONSE_SNAPSHOT if changed else QUALITY_UNUSABLE),
        raw={"note": "derived from response snapshot diff"},
    )
    return IngestedManifest(
        change_id=str(change_id),
        env=str(env),
        app=None,
        build=None,
        changes=(change,),
        quality=change.quality,
        raw={"schema_version": "v1", "change_id": change_id, "env": env, "changes": []},
    )


def changed_paths(before: Any, after: Any) -> set[str]:
    """结构化 diff → 变化的 JSONPath 集合（不携带任何字段值）。"""
    def validate(node: Any, depth: int = 0) -> None:
        if depth > 8:
            raise ChangeManifestError("snapshot_depth_limit")
        if isinstance(node, dict):
            for key, value in node.items():
                if not key or any(char in key for char in ".[]"):
                    raise ChangeManifestError("snapshot_path_not_addressable")
                validate(value, depth + 1)
        elif isinstance(node, list) and node:
            validate(node[0], depth + 1)

    validate(before)
    validate(after)
    def leaves(root: Any) -> dict[str, Any]:
        paths = enumerate_response_paths(root, max_paths=401)
        if len(paths) > 400:
            raise ChangeManifestError("snapshot_path_limit")
        result = {p: _leaf(root, p) for p in paths}
        # Presence and JSON types matter, including empty containers and null.
        return result

    def different(left: Any, right: Any) -> bool:
        if isinstance(left, bool) != isinstance(right, bool):
            return True
        return left != right

    left, right = leaves(before), leaves(after)
    changed = {p for p in set(left) | set(right)
               if p not in left or p not in right or different(left[p], right[p])}
    if not isinstance(before, (dict, list)) or not isinstance(after, (dict, list)):
        if different(before, after):
            changed.add("$")
    elif type(before) is not type(after):
        changed.add("$")
    return changed


# Historical internal spelling remains available to callers.
_changed_paths = changed_paths


_MISSING = object()


def _leaf(root: Any, path: str) -> Any:
    """按归一化路径取值；数组的 ``[*]`` 取第一个元素（与路径枚举同一约定）。"""
    node = root
    token = ""
    for char in path[1:]:  # 跳过起始的 $
        if char in ".[":
            if token:
                node = _descend(node, token)
                token = ""
            continue
        if char == "]":
            if token == "*":
                node = node[0] if isinstance(node, list) and node else _MISSING
            token = ""
            continue
        token += char
        if node is _MISSING:
            return _MISSING
    if token:
        node = _descend(node, token)
    if isinstance(node, (dict, list)):
        return (type(node).__name__,)  # 容器只保留类型，子路径承担值比较
    return node


def _descend(node: Any, key: str) -> Any:
    if isinstance(node, dict) and key in node:
        return node[key]
    return _MISSING
