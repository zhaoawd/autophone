"""migrate_v2.py — cases YAML schema_version 1 → 2 的一次性地基迁移。

只做「字段补齐」，逐行定点插入，**不走 yaml round-trip**——避免 safe_dump
重排/重引号已人工复核过的 automation_reason / raw 块，把语义无关的 diff 噪声
压到零。可重复执行（幂等）：已迁移的文件再跑只会原样输出。

补三处：
  meta.schema_version: 1 → 2
  meta.next_seq: <max 数字 id + 1>   （手工新增用例的 id 分配器）
  每条 case 在 `- id:` 行后插入 status: active / revision: 1

用法：
  python3 -m autophone.assets.case_import.migrate_v2 --dry-run   # 打 unified diff，不落盘
  python3 -m autophone.assets.case_import.migrate_v2 --write      # 落盘
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from autophone.assets.policy import assert_assets_writable
from autophone.paths import refuse_app_asset_override


ID_RE = re.compile(r"^- id:\s*(.+?)\s*$")
NUM_ID_RE = re.compile(r"^R(\d+)$")
SCHEMA_RE = re.compile(r"^(\s*)schema_version:\s*1\s*$")


def _next_seq(ids: list[str]) -> int:
    nums = [int(m.group(1)) for i in ids if (m := NUM_ID_RE.match(i))]
    return (max(nums) + 1) if nums else 1


def migrate_lines(lines: list[str]) -> list[str]:
    # 先扫一遍收集所有 case id，算 next_seq
    ids = [m.group(1) for line in lines if (m := ID_RE.match(line.rstrip("\n")))]
    next_seq = _next_seq(ids)

    out: list[str] = []
    meta_done = False
    for idx, line in enumerate(lines):
        body = line.rstrip("\n")

        # meta: schema_version 1 → 2，并在其后插入 next_seq
        m = SCHEMA_RE.match(body)
        if m and not meta_done:
            indent = m.group(1)
            out.append(f"{indent}schema_version: 2\n")
            out.append(f"{indent}next_seq: {next_seq}\n")
            meta_done = True
            continue

        out.append(line)

        # 每条 case：在 `- id:` 行后补 status / revision（幂等：已有则跳过）
        if ID_RE.match(body):
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if not nxt.startswith("status:"):
                out.append("  status: active\n")
                out.append("  revision: 1\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--path", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="打 unified diff，不落盘（默认）")
    g.add_argument("--write", action="store_true", help="落盘")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.is_absolute():
        # 相对路径按工作目录解析；App 资产根被移走时不知道该落在哪棵树，拒绝。
        refuse_app_asset_override("migrate case asset to v2 (relative --path)", path=path)
    if args.write:
        assert_assets_writable("migrate case asset to v2", path=path)
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 2

    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    migrated = migrate_lines(lines)

    if migrated == lines:
        print(f"{path} 已是 v2（无变化）", file=sys.stderr)
        return 0

    if args.write:
        path.write_text("".join(migrated), encoding="utf-8")
        added = len(migrated) - len(lines)
        print(f"已迁移 {path}（+{added} 行）", file=sys.stderr)
        return 0

    diff = difflib.unified_diff(
        lines, migrated, fromfile=str(path), tofile=str(path) + " (v2)", n=2
    )
    sys.stdout.writelines(diff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
