"""ai_output_schema.py — AI 场景输出 Schema 的传输形态与内容形态互转。

各创建期 AI 场景用「内容 Schema」描述并在本地校验模型回包；发给 provider 的是「传输 Schema」。
Codex 结构化输出（OpenAI Structured Outputs 严格模式）要求每个对象把全部字段列入 ``required``，
可选字段只能以显式 ``null`` 表达。本模块把「可选字段」这一类差异机械地转换：

- :func:`strict_transport_schema`：每个声明了 ``properties`` 的对象把全部字段列入 ``required``，
  原本可选的字段加入 ``null`` 类型。其他结构（``oneOf``、开放对象等）原样保留，由场景自行处理，
  并由 ``tests/test_ai_output_schema_contract.py`` 检查最终发给 provider 的 Schema。
- :func:`drop_optional_nulls`：把回包中「原本可选、且内容 Schema 不接受 null」的 ``null`` 字段删除，
  得到内容形态，再交给原有的内容 Schema 与契约校验。内容形态的回包经过本函数保持不变。

两函数不放宽校验：传输形态的回包经归一化后仍须通过原内容 Schema。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def _accepts_null(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    declared = schema.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    if "enum" in schema and None in (schema.get("enum") or []):
        return True
    return any(_accepts_null(option) for option in schema.get("anyOf") or [])


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if _accepts_null(schema):
        return schema
    out = dict(schema)
    if "type" in out:
        declared = out["type"]
        out["type"] = [*declared, "null"] if isinstance(declared, list) else [declared, "null"]
        if "enum" in out:
            out["enum"] = [*out["enum"], None]
        return out
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
        return out
    if "anyOf" in out:
        out["anyOf"] = [*out["anyOf"], {"type": "null"}]
        return out
    return {"anyOf": [out, {"type": "null"}]}


def strict_transport_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """返回传输 Schema：对象字段全部必填，原可选字段改为可空。不修改入参。"""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {key: walk(value) for key, value in node.items()}
        props = out.get("properties")
        if isinstance(props, dict):
            required = set(node.get("required") or [])
            out["properties"] = {
                key: (value if key in required else _nullable(value))
                for key, value in props.items()
            }
            out["required"] = list(props)
        return out

    return walk(deepcopy(schema))


def drop_optional_nulls(schema: dict[str, Any], payload: Any) -> Any:
    """按内容 Schema 删除可选字段上的 ``null``，返回新对象；内容形态输入原样返回等值对象。"""
    if isinstance(payload, list):
        items = schema.get("items") if isinstance(schema, dict) else None
        return [drop_optional_nulls(items, value) if isinstance(items, dict) else value for value in payload]
    if not isinstance(payload, dict) or not isinstance(schema, dict):
        return payload
    props = schema.get("properties")
    if not isinstance(props, dict):
        return dict(payload)
    required = set(schema.get("required") or [])
    out: dict[str, Any] = {}
    for key, value in payload.items():
        prop = props.get(key)
        if value is None and key not in required and prop is not None and not _accepts_null(prop):
            continue
        out[key] = drop_optional_nulls(prop, value) if isinstance(prop, dict) else value
    return out
