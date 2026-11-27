"""统一日志：终端、平台事件归档、run 证据与后台 worker 回传。

调用方只有两个入口：``setup_logging()`` 配置当前进程，``bind_run()`` 绑定
关联上下文。Web 触发的 spawn worker 通过 ``background_log_queue()`` 把记录回传
给 Web 父进程；父进程是唯一写入 run 日志和平台全局事件日志的一方。
"""
from __future__ import annotations

import copy
import json
import logging
import multiprocessing
import os
import re
import sys
import traceback
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

_OWN_LOGGERS = ("autophone", "tools")
from autophone.paths import STATE_ROOT

_DEFAULT_LOG_ROOT = STATE_ROOT / "logs" / "platform"
_MAX_OPEN_RUNS = 32

_run_id: ContextVar[str] = ContextVar("run_id", default="")
_run_dir: ContextVar[str] = ContextVar("run_dir", default="")
_case_id: ContextVar[str] = ContextVar("case_id", default="")
_step: ContextVar[str] = ContextVar("step", default="")
_stage: ContextVar[str] = ContextVar("log_stage", default="")

# Keep credentials and direct identifiers out of every sink. Raw HTTP evidence has
# its own explicitly-governed capture artifact; ordinary logs are never that sink.
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
# 先匹配完整引号值（含转义字符），保留 JSON/Python 字典的结构。
_QUOTED_SECRET = r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'"
_AUTH_RE = re.compile(
    r'''(?i)((?<!\w)["']?(?:authorization|cookie)["']?[ \t]*[:=][ \t]*)'''
    + "(" + _QUOTED_SECRET + r'''|[^\r\n]+)'''
)
_TOKEN_RE = re.compile(
    r'''(?i)((?<!\w)["']?(?:access_?token|refresh_?token|token|password|passwd|secret)["']?[ \t]*[:=][ \t]*)'''
    + "(" + _QUOTED_SECRET + r'''|[^\s,;"'}\]]+)'''
)
_CREDENTIAL_KEY = re.compile(
    r"(?i)(authorization|cookie|access_?token|refresh_?token|token|password|passwd|secret)"
)
# 身份证号：与 observation/frida/governance._IDCARD 同一掩码体例（保留前 6 后 4），
# 另要求出生日期段合法，避免把任意 18 位数字当作证件号。
_IDCARD_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{6})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"(\d{3}[\dXx])(?![0-9A-Za-z])"
)
# 订单号（BL-235，2026-10-10）。带标签时只替换号串、保留标签；号串 8～64 位且含数字。
_ORDER_LABEL = (
    r"订单编号|订单号|订单[ \t]*ID|流水号|交易号|单号"
    r"|(?<![A-Za-z])order[ \t_-]*(?:no|id|num(?:ber)?)(?![A-Za-z])"
)
_ORDER_VALUE = r"(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{7,63}(?![A-Za-z0-9-])"
_ORDER_LABELED_RE = re.compile(
    r"(?i)((?:" + _ORDER_LABEL + r")[\"']?[ \t]*[:：=#]?[ \t]*[\"']?)(" + _ORDER_VALUE + ")"
)
# 无标签：独立的 16～24 位纯数字串。前后紧邻字母、数字或 `-_.:/` 时不算独立（构建 id、
# 路径、小数、版本号、哈希片段），因此不命中 sha256、bld-、run_id、日期、端口与金额。
_ORDER_BARE_RE = re.compile(r"(?<![0-9A-Za-z\-_.:/])\d{16,24}(?![0-9A-Za-z\-_.:/])")
# JSON 键名形式的订单号字段（orderNo、order_id、shop_order_id 等）。
_ORDER_KEY = re.compile(r"(?i)(?:[a-z]+_)?order[_-]?(?:no|id|num(?:ber)?)|订单编号|订单号|流水号|交易号")
_ORDER_KEY_VALUE = re.compile(r"(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{7,63}")
# 中文地址（BL-235，2026-10-10）：至少两级才命中，从该段汉字起点到地址尾部整段替换。
# (1) 区县/乡镇或道路级单位 + 带阿拉伯数字的门牌、楼栋、单元、楼层或室；
# (2) 楼宇或小区名 + 连续两个带数字的楼栋、单元、楼层或室。
# 单独的门店名（「XX路餐厅」）、城市名与「门店地址」等标签文字没有数字门牌，不命中。
_CJK = "一-鿿"
_ADDR_NUM = r"\d{1,5}[A-Za-z]?(?:号楼|号院|号|栋|幢|座|单元|室|楼|层)"
_ADDRESS_RE = re.compile(
    "(?<![" + _CJK + "])(?:"
    + "[" + _CJK + "]{0,12}?(?:区|县|旗|镇|乡|街道|大道|大街|路|街|巷|弄|胡同)"
    + "[" + _CJK + r"A-Za-z0-9\-－]{0,30}?" + _ADDR_NUM
    + "|[" + _CJK + "]{1,12}?(?:小区|大厦|花园|公寓|广场|大楼|新村|苑|园|府|城|中心)"
    + _ADDR_NUM + r"[\-－]?" + _ADDR_NUM
    + ")[" + _CJK + r"A-Za-z0-9\-－#()（）]*"
)


def _mask_identifiers(text: str) -> str:
    """直接标识符：手机号、身份证号、订单号、地址。凭据类由调用方另行处理。"""
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _IDCARD_RE.sub(r"\1********\2", text)
    text = _ORDER_LABELED_RE.sub(r"\1[ORDER_ID]", text)
    text = _ORDER_BARE_RE.sub("[ORDER_ID]", text)
    return _ADDRESS_RE.sub("[ADDRESS]", text)


def _redact_credential(match: re.Match[str]) -> str:
    value = match.group(2)
    quote = value[0] if value.startswith(('"', "'")) else ""
    return match.group(1) + quote + "<redacted>" + quote


def _redact_order_field(item: Any) -> Any:
    """订单号键下的字符串或整数值：满足号串条件时返回 [ORDER_ID]，否则返回 None。"""
    if isinstance(item, bool) or not isinstance(item, (str, int)):
        return None
    return "[ORDER_ID]" if _ORDER_KEY_VALUE.fullmatch(str(item).strip()) else None


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _CREDENTIAL_KEY.fullmatch(key):
                redacted: Any = "<redacted>"
            elif _ORDER_KEY.fullmatch(key) and _redact_order_field(item) is not None:
                redacted = "[ORDER_ID]"
            else:
                redacted = _redact_json_value(item)
            out[_mask_identifiers(key)] = redacted
        return out
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = str(value)
        masked = _mask_identifiers(rendered)
        return masked if masked != rendered else value
    return value


def redact_text(value: Any) -> str:
    """Return a safe textual representation for terminal and durable logs."""
    text = str(value)
    # JSON 日志的 message 可能再次编码 JSON；先解码字符串值，避免转义引号隐藏凭据键。
    if text.lstrip().startswith(("{", "[")):
        try:
            document = json.loads(text)
        except ValueError:
            pass
        else:
            redacted = _redact_json_value(document)
            return text if redacted == document else json.dumps(redacted, ensure_ascii=False)
    lines = text.splitlines(keepends=True)
    if len(lines) > 1:
        return "".join(
            redact_text(line.rstrip("\r\n")) + line[len(line.rstrip("\r\n")):]
            for line in lines
        )
    text = _mask_identifiers(text)
    text = _AUTH_RE.sub(_redact_credential, text)
    return _TOKEN_RE.sub(_redact_credential, text)


# ── OCR 专用脱敏（BL-235，2026-10-10）────────────────────────────────────────
# 只用于 OCR 文本进入 AI 或离线投影之前，不并入 `redact_text`：8～12 位数字的规则会遮住
# 日志中合法的 YYYYMMDD、时间戳与计数。采样见 evidence/outputs/bl235-ablation-stage-a-20261010 §8。
#
# 易混字母按 OCR 把数字读成字母的方向归一化。A/a→4 来自样本（手机号中的 4 被读成 A/a）；
# 其余按字形取舍。一个号串最多含 2 个易混字母，含其他字母（十六进制串的 c/d/e/f 等）不算号串。
_OCR_CONFUSABLE_MAP = {
    "O": "0", "o": "0", "D": "0",
    "I": "1", "l": "1", "i": "1", "L": "1",
    "Z": "2", "z": "2",
    "A": "4", "a": "4",
    "S": "5", "s": "5",
    "b": "6", "G": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
}
#: 按易混字母处理的字母集合（离线统计据此区分「易混字母」与其他字母）。
OCR_CONFUSABLE_LETTERS = frozenset(_OCR_CONFUSABLE_MAP)
_OCR_CONFUSABLE = str.maketrans(_OCR_CONFUSABLE_MAP)
_OCR_MAX_LETTERS = 2
# 号串：前后不紧邻 ASCII 字母或数字、长 6～12 的字母数字段（汉字、标点、空白都可作边界）。
# 长 6 的号串只按「已知手机号的连续子串」处理（2026-10-10 r4 起），其余规则仍从 7 或 8 位起。
_OCR_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z]{6,12}(?![0-9A-Za-z])")
_OCR_SHORT_TOKEN = 6
_OCR_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_OCR_PHONE_HEAD_RE = re.compile(r"1[3-9]")
_OCR_FULL_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 日期形态（YYYYMMDD 开头）不按片段规则命中，例如「指标统计时间：YYYYMMDD/-YYYYMMDD」。
_OCR_DATE_RE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
# 长 6 的号串若为 YYYYMM（19xx、20xx 年且月份合法）形态，不按片段规则命中，与 YYYYMMDD 的处理一致。
_OCR_YEAR_MONTH_RE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])")
# 10 位秒级 Unix 时间戳区间（约 2017～2036 年）；只排除无上下文的前缀规则。
_OCR_EPOCH_SECONDS = (1_500_000_000, 2_100_000_000)
# 紧邻 `.:/_-` 的号串属于小数、版本号、路径或带分隔的编号，不按前缀与上下文规则命中。
_OCR_JOINERS = ".:/_-"
# 电话上下文：关键词、打码手机号（`138****5678` 及 OCR 变体）。
_OCR_PHONE_CONTEXT_RE = re.compile(
    r"(?i)电话|手机|联系|拨打|号码|致电|\btel\b|phone|mobile|(?<!\d)1[3-9]\d[ \t]?[*＊×xX·•]{2,}"
)
# App 自身打码手机号被 OCR 读成的变体：星号换成全角、×、x、中点，数量 2～6 个，尾部数字 1～4 位，
# 尾部多读的 1 个字符一并吸收。统一为 `前 3 位****尾部数字`。
_OCR_MASK_VARIANT_RE = re.compile(
    r"(?<![0-9A-Za-z])(1[3-9]\d)[ \t]?[*＊×xX·•]{2,6}[ \t]?(\d{1,4})[0-9A-Za-z]?(?![0-9A-Za-z])"
)
# 虚拟号分机：打码手机号后的 `,NNNN#`、`，NNNN`、`转NNNN`。
_OCR_EXT_RE = re.compile(r"((?<!\d)1[3-9]\d\*{4}\d{4})[ \t]*([,，转])[ \t]*\d{3,5}#?")
# 号串与帧内已知手机号共有的最短连续数字长度。
_OCR_SHARED_DIGITS = 5


def _ocr_digits(token: str) -> str | None:
    """易混字母归一化后的数字串；字母超过上限或含非易混字母时返回 None。"""
    letters = sum(1 for ch in token if not ch.isdigit())
    if letters > _OCR_MAX_LETTERS:
        return None
    digits = token.translate(_OCR_CONFUSABLE)
    return digits if digits.isascii() and digits.isdigit() else None


def _ocr_known_phones(texts: Iterable[str]) -> frozenset[str]:
    """帧内可读出的完整手机号：11 位原文号码与归一化后为 11 位手机号的号串。"""
    found: set[str] = set()
    for text in texts:
        found.update(_OCR_FULL_PHONE_RE.findall(text))
        for match in _OCR_TOKEN_RE.finditer(text):
            digits = _ocr_digits(match.group(0))
            if digits and len(digits) == 11 and _OCR_PHONE_RE.fullmatch(digits):
                found.add(digits)
    return frozenset(found)


def ocr_known_phones(texts: Iterable[Any]) -> frozenset[str]:
    """一组 OCR 文本中可读出的完整手机号（11 位原文号码与易混字母归一化后为 11 位手机号的号串）。

    离线工具据此汇总同一 App 多帧中的号码，作为 :func:`redact_ocr_texts` 的 ``known_numbers``。
    返回值是原始号码，调用方不得写入持久日志或入库文件。
    """
    return _ocr_known_phones([str(t) for t in texts])


def _ocr_normalize_known(numbers: Iterable[Any]) -> frozenset[str]:
    """``known_numbers`` 归一化：去空白与 `-`，易混字母转数字，结果必须是 11 位 `1[3-9]\\d{9}`。"""
    out: set[str] = set()
    for item in numbers:
        token = re.sub(r"[\s-]", "", str(item))
        digits = _ocr_digits(token) if token else None
        if digits is None or not _OCR_PHONE_RE.fullmatch(digits):
            raise ValueError("known_numbers entries must be 11-digit mobile numbers 1[3-9]NNNNNNNNN")
        out.add(digits)
    return frozenset(out)


def _ocr_has_phone_context(texts: list[str]) -> bool:
    return any(_OCR_PHONE_CONTEXT_RE.search(t) or _ocr_known_phones([t]) for t in texts)


def _ocr_informative(window: str) -> bool:
    # 同一数字的连续串（如 `00000`）不作为同一号码的证据：`199` 加 8 个 0 这类登录框号码与金额、错误码
    # 中的连续 0 重合，不能说明是同一号码。
    return len(set(window)) > 1


def _ocr_shares_phone_digits(digits: str, known: frozenset[str]) -> bool:
    n = _OCR_SHARED_DIGITS
    return any(_ocr_informative(window) and window in phone
               for phone in known
               for window in (digits[i:i + n] for i in range(len(digits) - n + 1)))


def _ocr_is_phone_substring(digits: str, known: frozenset[str]) -> bool:
    return _ocr_informative(digits) and any(digits in phone for phone in known)


#: OCR 专用规则的命中类别（`on_hit` 回调的第一个参数）。
OCR_RULES = ("mask_variant", "phone_confusable", "fragment_prefix", "fragment_known_phone",
             "fragment_context", "extension")


def _redact_ocr_one(raw: str, known: frozenset[str], phone_context: bool,
                    on_hit: Callable[[str, str], None] | None) -> str:
    def _hit(rule: str, original: str) -> None:
        if on_hit is not None:
            on_hit(rule, original)

    def _variant(match: re.Match[str]) -> str:
        masked = f"{match.group(1)}****{match.group(2)}"
        if masked != match.group(0):
            _hit("mask_variant", match.group(0))
        return masked

    def _fragment(source: str, match: re.Match[str]) -> str:
        token = match.group(0)
        digits = _ocr_digits(token)
        if digits is None:
            return token
        # (1) 易混字母归一化后是完整手机号：沿用 `前 3 位****后 4 位` 体例。
        if len(digits) == 11 and _OCR_PHONE_RE.fullmatch(digits):
            _hit("phone_confusable", token)
            return f"{digits[:3]}****{digits[-4:]}"
        if _OCR_DATE_RE.match(digits):
            return token
        # (2b') 长 6 的号串：只在是已知手机号（帧内或 known_numbers）的连续子串时命中；YYYYMM 形态除外。
        if len(digits) == _OCR_SHORT_TOKEN:
            if known and not _OCR_YEAR_MONTH_RE.fullmatch(digits) and _ocr_is_phone_substring(digits, known):
                _hit("fragment_known_phone", token)
                return "[PHONE]"
            return token
        before = source[match.start() - 1:match.start()]
        after = source[match.end():match.end() + 1]
        joined = bool(before and before in _OCR_JOINERS) or bool(after and after in _OCR_JOINERS)
        # (2a) 8～12 位、以 1[3-9] 开头的号串；无上下文的 10 位秒级时间戳区间除外。
        if 8 <= len(digits) <= 12 and _OCR_PHONE_HEAD_RE.match(digits) and not joined:
            epoch = len(digits) == 10 and digits == token \
                and _OCR_EPOCH_SECONDS[0] <= int(digits) < _OCR_EPOCH_SECONDS[1]
            if not epoch:
                _hit("fragment_prefix", token)
                return "[PHONE]"
        # (2b) 与已知手机号（帧内或 known_numbers）共有 5 位以上连续数字（OCR 截断、错读的同一号码）；
        # 共有部分全为同一数字时不算。
        if known and _ocr_shares_phone_digits(digits, known):
            _hit("fragment_known_phone", token)
            return "[PHONE]"
        # (2c) 8～10 位号串，本文本框或相邻文本框有电话上下文。
        if 8 <= len(digits) <= 10 and phone_context and not joined:
            _hit("fragment_context", token)
            return "[PHONE]"
        return token

    def _extension(match: re.Match[str]) -> str:
        _hit("extension", match.group(0))
        return f"{match.group(1)}{match.group(2)}[EXT]"

    text = _OCR_MASK_VARIANT_RE.sub(_variant, redact_text(raw))
    text = _OCR_TOKEN_RE.sub(lambda m, src=text: _fragment(src, m), text)
    return _OCR_EXT_RE.sub(_extension, text)


def redact_ocr_text(value: Any, *, context: Iterable[Any] = (), known_numbers: Iterable[Any] = (),
                    on_hit: Callable[[str, str], None] | None = None) -> str:
    """OCR 文本的脱敏：先执行 `redact_text` 的全部规则，再遮蔽 OCR 错读的手机号、片段与分机。

    ``context`` 是同一 OCR 帧中与本文本相邻的文本框原文，用于判断电话上下文与帧内已知手机号。
    ``known_numbers`` 是调用方已确认的完整手机号（见 :func:`redact_ocr_texts`）。
    整帧脱敏使用 :func:`redact_ocr_texts`。``on_hit(rule, original)`` 在每次 OCR 专用规则命中时调用，
    ``rule`` 取自 :data:`OCR_RULES`；回调只供统计，调用方不得把 ``original`` 写入持久日志。
    """
    raw = str(value)
    nearby = [str(t) for t in context]
    known = _ocr_known_phones([raw, *nearby]) | _ocr_normalize_known(known_numbers)
    return _redact_ocr_one(raw, known, _ocr_has_phone_context([raw, *nearby]), on_hit)


def redact_ocr_texts(texts: Iterable[Any], *, window: int = 1, known_numbers: Iterable[Any] = (),
                     on_hit: Callable[[str, str], None] | None = None) -> list[str]:
    """一帧 OCR 文本框按顺序脱敏：已知手机号取自整帧，电话上下文取前后 ``window`` 个文本框。

    ``known_numbers`` 是调用方已确认的完整手机号（归一化后为 11 位 `1[3-9]\\d{9}`，否则抛 ValueError），
    与帧内号码合并作为「已知手机号」：7～12 位号串与其共有 5 位以上连续数字、6 位号串是其连续子串时
    替换为 `[PHONE]`。离线工具传入同一 App 多帧汇总的号码；生产调用方没有号码集合来源，不传。
    """
    raw = [str(t) for t in texts]
    known = _ocr_known_phones(raw) | _ocr_normalize_known(known_numbers)
    flags = [_ocr_has_phone_context([t]) for t in raw]
    return [
        _redact_ocr_one(text, known, any(flags[max(0, i - window):i + window + 1]), on_hit)
        for i, text in enumerate(raw)
    ]


class ContextFilter(logging.Filter):
    """Attach portable correlation attributes, preserving worker-provided values."""

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = getattr(record, "run_id", "") or _run_id.get()
        run_dir = getattr(record, "run_dir", "") or _run_dir.get()
        case_id = getattr(record, "case_id", "") or _case_id.get()
        step = getattr(record, "step", "") or _step.get()
        stage = getattr(record, "stage", "") or _stage.get()
        record.run_id = run_id
        record.run_dir = run_dir
        record.case_id = case_id
        record.step = step
        record.stage = stage
        parts = []
        if run_id:
            parts.append(f"run={run_id}")
        if case_id:
            parts.append(f"case={case_id}")
        if step:
            parts.append(f"step={step}")
        record.ctx = (" [" + " ".join(parts) + "]") if parts else ""
        return True


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return (getattr(record, "run_id", "") or _run_id.get()) == self._run_id


class _UvicornNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


class AccessLogPathFilter(logging.Filter):
    """Keep HTTP route paths and status codes without logging query values."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access record is (client, method, target, version, status).
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True


class ScreenStreamFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/screen.jpg" not in msg and "/screen.mjpeg" not in msg


_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s%(ctx)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _make_formatter() -> logging.Formatter:
    return _RedactingFormatter(_FMT, datefmt=_DATEFMT)


def _event_from_record(record: logging.LogRecord) -> dict[str, Any]:
    exc = getattr(record, "exc_text", "")
    if not exc and record.exc_info:
        exc = "".join(traceback.format_exception(*record.exc_info))
    return {
        "schema_version": "v1",
        "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "logger": record.name,
        "message": redact_text(record.getMessage()),
        "run_id": getattr(record, "run_id", "") or None,
        "case_id": getattr(record, "case_id", "") or None,
        "step": getattr(record, "step", "") or None,
        "stage": getattr(record, "stage", "") or None,
        "process": record.process,
        "thread": record.threadName,
        "exception": redact_text(exc) if exc else None,
    }


class _JsonlHandler(logging.Handler):
    """Small append-only JSONL adapter; the parent process is its sole writer."""

    def __init__(self, path: Path) -> None:
        super().__init__(logging.DEBUG)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(_event_from_record(record), ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            self.handleError(record)


class _PlatformJsonlHandler(logging.Handler):
    """Daily global journal. It retains platform milestones/errors, not action DEBUG noise."""

    def __init__(self, root: Path) -> None:
        super().__init__(logging.INFO)
        self.root = root
        self._day = ""
        self._handler: _JsonlHandler | None = None
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            days = max(1, int(os.environ.get("AUTOPHONE_LOG_RETENTION_DAYS", "14")))
        except ValueError:
            days = 14
        cutoff = datetime.now().astimezone() - timedelta(days=days)
        for path in self.root.glob("service-*.jsonl") if self.root.exists() else []:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime).astimezone() < cutoff:
                    path.unlink()
            except OSError:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._handler = _JsonlHandler(self.root / f"service-{day}.jsonl")
        assert self._handler is not None
        self._handler.emit(record)


class _RunFileRouter(logging.Handler):
    """Route a correlated record to its run's human and JSONL journals.

    The route is chosen from the record rather than process-local state, allowing a
    spawned worker to be relayed safely by its Web parent.
    """

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self._sinks: OrderedDict[str, tuple[logging.FileHandler, _JsonlHandler]] = OrderedDict()

    def _sink(self, run_dir: str) -> tuple[logging.FileHandler, _JsonlHandler]:
        key = str(Path(run_dir))
        existing = self._sinks.pop(key, None)
        if existing is not None:
            self._sinks[key] = existing
            return existing
        path = Path(key)
        path.mkdir(parents=True, exist_ok=True)
        text = logging.FileHandler(path / "run.log", encoding="utf-8")
        text.setLevel(logging.DEBUG)
        text.setFormatter(_make_formatter())
        sink = (text, _JsonlHandler(path / "events.jsonl"))
        self._sinks[key] = sink
        while len(self._sinks) > _MAX_OPEN_RUNS:
            _, old = self._sinks.popitem(last=False)
            old[0].close()
        return sink

    def emit(self, record: logging.LogRecord) -> None:
        run_dir = getattr(record, "run_dir", "")
        if not run_dir:
            return
        try:
            text, events = self._sink(str(run_dir))
            text.emit(record)
            events.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for text, _events in self._sinks.values():
            text.close()
        self._sinks.clear()
        super().close()


class _WorkerQueueHandler(QueueHandler):
    """Pickle-safe worker adapter that retains a redacted traceback."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = copy.copy(record)
        ContextFilter().filter(record)
        record.msg = redact_text(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
        record.exc_info = None
        return record


class _RelayHandler(logging.Handler):
    """Re-emit a worker record through the parent process's configured sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger(record.name).handle(record)


_configured = False
_background_queue: Any | None = None
_background_listener: QueueListener | None = None


def _remove_autophone_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if any(getattr(handler, flag, False) for flag in (
            "_autophone_console", "_autophone_global", "_autophone_run_router", "_autophone_worker_queue",
        )):
            root.removeHandler(handler)
            handler.close()


def setup_logging(level: int = logging.INFO, *, force: bool = False) -> None:
    """Configure direct sinks for a normal (CLI/Web parent) process.

    It keeps stderr behaviour intact, adds a daily platform JSONL journal, and
    installs the run router used by both local and relayed worker records.
    """
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return
    _remove_autophone_handlers(root)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_make_formatter())
    console.addFilter(ContextFilter())
    console._autophone_console = True  # type: ignore[attr-defined]
    root.addHandler(console)

    log_root = Path(os.environ.get("AUTOPHONE_LOG_ROOT", str(_DEFAULT_LOG_ROOT)))
    global_log = _PlatformJsonlHandler(log_root)
    global_log.addFilter(ContextFilter())
    global_log._autophone_global = True  # type: ignore[attr-defined]
    root.addHandler(global_log)

    run_router = _RunFileRouter()
    run_router.addFilter(ContextFilter())
    run_router._autophone_run_router = True  # type: ignore[attr-defined]
    root.addHandler(run_router)

    root.setLevel(level)
    for name in _OWN_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.addFilter(AccessLogPathFilter())
    access_logger.addFilter(ScreenStreamFilter())
    logging.getLogger("uvicorn.error").addFilter(_UvicornNameFilter())
    _configured = True


def background_log_queue() -> Any | None:
    """Return the parent-owned queue for spawned workers, starting one relay once."""
    global _background_queue, _background_listener
    if _background_queue is None:
        context = multiprocessing.get_context("spawn")
        # Some hermetic process fakes intentionally model only ``Process``. They
        # exercise launch wiring, not IPC; preserve that seam without weakening
        # the real spawn path (whose context always exposes Queue).
        if not callable(getattr(context, "JoinableQueue", None)):
            return None
        # The parent watcher can wait for this queue to drain before it refreshes
        # an artifact manifest. A plain Queue would make run.log/events.jsonl
        # race the worker's manifest write.
        _background_queue = context.JoinableQueue()
        _background_listener = QueueListener(_background_queue, _RelayHandler())
        _background_listener.start()
    return _background_queue


def flush_background_logs() -> None:
    """Wait until records already sent by spawned workers reach parent sinks."""
    queue = _background_queue
    join = getattr(queue, "join", None) if queue is not None else None
    if callable(join):
        join()


def configure_worker_logging(queue: Any, level: int = logging.INFO) -> None:
    """Replace child-local sinks with a queue back to the Web parent."""
    root = logging.getLogger()
    _remove_autophone_handlers(root)
    handler = _WorkerQueueHandler(queue)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(ContextFilter())
    handler._autophone_worker_queue = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    for name in _OWN_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)


def has_run_router() -> bool:
    return any(getattr(h, "_autophone_run_router", False) for h in logging.getLogger().handlers)


def is_worker_logging() -> bool:
    return any(getattr(h, "_autophone_worker_queue", False) for h in logging.getLogger().handlers)


def verbosity_to_level(verbose: int) -> int:
    return logging.DEBUG if verbose >= 1 else logging.INFO


_LEVEL_ENV = "AUTOPHONE_LOG_LEVEL"


def level_from_env(default: int = logging.INFO) -> int:
    raw = os.environ.get(_LEVEL_ENV)
    if not raw:
        return default
    raw = raw.strip()
    return int(raw) if raw.isdigit() else getattr(logging, raw.upper(), default)


def export_level_to_env(level: int) -> None:
    os.environ[_LEVEL_ENV] = logging.getLevelName(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def current_run_id() -> str:
    """当前上下文绑定的 run_id（`bind_run` 设置）；未绑定时为空串。"""
    return _run_id.get()


@contextmanager
def bind_run(run_id: str, *, run_dir: Path | str | None = None, stage: str = "") -> Iterator[None]:
    tokens = [_run_id.set(run_id)]
    if run_dir is not None:
        tokens.append(_run_dir.set(str(run_dir)))
    if stage:
        tokens.append(_stage.set(stage))
    try:
        yield
    finally:
        if stage:
            _stage.reset(tokens.pop())
        if run_dir is not None:
            _run_dir.reset(tokens.pop())
        _run_id.reset(tokens.pop())


@contextmanager
def bind_case(case_id: str) -> Iterator[None]:
    token = _case_id.set(case_id)
    try:
        yield
    finally:
        _case_id.reset(token)


@contextmanager
def bind_step(step: str) -> Iterator[None]:
    token = _step.set(step)
    try:
        yield
    finally:
        _step.reset(token)


@contextmanager
def run_logfile(path: Path, level: int = logging.DEBUG) -> Iterator[None]:
    """Compatibility adapter for standalone tools that explicitly own a log file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_make_formatter())
    handler.addFilter(ContextFilter())
    current_run = _run_id.get()
    if current_run:
        handler.addFilter(_RunIdFilter(current_run))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
