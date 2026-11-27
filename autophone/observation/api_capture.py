"""API 抓取面（planes/api）→ 看板可消费的「API 调用 × 动作」结构。

phase4 的 Frida 进程内抓取产物 `planes/api/capture.jsonl`（一行一条合并后的调用，
schema 见 docs/phase4-api-capture.md §五）在这里被读出、**默认脱敏**、并按 `case_id`+`operation/step`
归因到动作分组，供 `GET /api/runs/{id}/api-capture` 渲染。

治理：复用 phase4 `autophone.observation.frida.governance`（纯 stdlib）的脱敏原语——url/headers/JSON 字段
打码、手机号/身份证保留前后位、大 body 不内嵌只给指纹。`allow_raw=True` 才直出原文（与
AI 净化包的 `--allow-raw` 同一治理姿态：默认收口、显式放行）。

fail-open-empty 而非 fail-closed：抓取面是**只读旁路证据**，缺文件 → `available=False` 空态
（对齐 detail() 里 telemetry_events 缺失即 None 的既有姿态），坏行计数跳过不崩。
"""
from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from autophone.observation.frida import governance

CAPTURE_REL = "planes/api/capture.jsonl"
# 单条 body 内嵌上限：超过只给指纹，避免把整包响应灌进看板 JSON。
MAX_BODY_CHARS = 8192


def _redact_text(text: str) -> str:
    """body 明文脱敏：能解 JSON 就按字段名打码 + 手机号掩码后重序列化，否则整体掩码手机号。"""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return governance.mask_phone(text)
    return json.dumps(governance._redact_json(obj), ensure_ascii=False, separators=(",", ":"))


def _body_view(
    *, text: str | None, b64: str | None, truncated: bool, allow_raw: bool
) -> dict[str, Any] | None:
    """req/resp body → 看板视图：明文（脱敏/原始）、超长截断、二进制只给指纹。"""
    if text:
        shown = text if allow_raw else _redact_text(text)
        over = len(shown) > MAX_BODY_CHARS
        view: dict[str, Any] = {
            "text": shown[:MAX_BODY_CHARS] if over else shown,
            "truncated": bool(truncated or over),
            "chars": len(shown),
        }
        if over and b64:
            view["fingerprint"] = governance.body_fingerprint(b64)
        return view
    if b64:
        fp = governance.body_fingerprint(b64)
        return {"binary": True, "fingerprint": fp} if fp else None
    return None


def _summarize(rec: dict[str, Any], *, allow_raw: bool) -> dict[str, Any]:
    raw_url = rec.get("url")
    return {
        "method": rec.get("method"),
        "url": raw_url if allow_raw else governance.redact_url(raw_url),
        "host": rec.get("host"),
        "status": rec.get("status"),
        "ms": rec.get("ms"),
        "case_id": rec.get("case_id"),
        "step": rec.get("step"),
        "operation": rec.get("operation"),
        "noise": bool(rec.get("noise")),
        "t_req": rec.get("t_req"),
        "t_resp": rec.get("t_resp"),
        "req_headers": rec.get("req_headers") if allow_raw else governance.redact_headers(rec.get("req_headers")),
        "resp_headers": rec.get("resp_headers") if allow_raw else governance.redact_headers(rec.get("resp_headers")),
        "req_body": _body_view(
            text=rec.get("req_body_text"),
            b64=rec.get("req_body_b64"),
            truncated=bool(rec.get("req_body_truncated")),
            allow_raw=allow_raw,
        ),
        "resp_body": _body_view(
            text=rec.get("resp_body_text"),
            b64=rec.get("resp_body_b64"),
            truncated=bool(rec.get("resp_body_truncated")),
            allow_raw=allow_raw,
        ),
        "resp_error": rec.get("resp_error"),
        "scenario": rec.get("scenario") if isinstance(rec.get("scenario"), dict) else None,
    }


# 未归因分桶键：业务调用但无 step 标记 vs 背景噪声（CDN/配置等），分开收，噪声默认折叠。
_UNATTRIBUTED = "__unattributed__"
_NOISE = "__noise__"


def _group_key(call: dict[str, Any]) -> tuple[str, str | None]:
    case_id = call.get("case_id")
    if case_id:
        return (str(case_id), call.get("operation") or call.get("step"))
    return ((_NOISE if call.get("noise") else _UNATTRIBUTED), None)


def _group_label(case_id: str, step: str | None) -> str:
    if case_id == _NOISE:
        return "背景噪声 · 无业务归因"
    if case_id == _UNATTRIBUTED:
        return "未归因 · 业务调用无 step 标记"
    return f"{case_id} · {step}" if step else str(case_id)


def _sort_ts(call: dict[str, Any]) -> float:
    t = call.get("t_req")
    return float(t) if isinstance(t, (int, float)) else float("inf")


def load_capture(run_dir: Path, *, allow_raw: bool = False) -> dict[str, Any]:
    """读 run 的 capture.jsonl → 脱敏 + 按动作分组的看板结构。缺文件返回空态（available=False）。"""
    path = run_dir / CAPTURE_REL
    if not path.exists():
        return {
            "available": False,
            "allow_raw": allow_raw,
            "total": 0,
            "attributed": 0,
            "noise": 0,
            "malformed": 0,
            "groups": [],
        }

    calls: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(rec, dict):
            malformed += 1
            continue
        view = _summarize(rec, allow_raw=allow_raw)
        # idx = 在有效记录中的序号（与 replay_call 重新解析时的计数一致），供前端重放定位。
        view["idx"] = len(calls)
        calls.append(view)

    buckets: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for call in calls:
        buckets.setdefault(_group_key(call), []).append(call)

    # 归因组按首调用时间排序在前，未归因/噪声桶恒沉底。
    def _bucket_sort(item: tuple[tuple[str, str | None], list[dict[str, Any]]]):
        (case_id, _step), members = item
        synthetic = case_id in (_NOISE, _UNATTRIBUTED)
        first = min((_sort_ts(c) for c in members), default=float("inf"))
        return (1 if synthetic else 0, case_id == _NOISE, first)

    groups = []
    for (case_id, step), members in sorted(buckets.items(), key=_bucket_sort):
        members.sort(key=_sort_ts)
        groups.append({
            "case_id": None if case_id in (_NOISE, _UNATTRIBUTED) else case_id,
            "step": step,
            "label": _group_label(case_id, step),
            "synthetic": case_id in (_NOISE, _UNATTRIBUTED),
            "noise": case_id == _NOISE,
            "count": len(members),
            "calls": members,
        })

    return {
        "available": True,
        "allow_raw": allow_raw,
        "total": len(calls),
        "attributed": sum(1 for c in calls if c.get("case_id")),
        "noise": sum(1 for c in calls if c.get("noise")),
        "malformed": malformed,
        "groups": groups,
    }


# ── 重放（replay）─────────────────────────────────────────────────────────────
# 用原始抓取字节（method/url/headers/req_body_b64）在 **host 侧重新发一次请求**，
# 回新响应供人工核对。原始凭据/body 只在 host 侧用、不回传浏览器；返回的响应面默认
# 脱敏（与 load_capture 同一治理姿态，allow_raw 才直出）。重放是**外发动作**（带真凭据
# 打到真后端），由前端显式点击 + 二次确认授权，后端只负责忠实重发并 fail-soft 回错误。

# 重放时不能照搬的逐跳头：长度由 urllib 重算，编码/连接语义不可转发。
_REPLAY_SKIP_HEADERS = {"content-length", "host", "accept-encoding", "connection", "transfer-encoding"}
_REPLAY_MAX_BODY = 1 << 20  # 1 MiB：重放响应体读取上限，超过截断


def _nth_record(path: Path, idx: int) -> dict[str, Any]:
    """重新解析 capture.jsonl，取第 idx 个有效记录（与 load_capture 的计数口径一致）。"""
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if n == idx:
            return rec
        n += 1
    raise IndexError(f"capture record idx={idx} out of range (have {n})")


def _decode_resp_body(raw: bytes, encoding: str | None) -> str | None:
    """响应体字节 → 文本（gzip/deflate 自动解，best-effort utf-8）。解不出返回 None。"""
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 — 解压失败按原始字节再尝试解码
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def replay_call(
    run_dir: Path, idx: int, *, allow_raw: bool = False, timeout: float = 15.0
) -> dict[str, Any]:
    """重放 capture.jsonl 第 idx 条调用：host 侧重发原始请求，回脱敏后的新响应。

    成功/失败都返回 dict（不抛网络异常）：``ok`` 标志重放是否拿到响应，``error`` 给原因。
    ``captured_status`` 是抓取时的状态码，``status`` 是本次重放的，``status_match`` 便于核对。
    定位失败（缺文件 / idx 越界）才抛 ``FileNotFoundError`` / ``IndexError``（fail-closed）。
    """
    path = run_dir / CAPTURE_REL
    if not path.exists():
        raise FileNotFoundError(f"capture not found: {path}")
    rec = _nth_record(path, idx)

    url = rec.get("url")
    method = (rec.get("method") or "GET").upper()
    if not url:
        return {"ok": False, "idx": idx, "error": "原始记录缺 url，无法重放", "method": method}

    body: bytes | None = None
    b64 = rec.get("req_body_b64")
    if b64:
        try:
            body = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            body = None
    elif rec.get("req_body_text"):
        body = str(rec["req_body_text"]).encode("utf-8")

    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (rec.get("req_headers") or {}).items():
        if k.lower() in _REPLAY_SKIP_HEADERS:
            continue
        try:
            req.add_header(k, v)
        except (ValueError, TypeError):
            continue

    captured_status = rec.get("status")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = dict(resp.headers.items())
            raw = resp.read(_REPLAY_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 仍是一次成功的重放，有响应可读
        status = exc.code
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            raw = exc.read(_REPLAY_MAX_BODY + 1)
        except Exception:  # noqa: BLE001
            raw = b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # 网络/连接/超时：重放未达
        ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "idx": idx,
            "method": method,
            "url": url if allow_raw else governance.redact_url(url),
            "captured_status": captured_status,
            "ms": ms,
            "error": f"重放未达后端：{exc.reason if hasattr(exc, 'reason') else exc}",
        }

    ms = int((time.monotonic() - started) * 1000)
    truncated = len(raw) > _REPLAY_MAX_BODY
    text = _decode_resp_body(raw[:_REPLAY_MAX_BODY], resp_headers.get("Content-Encoding"))
    body_view = _body_view(
        text=text,
        b64=base64.b64encode(raw[:_REPLAY_MAX_BODY]).decode("ascii") if text is None else None,
        truncated=truncated,
        allow_raw=allow_raw,
    )
    return {
        "ok": True,
        "idx": idx,
        "method": method,
        "url": url if allow_raw else governance.redact_url(url),
        "captured_status": captured_status,
        "status": status,
        "status_match": (captured_status is not None and status == captured_status),
        "ms": ms,
        "resp_headers": resp_headers if allow_raw else governance.redact_headers(resp_headers),
        "resp_body": body_view,
    }
