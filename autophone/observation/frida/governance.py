#!/usr/bin/env python3
# coding=utf8
"""M4 — 敏感数据治理（§七-bis）。随抓取层一起落地，不是事后补。

原始 JSONL（含真实 PII / 内网拓扑 / 凭据）默认不进 git（.gitignore 已挡 autophone/observation/frida/runs/）。
报告与提交物只含**脱敏摘要**。本模块提供报告面的脱敏：

  - headers allowlist：只保留断言/排障需要的头；Authorization/Cookie/token 头 → <redacted>
  - body 按路径/正则 redact：手机号、身份证、地址、token、sign 等落报告前替换
  - 大 body：摘要不内嵌 body，只给长度 + sha256 指纹，原文回溯靠本地 net_jsonl 路径
  - url：query 里的 token/sign 参数打码
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

# 只放行这些响应/请求头进报告，其余默认丢弃（不是 redact——报告根本不需要）
HEADER_ALLOWLIST = {"content-type", "content-encoding", "content-length", "server", "date"}
# 这些头即使被点名也必须打码（防 allowlist 误配）
HEADER_DENYLIST = {"authorization", "cookie", "set-cookie", "x-auth-token", "token"}

# 敏感字段名（JSON key，大小写不敏感）→ 报告里替换成 <redacted>
SENSITIVE_KEYS = {"token", "sign", "password", "pwd", "idcard", "idcardno", "id_card",
                  "mobile", "phone", "tel", "address", "addr", "lat", "lng", "latitude",
                  "longitude", "accesstoken", "access_token", "refreshtoken"}
# url query 参数同理
SENSITIVE_QUERY = {"token", "sign", "sig", "access_token", "accesstoken", "key"}

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")          # 中国手机号 → 保留前3后4
_IDCARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dxX])(?!\d)")       # 身份证 → 保留前6后4
REDACTED = "<redacted>"


def mask_phone(text: str) -> str:
    text = _PHONE.sub(r"\1****\2", text)
    text = _IDCARD.sub(r"\1********\2", text)
    return text


def redact_headers(headers: dict | None) -> dict:
    if not headers:
        return {}
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in HEADER_DENYLIST:
            out[k] = REDACTED
        elif lk in HEADER_ALLOWLIST:
            out[k] = v
        # 其余头不进报告
    return out


def redact_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if parts.query:
            kvs = []
            for kv in parts.query.split("&"):
                k = kv.split("=", 1)[0]
                kvs.append(f"{k}={REDACTED}" if k.lower() in SENSITIVE_QUERY else kv)
            parts = parts._replace(query="&".join(kvs))
        return urlunsplit(parts)
    except Exception:  # noqa: BLE001
        return url


def redact_value_for_key(key: str, value):
    return REDACTED if key.lower() in SENSITIVE_KEYS else value


def _redact_json(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if k.lower() in SENSITIVE_KEYS else _redact_json(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_json(x) for x in obj]
    if isinstance(obj, str):
        return mask_phone(obj)
    return obj


def body_fingerprint(b64: str | None) -> dict | None:
    """大 body 不内嵌：给长度 + sha256 指纹用于对账。"""
    if not b64:
        return None
    import base64
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    return {"len": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]}


def redact_summary(call: dict) -> dict:
    """合并后的调用 → 报告安全摘要：无原始 body，url/headers/字段脱敏。"""
    return {
        "method": call.get("method"),
        "url": redact_url(call.get("url")),
        "host": call.get("host"),
        "status": call.get("status"),
        "ms": call.get("ms"),
        "case_id": call.get("case_id"),
        "step": call.get("step"),
        "operation": call.get("operation"),
        "req_headers": redact_headers(call.get("req_headers")),
        "resp_content_encoding": call.get("resp_content_encoding"),
        "req_body_fp": body_fingerprint(call.get("req_body_b64")),
        "resp_body_fp": body_fingerprint(call.get("resp_body_b64")),
    }


def redact_assertion(a: dict) -> dict:
    """oracle 断言结果 → 报告安全版：got 值按字段名脱敏 + 手机号打码，url 脱敏。"""
    out = dict(a)
    if "url" in out:
        out["url"] = redact_url(out.get("url"))
    fields = []
    for f in a.get("fields", []) or []:
        g = f.get("got")
        key = (f.get("key") or "").split(".")[-1]
        if isinstance(g, str):
            g = REDACTED if key.lower() in SENSITIVE_KEYS else mask_phone(g)
        fields.append({**f, "got": g})
    if fields:
        out["fields"] = fields
    # detail 文本里也可能带 got 原值 → 整体打码手机号；token/sign 字段已在 fields 处理，detail 重建
    if out.get("fields"):
        bad = [f for f in out["fields"] if not f["ok"]]
        out["detail"] = "; ".join(f"{f['key']} want={f['want']!r} got={f['got']!r}" for f in bad) or "ok"
    elif isinstance(out.get("detail"), str):
        out["detail"] = mask_phone(out["detail"])
    return out
