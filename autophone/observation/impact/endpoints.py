"""Endpoint identity 与响应字段寻址的标准化（影响图谱 §6.5）。

一条接口的身份是

    METHOD + normalized_host_alias + normalized_path_template

**真实门店、商品、账号、订单 ID 绝不进 identity**——否则同一个接口会因为业务对象不同
裂成无数条，字段绑定与覆盖统计立刻失效，而且真实 ID 会经由资产文件泄露出去。

标准化是幂等的：已经模板化的路径（``/store/{id}/menu``）再跑一次不变，所以后端
manifest 直接给的 path_template 和抓包 URL 推出来的 path_template 落在同一个键上。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# 段级模板占位符：{id} / {num} / {uuid} / {hex}，也接受后端自带的 {storeId} 这类命名占位符
_PLACEHOLDER = re.compile(r"^\{[^/{}]*\}$")
_ALL_DIGITS = re.compile(r"^\d+$")
_VERSION = re.compile(r"^v\d+(\.\d+)*$", re.IGNORECASE)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")
_HAS_DIGIT = re.compile(r"\d")
_DIGIT_RUN = re.compile(r"\d+")
# 数组下标：$.a[0].b / $['a'][12] → 归一成 [*]，让「第 3 个商品」和「第 7 个商品」同键
_ARRAY_INDEX = re.compile(r"\[\s*\d+\s*\]")

# 兜底 ID 判据：长且含数字的段大概率是业务对象 ID（订单号/门店号/商品编码）
_ID_MIN_LEN_WITH_DIGIT = 6
# 纯字母但极长的段（base64 token / 会话串）同样不该进 identity
_ID_MIN_LEN_OPAQUE = 24


class EndpointNormalizeError(ValueError):
    """URL / path 无法标准化成 endpoint identity。"""


@dataclass(frozen=True)
class EndpointKey:
    method: str
    host_alias: str
    path_template: str

    @property
    def key(self) -> str:
        return f"{self.method} {self.host_alias}{self.path_template}"

    def as_dict(self) -> dict[str, str]:
        return {
            "endpoint_key": self.key,
            "method": self.method,
            "host_alias": self.host_alias,
            "path_template": self.path_template,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.key


def host_alias(host: str | None, *, aliases: dict[str, str] | None = None) -> str:
    """真实 host → 稳定别名。

    先查显式别名表（后端给的正式服务名优先），否则把每个标签里的数字串折成 ``#``：
    ``api-shard-1.example.com`` → ``api-shard-#.example.com``。这样分片/环境编号不同的
    同一服务落同一个键，而域名本身仍然人读得懂。
    """
    raw = str(host or "").strip().lower()
    if not raw:
        raise EndpointNormalizeError("endpoint 缺少 host，无法标准化 identity")
    raw = raw.split("@")[-1]  # 丢掉 userinfo，凭据不进 identity
    if raw.startswith("["):  # IPv6 字面量
        raw = raw.split("]")[0] + "]"
    else:
        raw = raw.split(":")[0]  # 丢端口：同服务换端口不该裂键
    explicit = (aliases or {}).get(raw)
    if explicit:
        return str(explicit)
    return _DIGIT_RUN.sub("#", raw)


def _template_segment(segment: str) -> str:
    if not segment or _PLACEHOLDER.match(segment):
        return segment
    if _VERSION.match(segment):
        return segment  # v1 / v2.1 是接口版本，属于 identity
    if _ALL_DIGITS.match(segment):
        return "{num}"
    if _UUID.match(segment):
        return "{uuid}"
    if _HEX.match(segment):
        return "{hex}"
    # 带扩展名的静态资源：只模板化主名，保留扩展名的语义
    stem, dot, ext = segment.rpartition(".")
    if dot and stem and len(ext) <= 5 and ext.isalnum() and not _HAS_DIGIT.search(ext):
        templated = _template_segment(stem)
        return f"{templated}.{ext}" if templated != stem else segment
    if _HAS_DIGIT.search(segment) and len(segment) >= _ID_MIN_LEN_WITH_DIGIT:
        return "{id}"
    if len(segment) >= _ID_MIN_LEN_OPAQUE:
        return "{id}"
    return segment


def path_template(path: str | None) -> str:
    """路径 → 模板化路径。查询串和 fragment 直接丢弃（值不进 identity）。"""
    raw = str(path or "").strip()
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    if not raw:
        return "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    trailing = raw.endswith("/") and raw != "/"
    segments = [_template_segment(seg) for seg in raw.split("/")]
    out = "/".join(segments)
    if trailing and not out.endswith("/"):
        out += "/"
    return out or "/"


def normalize_endpoint(
    method: str | None,
    url: str | None = None,
    *,
    host: str | None = None,
    path: str | None = None,
    aliases: dict[str, str] | None = None,
) -> EndpointKey:
    """(method, url) 或 (method, host, path) → EndpointKey。

    ``url`` 是抓包侧的输入；``host``/``path``（或后端给的 path_template）是 manifest 侧
    的输入。两条入口必须收敛到同一个键，否则 change manifest 反查不到 binding。
    """
    verb = str(method or "").strip().upper()
    if not verb:
        raise EndpointNormalizeError("endpoint 缺少 method")
    resolved_host = host
    resolved_path = path
    if url:
        parts = urlsplit(str(url).strip())
        if parts.netloc:
            resolved_host = resolved_host or parts.netloc
            resolved_path = resolved_path or parts.path
        else:
            # 没有 scheme://，整串当路径（抓包偶尔只给相对 URL）
            resolved_path = resolved_path or str(url)
    return EndpointKey(
        method=verb,
        host_alias=host_alias(resolved_host, aliases=aliases),
        path_template=path_template(resolved_path),
    )


def endpoint_key(
    method: str | None,
    url: str | None = None,
    *,
    host: str | None = None,
    path: str | None = None,
    aliases: dict[str, str] | None = None,
) -> str:
    return normalize_endpoint(
        method, url, host=host, path=path, aliases=aliases
    ).key


def endpoint_key_from_binding(endpoint: dict[str, Any] | None) -> str | None:
    """binding / plan 里已经标准化过的 endpoint 段 → 键字符串。"""
    if not isinstance(endpoint, dict):
        return None
    method = str(endpoint.get("method") or "").strip().upper()
    alias = str(endpoint.get("host_alias") or "").strip()
    template = str(endpoint.get("path_template") or "").strip()
    if not (method and alias and template):
        return None
    return f"{method} {alias}{template}"


# --------------------------------------------------------------------------
# 响应字段寻址
# --------------------------------------------------------------------------

def normalize_response_path(path: str | None) -> str:
    """JSONPath 归一：数组下标 → ``[*]``，去空白，补 ``$`` 前缀。

    ``$.data.categories[0].items[3].soldOut`` 与 ``$.data.categories[*].items[*].soldOut``
    描述的是同一个字段族；不归一，manifest 与观测就永远对不上。
    """
    raw = str(path or "").strip()
    if not raw:
        return ""
    raw = _ARRAY_INDEX.sub("[*]", raw)
    if raw.startswith("$"):
        return raw
    if raw.startswith("."):
        return "$" + raw
    return "$." + raw


def response_paths(
    body: Any,
    *,
    max_paths: int = 400,
    max_depth: int = 8,
) -> list[str]:
    """响应体 → 结构化 JSONPath 列表（**只留结构，不留值**）。

    这是 observation 里唯一记录的响应侧信息：字段值可能是手机号、地址、订单号或会员
    资产，一律不进图谱资产；能进的只有「这个接口这次返回里出现过哪些字段路径」。
    数组只展开第一个元素并记成 ``[*]``，避免长列表把路径集炸开。
    """
    out: list[str] = []
    seen: set[str] = set()

    def walk(node: Any, prefix: str, depth: int) -> None:
        if len(out) >= max_paths or depth > max_depth:
            return
        if isinstance(node, dict):
            for key in node:
                child = f"{prefix}.{key}"
                if child not in seen:
                    seen.add(child)
                    out.append(child)
                walk(node[key], child, depth + 1)
                if len(out) >= max_paths:
                    return
        elif isinstance(node, list):
            child = f"{prefix}[*]"
            if node:
                if child not in seen:
                    seen.add(child)
                    out.append(child)
                walk(node[0], child, depth + 1)

    walk(body, "$", 0)
    return out


def response_path_matches(candidate: str, pattern: str) -> bool:
    """观测到的路径是否落在 binding 声明的路径族里（两边都先归一）。

    只做前缀族匹配：pattern ``$.data.items[*]`` 命中 ``$.data.items[*].price``。
    不实现完整 JSONPath 过滤器语义——绑定表里用不到，也不该用。
    """
    left = normalize_response_path(candidate)
    right = normalize_response_path(pattern)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.startswith(right + ".") or left.startswith(right + "[")
