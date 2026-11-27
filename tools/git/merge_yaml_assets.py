#!/usr/bin/env python3
"""merge_yaml_assets.py — git 自定义合并驱动：用例库 / 操作库 YAML 的三方合并。

这些资产是 `meta + <list>`（cases / operations）形态，两台机器各自 `app case import`、
看板编辑、运行期 status/revision++ 后，用 git 同步会撞两类东西：
  ① 行级冲突满屏（每条目 revision 各自 ++、meta.next_seq 各自递增）；
  ② 真撞号（两机各自 append 出同一个 R{n}，内容却不同）。

普通 text 合并对②无能为力、对①只会丢一堆冲突标记。本驱动按 *条目语义* 三方合并：
  - 稳定键 = raw.external_id（非空时）否则 id；按键做三方 union 合并；
  - 一方未改 → 取另一方；两方都改 → 取 revision 高者；revision 相同且内容不同 → 真冲突；
  - 条目只在一方出现（另一方是旧/空副本）→ 保留，绝不丢用例（本平台用 deprecate 软删，
    从不从列表里物删，故 union 是安全且正确的）；
  - meta.next_seq 取 max(两方 next_seq, 最大数字 id + 1)，total 重算；
  - 真撞号（两个不同键落到同一个 R{n}）→ fail-loud 退出，列出冲突 id 让人工改号
    （不静默 re-key，因为 recipes/r{n}.yaml、历史报告都按 id 引用，改号会断链）。

git 调用约定：driver %O %A %B %P
  %O=共同祖先 %A=ours（合并结果必须写回这里）%B=theirs %P=路径名。
  退出码 0=合并成功；非 0=留冲突给人工（git 标 unmerged，%A 留最佳努力结果）。

限制（中等档，刻意取舍）：
  - meta 的 note/title/stats 取 ours（A）一侧；stats 不重算（运行期会重算，仅展示用）。
  - 重新 dump 整个文档（PyYAML），长字符串会被 reflow——比冲突标记可读得多，且能被
    asset_store 重新 round-trip。

自检： python3 -m tools.git.merge_yaml_assets --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# 已知资产列表键 → id 前缀；自动探测时用作校验。
KNOWN_LIST_KEYS = {"cases": "R", "operations": "OP"}


def _load(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: 顶层不是 mapping，无法按资产合并")
    return doc


def _detect_list_key(*docs: dict[str, Any]) -> str:
    """探测 meta+list 里那个列表键（cases / operations）。多文档取并集校验一致。"""
    found: set[str] = set()
    for doc in docs:
        for k in KNOWN_LIST_KEYS:
            if isinstance(doc.get(k), list):
                found.add(k)
    if len(found) == 1:
        return found.pop()
    if not found:
        raise ValueError(f"未找到资产列表键（期望其一：{sorted(KNOWN_LIST_KEYS)}）")
    raise ValueError(f"探测到多个资产列表键，无法判定：{sorted(found)}")


def _key_of(item: dict[str, Any]) -> tuple[str, str]:
    """条目稳定键：raw.external_id 非空优先，否则 id。external_id 跨机稳定、id 会撞。"""
    ext = ((item.get("raw") or {}).get("external_id") or "")
    if isinstance(ext, str) and ext.strip():
        return ("ext", ext.strip())
    return ("id", str(item.get("id")))


def _num_id(item: dict[str, Any]) -> int:
    """从 'R47' / 'OP10' 取数字部分，用于排序与 next_seq 计算；非法返回 -1。"""
    cid = str(item.get("id") or "")
    digits = "".join(ch for ch in cid if ch.isdigit())
    return int(digits) if digits else -1


def _revision(item: dict[str, Any]) -> int:
    try:
        return int(item.get("revision") or 1)
    except (TypeError, ValueError):
        return 1


def merge_docs(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """三方合并，返回 (merged_doc, conflicts)。conflicts 非空即需人工介入。"""
    list_key = _detect_list_key(base, ours, theirs)

    def by_key(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for it in (doc.get(list_key) or []):
            if isinstance(it, dict):
                out[_key_of(it)] = it
        return out

    mO, mA, mB = by_key(base), by_key(ours), by_key(theirs)
    conflicts: list[str] = []
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    for key in {*mA, *mB}:  # 祖先里有、两侧都没的 → 两侧都删，自然丢弃
        a, b, o = mA.get(key), mB.get(key), mO.get(key)
        if a is not None and b is not None:
            if a == b:
                merged[key] = a
            elif o is not None and a == o:
                merged[key] = b            # 仅 theirs 改
            elif o is not None and b == o:
                merged[key] = a            # 仅 ours 改
            else:                          # 两侧都改（或无祖先）：取 revision 高者
                ra, rb = _revision(a), _revision(b)
                if ra > rb:
                    merged[key] = a
                elif rb > ra:
                    merged[key] = b
                else:                      # 同 revision 内容不同 → 真冲突
                    conflicts.append(
                        f"内容冲突 key={key} id={a.get('id')}：两侧 revision 同为 {ra} 但内容不同"
                    )
                    merged[key] = a        # 先留 ours，待人工
        elif a is not None:
            merged[key] = a                # 只在 ours（theirs 是旧/空副本）→ 保留
        else:
            merged[key] = b                # 只在 theirs → 保留

    # 真撞号检查：不同稳定键落到同一个 R{n}
    by_id: dict[str, tuple[str, str]] = {}
    for key, it in merged.items():
        cid = str(it.get("id"))
        if cid in by_id and by_id[cid] != key:
            conflicts.append(
                f"撞号 id={cid}：两个不同条目共用此 id（keys {by_id[cid]} vs {key}），"
                f"请在其中一台机器把该用例改成新号后重新提交（勿自动改号，会断 recipe/报告引用）"
            )
        by_id[cid] = key

    # 输出：meta 取 ours 一侧，只校正 next_seq / total；条目按数字 id 升序稳定排列。
    out = dict(ours)
    meta = dict(ours.get("meta") or {})
    max_num = max([_num_id(it) for it in merged.values()] or [0])
    meta["next_seq"] = max(
        int((ours.get("meta") or {}).get("next_seq") or 1),
        int((theirs.get("meta") or {}).get("next_seq") or 1),
        max_num + 1,
    )
    if "total" in meta:
        meta["total"] = len(merged)
    out["meta"] = meta
    out[list_key] = sorted(merged.values(), key=_num_id)
    return out, conflicts


def _dump(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(
        doc, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1 << 30
    )


def run_driver(base_path: str, ours_path: str, theirs_path: str) -> int:
    base, ours, theirs = _load(base_path), _load(ours_path), _load(theirs_path)
    merged, conflicts = merge_docs(base, ours, theirs)
    # 最佳努力结果先写回 %A，无论是否冲突，便于人工在文件里直接看/改。
    Path(ours_path).write_text(_dump(merged), encoding="utf-8")
    if conflicts:
        sys.stderr.write(f"[merge_yaml_assets] {ours_path} 需人工介入：\n")
        for c in conflicts:
            sys.stderr.write(f"  - {c}\n")
        return 1
    return 0


def _selftest() -> int:
    base = {"meta": {"next_seq": 3, "total": 2}, "cases": [
        {"id": "R1", "revision": 1, "item": "登录", "raw": {"external_id": "1"}},
        {"id": "R2", "revision": 1, "item": "上岗", "raw": {"external_id": "2"}},
    ]}
    # ours：R2 改了内容(rev2)，又 append 了 R3(ext 10)
    ours = {"meta": {"next_seq": 4, "total": 3}, "cases": [
        {"id": "R1", "revision": 1, "item": "登录", "raw": {"external_id": "1"}},
        {"id": "R2", "revision": 2, "item": "上岗-改", "raw": {"external_id": "2"}},
        {"id": "R3", "revision": 1, "item": "小休", "raw": {"external_id": "10"}},
    ]}
    # theirs：R1 改了内容(rev2)，又 append 了 R3(ext 20) —— 与 ours 的 R3 撞号、内容不同
    theirs = {"meta": {"next_seq": 4, "total": 3}, "cases": [
        {"id": "R1", "revision": 2, "item": "登录-改", "raw": {"external_id": "1"}},
        {"id": "R2", "revision": 1, "item": "上岗", "raw": {"external_id": "2"}},
        {"id": "R3", "revision": 1, "item": "离岗", "raw": {"external_id": "20"}},
    ]}
    merged, conflicts = merge_docs(base, ours, theirs)
    by_id = {c["id"]: c for c in merged["cases"]}
    assert by_id["R1"]["item"] == "登录-改", "应取 theirs 改后的 R1"
    assert by_id["R2"]["item"] == "上岗-改", "应取 ours 改后的 R2"
    assert merged["meta"]["next_seq"] >= 4, merged["meta"]
    assert any("撞号" in c for c in conflicts), f"应检出 R3 撞号：{conflicts}"
    # 无撞号场景：theirs 的 R3 换成不撞的 R4
    theirs2 = {"meta": {"next_seq": 5}, "cases": [
        {"id": "R1", "revision": 2, "item": "登录-改", "raw": {"external_id": "1"}},
        {"id": "R4", "revision": 1, "item": "离岗", "raw": {"external_id": "20"}},
    ]}
    merged2, conflicts2 = merge_docs(base, ours, theirs2)
    ids = {c["id"] for c in merged2["cases"]}
    assert ids == {"R1", "R2", "R3", "R4"}, ids
    assert not conflicts2, conflicts2
    assert merged2["meta"]["next_seq"] >= 5
    print("selftest OK")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--selftest":
        return _selftest()
    if len(argv) < 3:
        sys.stderr.write(
            "用法: merge_yaml_assets.py <base %O> <ours %A> <theirs %B> [path %P]\n"
            "      merge_yaml_assets.py --selftest\n"
        )
        return 2
    return run_driver(argv[0], argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
