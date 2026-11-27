"""from_template.py — 把「通用用例模板」xlsx 转成结构化 v2 YAML 用例集。

与 `from_excel.py` 的区别：那个绑定了既有回归专用表（合并单元格 + 整行分组标题 +
A–P 固定列序）；本转换器吃的是 **app-agnostic 的通用模板**
（随包模板见 `autophone/_data/test-cases-template.xlsx`），列由表头文字驱动，不假设列在第几列：

    模块 / 用例编号 / 用例名称 / 用例级别 / 前置条件 / 测试步骤 /
    预期结果 / 用例类型 / 测试点 / 责任人 / 备注

字段映射（→ v2 case 字段）：
    模块→module 用例名称→item 用例级别→level 前置条件→precondition[]
    测试步骤→steps[] 预期结果→expects[] 用例类型→case_type 测试点→test_point
    责任人→owner 备注→remark。「用例编号」是录入方自己的编号，平台不拿它当主键
    （平台主键恒为自分配的 R{next_seq}），原样存进 raw.external_id 供追溯/去重。

真源归属（与 from_excel 一致）：模板仅作录入入口。--out 不存在 → seed（全新 v2 全集，
每条 draft）；已存在 → merge（按 external_id 去重，仅追加新行为 status=draft，现存
用例一字不动；模板缺行不触发废弃）。无 external_id 的行无法去重，一律追加。

automation 分类沿用 from_excel 的启发式：命中明确关键词时标记
blocked/fixture；未命中时留空，不推断为 ui_ready。所有初值均需人工复核。

用法：
    python3 -m autophone.assets.case_import.from_template \\
        --xlsx autophone/_data/test-cases-template.xlsx \\
        --out  apps/<app>/cases/cases.yaml [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openpyxl
import yaml

from autophone.assets import cases as cases_store
from autophone.assets import store as asset_store
from autophone.assets.policy import assert_assets_writable
from autophone.assets.case_ids import (
    allocate_case_ids,
    import_dedup_external_ids,
)
from autophone.assets.case_lint import validate_case_registry_or_raise
from autophone.assets.case_import.import_helpers import (
    _norm_cell,
    _patch_meta_scalar,
    classify_automation_match,
    derive_import_semantics,
    split_numbered,
)

# 表头文字 → 语义字段名。表头匹配按「去空白后精确相等」，列在第几列无所谓。
HEADER_FIELD = {
    "模块": "module",
    "用例编号": "external_id",
    "用例名称": "item",
    "用例级别": "level",
    "前置条件": "precondition",
    "测试步骤": "steps",
    "预期结果": "expects",
    "用例类型": "case_type",
    "测试点": "test_point",
    "责任人": "owner",
    "备注": "remark",
}

# 表头行至少要命中这么多个已知表头，才认定它是表头行（容忍模板有装饰/空白行在前）。
MIN_HEADER_HITS = 4
HEADER_SCAN_ROWS = 8  # 只在前若干行里找表头


def _find_header(ws) -> tuple[int, dict[str, int]]:
    """扫前 HEADER_SCAN_ROWS 行找表头行，返回 (表头行号, {字段名: 列号(1-based)})。

    找不到（命中数不够）直接 fail-loud——喂错文件别静默产出空集。"""
    for r in range(1, min(HEADER_SCAN_ROWS, ws.max_row) + 1):
        col_of: dict[str, int] = {}
        for cidx in range(1, ws.max_column + 1):
            text = _norm_cell(ws.cell(row=r, column=cidx).value)
            field = HEADER_FIELD.get(text)
            if field and field not in col_of:
                col_of[field] = cidx
        if len(col_of) >= MIN_HEADER_HITS:
            return r, col_of
    raise SystemExit(
        f"未识别到用例模板表头（前 {HEADER_SCAN_ROWS} 行里命中 < {MIN_HEADER_HITS} 个已知列）；"
        f"期望列名：{'、'.join(HEADER_FIELD)}"
    )


def infer_login_precondition(module: str, item: str, steps: str) -> str:
    """启发式推断 login_precondition：登录/注销类用例需要 logged_out，其他默认 required。"""
    searchable = f"{module} {item} {steps}".lower()
    # 登录、注销、退出登录等场景需要未登录状态
    if any(kw in searchable for kw in ("登录", "注销", "退出", "login", "logout", "sign in", "sign out")):
        # 排除「已登录」「保持登录」等需要登录态的场景
        if any(kw in searchable for kw in ("已登录", "保持登录", "登录状态", "logged in", "stay logged")):
            return "required"
        return "logged_out"
    return "required"


# Automation Level 关键词判定读取的模板字段（precondition/steps 为单元格原文）。
AUTOMATION_TEXT_FIELDS = ("module", "item", "test_point", "precondition", "steps")


def automation_searchable_text(row_vals: dict[str, str]) -> str:
    """Join the template fields the Automation Level keyword check reads."""
    return " ".join(row_vals.get(k, "") for k in AUTOMATION_TEXT_FIELDS)


def automation_match(case: dict[str, Any]) -> dict[str, str | None]:
    """Recompute the importer's Automation Level match for one extracted case dict.

    ``extract`` 写入 ``automation`` / ``automation_reason`` 时用同一段文本和同一个
    ``classify_automation_match``；precondition/steps 取 ``raw`` 中的单元格原文，
    与提取时的 ``row_vals`` 相同。导入预览用它取命中的关键词，结果不写入 Case。
    """
    raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    return classify_automation_match(automation_searchable_text({
        "module": str(case.get("module") or ""),
        "item": str(case.get("item") or ""),
        "test_point": str(case.get("test_point") or ""),
        "precondition": str(raw.get("precondition") or ""),
        "steps": str(raw.get("steps") or ""),
    }))


def _row_case(row_vals: dict[str, str], row_no: int) -> dict[str, Any]:
    """把一行模板字段拼成 v2 case 业务 dict（不含 id/status/revision，由落库层加）。"""
    match = classify_automation_match(automation_searchable_text(row_vals))
    automation, reason = str(match["automation"]), str(match["reason"])
    login_precondition = infer_login_precondition(
        row_vals.get("module", ""), row_vals.get("item", ""), row_vals.get("steps", "")
    )
    precondition = split_numbered(row_vals.get("precondition", ""))
    steps = split_numbered(row_vals.get("steps", ""))
    expects = split_numbered(row_vals.get("expects", ""))
    return {
        "row": row_no,
        "module": row_vals.get("module", ""),
        "item": row_vals.get("item", ""),
        "order_type": None,
        "level": row_vals.get("level", ""),
        "automation": automation,
        "automation_reason": reason,
        "login_precondition": login_precondition,
        "desc": "",
        "precondition": precondition,
        "steps": steps,
        "expects": expects,
        "import_semantics": derive_import_semantics(
            item=row_vals.get("item", ""),
            precondition=precondition,
            steps=steps,
            expects=expects,
        ),
        "suitable": {"api": "", "ui": ""},
        "case_type": row_vals.get("case_type", ""),
        "test_point": row_vals.get("test_point", ""),
        "owner": row_vals.get("owner", ""),
        "remark": row_vals.get("remark", ""),
        "raw": {
            "external_id": row_vals.get("external_id", ""),
            "precondition": row_vals.get("precondition", ""),
            "steps": row_vals.get("steps", ""),
            "expect": row_vals.get("expects", ""),
        },
    }


def extract(xlsx_path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """读 workbook → 定位表头 → 产出 case 业务 dict 列表 + 表标题（首行单元格或文件名）。"""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.worksheets[0]
    header_row, col_of = _find_header(ws)

    cases: list[dict[str, Any]] = []
    for r in range(header_row + 1, ws.max_row + 1):
        row_vals = {field: _norm_cell(ws.cell(row=r, column=c).value) for field, c in col_of.items()}
        # 必须有「用例名称」或「测试步骤」才算一条用例，否则是空行/分隔行。
        if not (row_vals.get("item") or row_vals.get("steps")):
            continue
        cases.append(_row_case(row_vals, r))

    title = _norm_cell(ws.title) or Path(xlsx_path).stem
    return cases, title


def _v2_case(business: dict[str, Any], cid: str, status: str) -> dict[str, Any]:
    """业务 dict 套上 v2 生命周期字段（id/status/revision 居前，与 migrate_v2 字段序一致）。"""
    return {"id": cid, "status": status, "revision": 1, **business}


def _next_seq_from(ids: list[str]) -> int:
    from autophone.assets.case_import.migrate_v2 import _next_seq

    return _next_seq(ids)


def build_document(cases: list[dict[str, Any]], title: str, source: str) -> dict[str, Any]:
    """seed 文档（仅输出不存在时用）：meta + 分类统计 + draft 全集。id 从 R1 顺序分配。"""
    from collections import Counter

    case_dicts = [_v2_case(b, f"R{i}", "draft") for i, b in enumerate(cases, start=1)]
    by_auto = Counter(b["automation"] for b in cases)
    by_module = Counter(b["module"] for b in cases)
    document = {
        "meta": {
            "title": title,
            "source": source,
            "total": len(case_dicts),
            "schema_version": 2,
            "next_seq": len(case_dicts) + 1,
            "note": "由 autophone/assets/case_import/from_template.py 自动生成；automation 为启发式初值，需人工复核。",
        },
        "stats": {"by_automation": dict(by_auto), "by_module": dict(by_module)},
        "cases": case_dicts,
    }
    validate_case_registry_or_raise(document, source=f"template seed {source}")
    return document
NewRowsValidator = Callable[[list[dict[str, Any]]], None]


def partition_import_cases(
    existing: dict[str, Any] | None,
    cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split an import batch into first-seen rows and duplicate external IDs.

    Existing Registry identities and earlier rows in this same batch share one
    ``seen`` set.  Blank external IDs remain non-deduplicable and are all kept.
    """
    seen = import_dedup_external_ids(existing) if existing is not None else set()
    new_cases: list[dict[str, Any]] = []
    duplicate_external_ids: list[str] = []
    for case in cases:
        raw = case.get("raw")
        value = raw.get("external_id") if isinstance(raw, dict) else None
        external_id = value.strip() if isinstance(value, str) else ""
        if external_id and external_id in seen:
            duplicate_external_ids.append(external_id)
            continue
        new_cases.append(case)
        if external_id:
            seen.add(external_id)
    return new_cases, duplicate_external_ids


def merge_into(existing_text: str, cases: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """把模板解析出的 cases 合并进已存在的 v2 YAML：按 external_id 去重，仅追加新行为 draft。

    现存用例一字不动。返回 (新文本|None, 新增 id 列表)；无新增返回 (None, [])。
    现存非 v2 → fail-loud。无 external_id 的行无法去重，一律视为新行追加。
    """
    existing = yaml.safe_load(existing_text) or {}
    if not isinstance(existing, dict):
        raise ValueError("现存 Case Registry 顶层必须是对象")
    if (existing.get("meta") or {}).get("schema_version") != 2:
        raise SystemExit(
            "现存 YAML 不是 schema_version 2；merge 前请先跑 "
            "`python3 -m autophone.assets.case_import.migrate_v2 --write`"
        )
    validate_case_registry_or_raise(existing, source="template merge existing")
    existing_cases = existing.get("cases") or []
    existing_ids = {str(c.get("id")) for c in existing_cases}
    # 普通 deprecated Case 释放 external_id；已领取 reservation 的 Case 在任意生命周期
    # 状态都保留稳定身份。现存与批内去重共用 partition helper，预览/commit 保持一致。
    new_business, _duplicate_external_ids = partition_import_cases(existing, cases)
    if not new_business:
        return None, []

    new_ids, next_seq_after = allocate_case_ids(existing, new_business)
    new_dicts = [
        _v2_case(business, case_id, "draft")
        for business, case_id in zip(new_business, new_ids, strict=True)
    ]
    block = yaml.safe_dump(new_dicts, allow_unicode=True, sort_keys=False, width=100)

    text = asset_store._append_block_to_list(existing_text, "cases", block)
    text = _patch_meta_scalar(text, "total", len(existing_ids) + len(new_ids))
    text = _patch_meta_scalar(text, "next_seq", next_seq_after)
    validate_case_registry_or_raise(yaml.safe_load(text), source="template merge")
    return text, new_ids


def commit_to_path(
    path: Path,
    cases: list[dict[str, Any]],
    title: str,
    *,
    source: str,
    dry_run: bool = False,
    validate_new_rows: NewRowsValidator | None = None,
) -> tuple[str, list[str]]:
    """Plan and optionally commit one seed/merge through the Case Registry transaction.

    ``validate_new_rows`` runs inside the transaction on the rows that remain
    after external_id deduplication; rows dropped as duplicates are not passed.
    """

    def mutation(existing_text: str | None) -> tuple[str | None, tuple[str, list[str]]]:
        if existing_text is not None:
            if validate_new_rows is not None:
                existing = yaml.safe_load(existing_text) or {}
                if isinstance(existing, dict):
                    # 与 merge_into 同一去重规则；被去重丢弃的行不参与校验。
                    appended, _duplicates = partition_import_cases(existing, cases)
                    if appended:
                        validate_new_rows(appended)
            replacement, new_ids = merge_into(existing_text, cases)
            return replacement, ("merge", new_ids)
        new_cases, _duplicate_external_ids = partition_import_cases(None, cases)
        if validate_new_rows is not None and new_cases:
            validate_new_rows(new_cases)
        document = build_document(new_cases, title, source=source)
        replacement = yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        )
        return replacement, ("seed", [case["id"] for case in document["cases"]])

    if dry_run:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        _replacement, result = mutation(current)
        return result
    return cases_store.mutate_registry_text(path, mutation)


def reject_family_rows(rows: list[dict[str, Any]]) -> None:
    """Standalone entry: refuse rows carrying ``family_id``.

    This entry only knows the output file, not the App Profile, so it cannot
    check ``business_chains.yaml`` (Case Family proposal V3/V4).  The current
    template has no Family column; such rows must go through
    ``autophone --app <app> app case import`` or the dashboard import, which
    check the declaration.
    """
    carrying = [
        str((row.get("raw") or {}).get("external_id") or index)
        for index, row in enumerate(rows)
        if isinstance(row.get("family_id"), str) and row["family_id"].strip()
    ]
    if carrying:
        raise SystemExit(
            f"{len(carrying)} 行带 family_id（{carrying[:5]}）；独立入口不能校验 "
            "business_chains.yaml，请改用 app case import 或看板导入提交"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--xlsx", required=True, help="通用用例模板 xlsx 路径")
    ap.add_argument("--out", required=True, help="目标 YAML（apps/<app>/cases/*.yaml）")
    ap.add_argument("--dry-run", action="store_true", help="只打将写/追加的条数，不落盘")
    args = ap.parse_args()

    out = Path(args.out)
    if not args.dry_run:
        assert_assets_writable("import case template asset", path=out)
    cases, title = extract(args.xlsx)

    mode, new_ids = commit_to_path(
        out,
        cases,
        title,
        source=args.xlsx,
        dry_run=args.dry_run,
        validate_new_rows=reject_family_rows,
    )
    if mode == "merge" and not new_ids:
        print(f"merge：无新增用例（按 external_id 去重），{out} 未变更", file=sys.stderr)
    elif args.dry_run and mode == "merge":
        print(f"merge dry-run：将追加 {len(new_ids)} 条 draft → {new_ids}", file=sys.stderr)
    elif args.dry_run:
        print(f"seed dry-run：将写 {len(new_ids)} 条 draft → {out}（文件当前不存在）", file=sys.stderr)
    elif mode == "merge":
        print(f"merge：追加 {len(new_ids)} 条 draft → {out}：{new_ids}", file=sys.stderr)
    else:
        print(f"seed：写入 {len(new_ids)} 条 draft → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
