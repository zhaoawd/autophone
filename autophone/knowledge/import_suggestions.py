"""Read-only validity and readiness suggestions for Excel import preview (BL-038 B2).

Every suggestion is a deterministic rule with its basis attached; none calls AI and
none is written into a Case.  The readiness Automation Level reuses the importer's
own keyword match, so the preview shows the same value commit writes.  Silence is
reported as "未判定" / "未发现需复核的信号", never as "ready" or "valid".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Iterable

from autophone.assets.case_import import from_template
from autophone.knowledge.family_candidates import case_match_terms, match_terms

SOURCE = "heuristic"
OPERATION_HINT_LIMIT = 3
SHARED_TERM_LIMIT = 12

READINESS_LABELS = {
    "blocked_by_infra": "依赖地基能力",
    "needs_fixture": "需要预置条件",
    "undetermined": "未判定",
}

# §7.1 中本切片不做判断的项；页面原样列出，避免把「未发现信号」读成「已复核」。
VALIDITY_NOT_COVERED = (
    "当前环境是否仍存在该功能",
    "活动、商品、券和权益是否仍有效（日期检查以外）",
    "当前账号是否具备资格",
    "门店、地址、履约方式是否仍可用",
    "原始预期是长期结构规则还是 Campaign-bound Case",
)

# 带四位年份的日期：YYYY-M-D、YYYY/M/D、YYYY年M月D日（月、日一到两位）。
# 前后不能紧贴数字；在 NFKC 规范化后的文本上匹配，所以全角数字与全角斜杠同样命中。
_DATE = re.compile(
    r"(?<!\d)(?P<y>\d{4})(?:"
    r"(?P<sep>[-/])(?P<m>\d{1,2})(?P=sep)(?P<d>\d{1,2})"
    r"|年(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日"
    r")(?!\d)"
)

# 有效性日期检查按此顺序读取的字段；precondition/steps/expect 取 raw 中的单元格原文。
_VALIDITY_FIELDS = (
    ("module",), ("item",), ("test_point",),
    ("raw", "precondition"), ("raw", "steps"), ("raw", "expect"), ("remark",),
)


def normalize_title(text: Any) -> str:
    """NFKC (full/half width), casefold, strip, and collapse whitespace runs to one space."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"\s+", " ", value).strip()


def past_dates(text: str, today: date) -> list[dict[str, str]]:
    """Dates with an explicit year in ``text`` that are strictly before ``today``.

    Impossible calendar dates (``2025-02-30``) and dates without a year are ignored;
    a date equal to ``today`` is not past.  Each hit is reported once.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _DATE.finditer(unicodedata.normalize("NFKC", text or "")):
        month = match.group("m") or match.group("m2")
        day = match.group("d") or match.group("d2")
        try:
            value = date(int(match.group("y")), int(month), int(day))
        except ValueError:
            continue
        if value >= today or match.group(0) in seen:
            continue
        seen.add(match.group(0))
        found.append({"text": match.group(0), "date": value.isoformat()})
    return found


def validity_text(case: dict[str, Any]) -> str:
    raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    return "\n".join(
        str((raw if path[0] == "raw" else case).get(path[-1]) or "") for path in _VALIDITY_FIELDS
    )


def title_index(existing_cases: Iterable[Any]) -> dict[str, list[str]]:
    """Normalized title → IDs of existing non-deprecated Cases, in Registry order."""
    index: dict[str, list[str]] = {}
    for case in existing_cases:
        if not isinstance(case, dict) or case.get("status") == "deprecated":
            continue
        title = normalize_title(case.get("item"))
        if title:
            index.setdefault(title, []).append(str(case.get("id") or ""))
    return index


def validity_suggestion(
    case: dict[str, Any], titles: dict[str, list[str]], *, today: date,
) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    title = normalize_title(case.get("item"))
    duplicates = titles.get(title, []) if title else []
    if duplicates:
        signals.append({
            "code": "possible_duplicate_title",
            "label": "可能重复",
            "basis": "规范化标题与现有 Case 相同：" + "、".join(duplicates),
            "case_ids": list(duplicates),
        })
    dates = past_dates(validity_text(case), today)
    if dates:
        signals.append({
            "code": "past_date",
            "label": "含已过去的日期，活动可能已结束",
            "basis": "命中日期：" + "、".join(d["text"] for d in dates) + f"（早于 {today.isoformat()}）",
            "dates": dates,
        })
    return {
        "source": SOURCE,
        "status": "signals" if signals else "no_signal",
        "summary": f"发现 {len(signals)} 项需复核的信号" if signals else "未发现需复核的信号",
        "signals": signals,
    }


def operation_hints(
    case: dict[str, Any], operations: list[dict[str, Any]] | None, *,
    library_state: str, limit: int = OPERATION_HINT_LIMIT,
) -> dict[str, Any]:
    """Active Operations sharing terms with the Case; a lead, not an execution claim."""
    if operations is None:
        return {"state": "unavailable", "basis": "操作库不可读取，未比对", "total": 0, "limit": limit, "items": []}
    active = [op for op in operations if op.get("status") == "active"]
    if not active:
        basis = "操作库不存在" if library_state == "missing" else "操作库没有 active 操作"
        return {"state": "no_active_operations", "basis": basis, "total": 0, "limit": limit, "items": []}
    terms = case_match_terms(case)
    matched = []
    for op in active:
        shared = sorted(terms & match_terms(" ".join(
            str(op.get(key) or "") for key in ("name", "display_name", "display_summary")
        )))
        if shared:
            matched.append({
                "id": str(op.get("id") or ""),
                "name": str(op.get("name") or ""),
                "display_name": str(op.get("display_name") or ""),
                "shared_terms": shared[:SHARED_TERM_LIMIT],
                "score": len(shared),
            })
    matched.sort(key=lambda row: (-row["score"], row["name"]))
    if not matched:
        return {"state": "no_shared_terms", "basis": "无共享词项", "total": 0, "limit": limit, "items": []}
    return {
        "state": "matched",
        "basis": f"与 {len(matched)} 个 active 操作有共享词项；仅为线索，不代表可由这些操作执行",
        "total": len(matched),
        "limit": limit,
        "items": matched[:limit],
    }


def readiness_suggestion(
    case: dict[str, Any], operations: list[dict[str, Any]] | None, *, library_state: str,
) -> dict[str, Any]:
    """Importer keyword match plus Operation leads for one extracted row.

    ``automation`` equals what commit writes for this row; a mismatch means the
    preview and commit no longer share one judgment and is refused.
    """
    match = from_template.automation_match(case)
    if match["automation"] != (case.get("automation") or ""):
        raise ValueError(
            f"import preview Automation Level diverges from extract for row {case.get('row')}"
        )
    status = str(match["automation"] or "undetermined")
    return {
        "source": SOURCE,
        "status": status,
        "label": READINESS_LABELS[status],
        "automation": str(match["automation"]),
        "keyword": match["keyword"],
        "basis": str(match["reason"]) if match["keyword"] else "未命中导入器关键词",
        "operation_hints": operation_hints(case, operations, library_state=library_state),
    }


def suggestion_rows(
    new_rows: list[dict[str, Any]],
    existing_cases: Iterable[Any],
    operations: list[dict[str, Any]] | None,
    *, library_state: str, today: date,
) -> list[dict[str, Any]]:
    titles = title_index(existing_cases)
    return [
        {
            "row": case["row"],
            "external_id": (case.get("raw") or {}).get("external_id", ""),
            "item": case.get("item", ""),
            "readiness_suggestion": readiness_suggestion(case, operations, library_state=library_state),
            "validity_suggestion": validity_suggestion(case, titles, today=today),
        }
        for case in new_rows
    ]
