"""asset_store.py — app-agnostic 的 YAML 资产生命周期核心（编辑 / 废弃 / 追加）。

平台里「用例库」「操作库」等所有 `meta + <list>` 形态的 YAML 资产共享同一套
生命周期写操作：stable key / revision / status(draft·active·deprecated·archived) +
surgical 定点写回。本模块是那套核心，不绑定任何具体资产类型——靠 `AssetSpec`
参数化（顶层列表键、schema_version、可编辑标量集、id 前缀），Case 与 Operation
存储通过对应的 `AssetSpec` 使用同一核心（见 platform-roadmap §八.3）。

沿用「逐行定点改、不 round-trip」原则：编辑只动目标条目的目标标量行，追加只在
列表尾 append——避免 yaml.safe_dump 整体重排，git diff 永远只反映真实改动。

约束（fail-loud）：
  - 标量字段（automation / level / 文案等）走 surgical 定点写回，只动目标行。
  - 列表字段（steps/expects/precondition…）按 `editable_lists` 显式声明才可编辑；
    多行块没法逐行 patch，改它会**整条目重排一次**（仅该条目的块被 yaml 重 dump，
    其余条目一字不动），diff 仍局部。未声明为可编辑的字段传入直接报错。
  - 内容字段变更 → revision++；纯 status 变更（废弃 / 复活）不动 revision。
    `AssetSpec.classification_scalars` 列出的分类元数据标量同样不动 revision。
  - 现存 YAML 的 meta.schema_version 必须等于 spec.schema_version。
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from weakref import WeakValueDictionary

from autophone.assets.policy import assert_assets_writable

# 生命周期状态机：所有资产共用同一套 status 取值。
VALID_STATUS = {"draft", "active", "deprecated", "archived"}
LIFECYCLE = {"status"}

_ID_RE = re.compile(r"^(?P<indent>\s*)- id:\s*(?P<value>.+?)\s*$")
_TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*")

_PATH_LOCKS_GUARD = threading.Lock()
# 活跃或等待中的调用都在局部变量中强引用同一把锁；无调用者的动态路径可回收。
_PATH_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_PATH_LOCK_STATE = threading.local()


def _resolved_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _process_path_lock(path: Path) -> threading.RLock:
    key = str(_resolved_path(path))
    with _PATH_LOCKS_GUARD:
        process_lock = _PATH_LOCKS.get(key)
        if process_lock is None:
            process_lock = threading.RLock()
            _PATH_LOCKS[key] = process_lock
        return process_lock


@contextmanager
def path_write_lock(path: Path) -> Iterator[None]:
    """Serialize one asset path across threads and independent host processes.

    ``flock`` alone is not a sufficient same-process thread mutex when callers
    open the lock file through different descriptors.  The per-path ``RLock``
    supplies that boundary and also makes nested domain-store calls re-entrant.
    A stable sibling lock file keeps the lock inode independent from the atomic
    replacement of the asset itself.
    """
    resolved = _resolved_path(path)
    key = str(resolved)
    process_lock = _process_path_lock(resolved)
    with process_lock:
        held = getattr(_PATH_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _PATH_LOCK_STATE.held = held
        if key in held:
            yield
            return

        resolved.parent.mkdir(parents=True, exist_ok=True)
        lock_path = resolved.parent / f".{resolved.name}.autophone.lock"
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            held[key] = lock_file
            try:
                yield
            finally:
                held.pop(key, None)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` through a same-directory temporary file and ``os.replace``."""
    path = _resolved_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class AssetSpec:
    """一类 YAML 资产的生命周期契约。

    list_key:         顶层列表键（"cases" / "operations"），也是追加时的块头。
    schema_version:   meta.schema_version 期望值，不符直接 fail-loud。
    editable_scalars: 允许 UI/API 编辑的扁平标量字段集（嵌套结构不在内）。
    editable_lists:   允许编辑的「字符串列表」字段集（steps/expects/precondition…）；
                      改它走整条目块重排（见模块 docstring），未声明的列表字段不可改。
    editable_structs: 允许编辑的「结构化列表」字段集（list-of-dict，如操作引用
                      setup/teardown/suggested_*）。与 editable_lists 同样走整条目块重排，
                      但不施加「元素须为 str」的守卫——元素可以是嵌套 dict。
    id_prefix:        append 分配 id 的前缀（"R" / "OP"），id = f"{prefix}{next_seq}"。
    migrate_hint:     schema_version 不符时附在错误消息后的迁移提示（可空）。
    classification_scalars:
                      editable_scalars 中的分类元数据字段。写入仍走同一写锁与 surgical 写回，
                      但与 status 一样不使 revision 加 1：revision 绑定测试意图与执行身份，
                      分类不改变二者（Case Family 提案 D2）。
    """

    list_key: str
    schema_version: int
    editable_scalars: frozenset[str]
    id_prefix: str
    editable_lists: frozenset[str] = field(default_factory=frozenset)
    editable_structs: frozenset[str] = field(default_factory=frozenset)
    migrate_hint: str = ""
    valid_status: frozenset[str] = field(default_factory=lambda: frozenset(VALID_STATUS))
    classification_scalars: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        stray = self.classification_scalars - self.editable_scalars
        if stray:
            raise ValueError(f"classification_scalars 必须是 editable_scalars 的子集：{sorted(stray)}")

    def bumps_revision(self, key: str) -> bool:
        """Whether writing ``key`` is a content change that increments revision."""
        if key in self.classification_scalars:
            return False
        return key in self.editable_scalars or key in self.editable_lists or key in self.editable_structs


def _require_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - 写路径只在装了依赖的 server 跑
        raise RuntimeError("asset_store 写回需要 PyYAML；请 `uv sync` 后在平台进程内调用") from exc
    return yaml


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _emit_scalar(key: str, value: Any, *, indent: str = "  ") -> str:
    """把单个标量字段 dump 成一行（含必要的引号/转义）。"""
    yaml = _require_yaml()
    dumped = yaml.safe_dump(
        {key: value}, allow_unicode=True, default_flow_style=False, sort_keys=False, width=10**9
    ).rstrip("\n")
    if "\n" in dumped:
        raise ValueError(f"字段 {key} 的值跨行，surgical 写回只支持单行标量")
    return indent + dumped


def _item_indent(lines: list[str], start: int) -> str:
    match = _ID_RE.match(lines[start].rstrip("\n"))
    if match is None:
        raise ValueError("资产条目起始行不是 '- id:'")
    return match.group("indent")


def _field_indent(lines: list[str], start: int) -> str:
    return _item_indent(lines, start) + "  "


def _find_block(lines: list[str], item_id: str) -> tuple[int, int]:
    """返回 item_id 对应块的 [start, end)；start 是 `- id:` 行，end 独占下一个块/EOF。"""
    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _ID_RE.match(line.rstrip("\n"))
        if m and _unquote(m.group("value")) == item_id:
            candidates.append((len(m.group("indent")), i))
    if not candidates:
        raise KeyError(item_id)
    _, start = min(candidates)
    item_indent = _item_indent(lines, start)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        match = _ID_RE.match(lines[j].rstrip("\n"))
        if match is not None and match.group("indent") == item_indent:
            end = j
            break
    return start, end


def _field_line_idx(lines: list[str], start: int, end: int, key: str) -> int | None:
    pat = re.compile(rf"^{re.escape(_field_indent(lines, start))}{re.escape(key)}:(\s|$)")
    for i in range(start, end):
        if pat.match(lines[i].rstrip("\n")):
            return i
        # 只扫该条目的直接子字段（2 空格缩进），不误入更深层嵌套块
    return None


def _set_field(lines: list[str], start: int, end: int, key: str, value: Any) -> tuple[list[str], int]:
    """替换或插入一个标量字段，返回新 lines 与新 end（插入会 +1）。"""
    new_line = _emit_scalar(key, value, indent=_field_indent(lines, start)) + "\n"
    idx = _field_line_idx(lines, start, end, key)
    if idx is not None:
        lines[idx] = new_line
        return lines, end
    # 不存在 → 插到 id 行之后（status/revision 之前的位置即可，保持靠前）
    lines.insert(start + 1, new_line)
    return lines, end + 1


def _patch_meta_scalar(text: str, key: str, value: Any) -> str:
    pat = re.compile(rf"^(  {re.escape(key)}: ).*$", re.MULTILINE)
    new, n = pat.subn(lambda m: f"{m.group(1)}{value}", text, count=1)
    if n == 0:
        raise ValueError(f"meta.{key} 未找到，无法定点 patch")
    return new


def _read_doc(spec: AssetSpec, path: Path) -> dict[str, Any]:
    yaml = _require_yaml()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if (doc.get("meta") or {}).get("schema_version") != spec.schema_version:
        hint = f"；{spec.migrate_hint}" if spec.migrate_hint else ""
        raise ValueError(f"{path} 不是 schema_version {spec.schema_version}{hint}")
    return doc


def _item_dict(spec: AssetSpec, doc: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in doc.get(spec.list_key) or []:
        if str(item.get("id")) == item_id:
            return item
    raise KeyError(item_id)


def _append_block_to_list(text: str, list_key: str, block: str) -> str:
    """Append a dumped list item block to a top-level YAML list.

    The asset files are human-maintained and may have comments or later top-level
    sections after the list. Insert at the end of the target list block instead
    of assuming the list is the final document section.
    """
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^{re.escape(list_key)}:\s*(.*)$")
    list_idx = None
    inline_empty = False
    inline_comment = ""
    for i, line in enumerate(lines):
        match = key_re.match(line.rstrip("\n"))
        if match:
            list_idx = i
            inline_match = re.fullmatch(r"\[\s*\](?P<comment>\s+#.*)?\s*", match.group(1))
            inline_empty = inline_match is not None
            if inline_match is not None:
                inline_comment = inline_match.group("comment") or ""
            break
    if list_idx is None:
        raise ValueError(f"{list_key}: 未找到，无法追加")

    if inline_empty:
        lines[list_idx] = f"{list_key}:{inline_comment}\n"

    insert_at = len(lines)
    for i in range(list_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _TOP_LEVEL_KEY_RE.match(line):
            insert_at = i
            break

    item_indent = 0
    for line in lines[list_idx + 1:insert_at]:
        match = re.match(r"^(\s*)-\s", line)
        if match:
            item_indent = len(match.group(1))
            break
    if item_indent:
        indent = " " * item_indent
        block = "".join(indent + line if line.strip() else line for line in block.splitlines(keepends=True))

    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    lines[insert_at:insert_at] = block.splitlines(keepends=True)
    return "".join(lines)


# --------------------------------------------------------------------------- #
# 公开 API（按 spec 操作；具体资产模块包一层语义化名字）
# --------------------------------------------------------------------------- #
def update_item(spec: AssetSpec, path: Path, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """编辑一条资产的字段。内容字段变更 → revision++；纯 status 与分类元数据变更不动 revision。

    标量字段走 surgical 定点写回；列表/结构化字段（在 spec.editable_lists /
    editable_structs 内）走整条目块重排。
    返回写回后的该条目 dict。未知/未声明可编辑字段、非法 status 一律 fail-loud。
    """
    assert_assets_writable(f"update {spec.list_key} asset", path=path)
    bad = (
        set(changes)
        - set(spec.editable_scalars)
        - set(spec.editable_lists)
        - set(spec.editable_structs)
        - LIFECYCLE
    )
    if bad:
        raise ValueError(f"不可编辑的字段：{sorted(bad)}（嵌套结构或未知字段）")
    if "status" in changes and changes["status"] not in spec.valid_status:
        raise ValueError(f"非法 status：{changes['status']}（应为 {sorted(spec.valid_status)}）")

    for k in (k for k in changes if k in spec.editable_lists):
        v = changes[k]
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            raise ValueError(f"字段 {k} 须为字符串列表（steps/expects/precondition 形态）")
    for k in (k for k in changes if k in spec.editable_structs):
        if not isinstance(changes[k], list):
            raise ValueError(f"字段 {k} 须为列表（操作引用 [{{op, with}}] 形态）")

    # 列表 + 结构化字段都没法逐行 patch，需走整条目块重排。
    block_keys = [k for k in changes if k in spec.editable_lists or k in spec.editable_structs]

    doc = _read_doc(spec, path)
    _item_dict(spec, doc, item_id)  # 校验存在，不存在抛 KeyError
    content_changed = any(spec.bumps_revision(k) for k in changes)

    # 列表/结构化字段无法逐行 patch：把该条目整块按更新后的 dict 重 dump（仅这一条重排）。
    if block_keys:
        return _rewrite_item(spec, path, item_id, changes, bump_revision=content_changed)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_block(lines, item_id)

    for key, value in changes.items():
        lines, end = _set_field(lines, start, end, key, value)

    if content_changed:
        ridx = _field_line_idx(lines, start, end, "revision")
        cur = 1
        if ridx is not None:
            m = re.match(
                rf"^{re.escape(_field_indent(lines, start))}revision:\s*(\d+)",
                lines[ridx].rstrip("\n"),
            )
            cur = int(m.group(1)) if m else 1
        lines, end = _set_field(lines, start, end, "revision", cur + 1)

    atomic_write_text(path, "".join(lines))
    return _item_dict(spec, _read_doc(spec, path), item_id)


def rewrite_item_fields(
    spec: AssetSpec, path: Path, item_id: str, changes: dict[str, Any], *,
    bump_revision: bool = False,
) -> dict[str, Any]:
    """写回 `update_item` 的可编辑面之外的结构化字段（provenance 这类元数据）。

    `update_item` 只放行声明过的可编辑字段——那是给 UI/API 的护栏，防人误改契约。平台自己
    维护的溯源元数据（来源录制没了、step→event 映射…）不该为此被迫加进那张白名单，否则
    编辑面板也跟着能改。调用方自己负责字段合法性（并在写后过 schema 校验）。
    """
    assert_assets_writable(f"rewrite {spec.list_key} asset", path=path)
    return _rewrite_item(spec, path, item_id, changes, bump_revision=bump_revision)


def _rewrite_item(
    spec: AssetSpec, path: Path, item_id: str, changes: dict[str, Any], *, bump_revision: bool
) -> dict[str, Any]:
    """把目标条目整块替换成「应用 changes 后的 dict」的 yaml dump，其余条目原样保留。

    用于列表字段编辑——多行块没法 surgical patch。字段顺序沿用现存 dict（safe_load
    保序），改值就地、新键追加，故 diff 仍只落在这一条目。"""
    assert_assets_writable(f"rewrite {spec.list_key} asset", path=path)
    yaml = _require_yaml()
    doc = _read_doc(spec, path)
    item = dict(_item_dict(spec, doc, item_id))  # 现存条目（保序）
    for key, value in changes.items():
        item[key] = value
    if bump_revision:
        item["revision"] = int(item.get("revision") or 1) + 1

    block = yaml.safe_dump([item], allow_unicode=True, sort_keys=False, width=100)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_block(lines, item_id)
    indent = _item_indent(lines, start)
    if indent:
        block = "".join(
            indent + line if line.strip() else line
            for line in block.splitlines(keepends=True)
        )
    new_lines = lines[:start] + block.splitlines(keepends=True) + lines[end:]
    atomic_write_text(path, "".join(new_lines))
    return _item_dict(spec, _read_doc(spec, path), item_id)


def deprecate_item(spec: AssetSpec, path: Path, item_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """软删一条资产：status=deprecated（+ 可选 deprecate_reason），不动 revision。"""
    assert_assets_writable(f"deprecate {spec.list_key} asset", path=path)
    result = update_item(spec, path, item_id, {"status": "deprecated"})
    if reason:
        # deprecate_reason 不在 editable_scalars（避免 UI 误改），这里单独定点写
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        start, end = _find_block(lines, item_id)
        lines, _ = _set_field(lines, start, end, "deprecate_reason", reason)
        atomic_write_text(path, "".join(lines))
        result = _item_dict(spec, _read_doc(spec, path), item_id)
    return result


def deprecate_items(
    spec: AssetSpec, path: Path, item_ids: list[str], *, reason: str | None = None
) -> dict[str, Any]:
    """批量软删：逐条走 surgical `deprecate_item`（保持最小 diff），一次调用一次汇总返回。

    返回 {"deprecated": [item dict...], "failed": [{"id":..., "error":...}...]}。
    单条失败（不存在/非法）不影响其余，逐条 fail-soft 记录。已 deprecated 的条目
    再次废弃是幂等的（status 不变），照常计入 deprecated。
    """
    assert_assets_writable(f"batch deprecate {spec.list_key} assets", path=path)
    deprecated: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item_id in item_ids:
        sid = str(item_id)
        if sid in seen:  # 去重，避免对同一条目重复写
            continue
        seen.add(sid)
        try:
            deprecated.append(deprecate_item(spec, path, sid, reason=reason))
        except (KeyError, ValueError) as exc:
            failed.append({"id": sid, "error": str(exc) or type(exc).__name__})
    return {"deprecated": deprecated, "failed": failed}


def set_status_items(
    spec: AssetSpec, path: Path, item_ids: list[str], status: str
) -> dict[str, Any]:
    """批量改状态：逐条走 surgical `update_item({"status": status})`（最小 diff），一次汇总返回。

    返回 {"updated": [item dict...], "failed": [{"id":..., "error":...}...]}。
    单条失败（不存在/非法 status）fail-soft 记录，不影响其余；同 id 去重。
    纯 status 变更不动 revision（由 update_item 判定）。
    """
    assert_assets_writable(f"batch update {spec.list_key} status", path=path)
    updated: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item_id in item_ids:
        sid = str(item_id)
        if sid in seen:
            continue
        seen.add(sid)
        try:
            updated.append(update_item(spec, path, sid, {"status": status}))
        except (KeyError, ValueError) as exc:
            failed.append({"id": sid, "error": str(exc) or type(exc).__name__})
    return {"updated": updated, "failed": failed}


def append_item(
    spec: AssetSpec, path: Path, fields: dict[str, Any], *, status: str = "active",
    item_id: str | None = None, allocated_next_seq: int | None = None,
) -> dict[str, Any]:
    """在列表尾追加一条新资产，返回新条目 dict。

    fields 给业务字段；status/revision/meta.total/next_seq 由本函数管理。
    id 默认从 meta.next_seq 分配（`{prefix}{next_seq}`）；传 item_id 则用它当 id
    （api_expectations 等「id 须与外部主键（CASE_ID）一致」的资产用），next_seq 仍前进。
    ``allocated_next_seq`` 仅供具有独立分配契约的领域 store 使用：调用方一次性提供已
    规划的 id 与写后游标，保留 ID 等旁路分配因此无需伪消耗普通游标。
    """
    assert_assets_writable(f"append {spec.list_key} asset", path=path)
    if status not in spec.valid_status:
        raise ValueError(f"非法 status：{status}（应为 {sorted(spec.valid_status)}）")
    yaml = _require_yaml()
    doc = _read_doc(spec, path)
    next_seq = int((doc.get("meta") or {}).get("next_seq") or 1)
    if allocated_next_seq is not None and (
        not isinstance(allocated_next_seq, int)
        or isinstance(allocated_next_seq, bool)
        or allocated_next_seq < next_seq
    ):
        raise ValueError("allocated_next_seq 必须是不小于当前 meta.next_seq 的整数")
    new_id = str(item_id) if item_id is not None else f"{spec.id_prefix}{next_seq}"
    existing = {str(c.get("id")) for c in (doc.get(spec.list_key) or [])}
    if new_id in existing:  # 显式 id 撞已存在，或 next_seq 与现存条目不一致 → fail-loud
        raise ValueError(f"分配的 id {new_id} 已存在；显式 id 撞库或 meta.next_seq 不一致")

    item = {
        "id": new_id,
        "status": status,
        "revision": 1,
        # 生命周期字段由本层管理；业务 fields 不得覆盖已校验的值。
        **{k: v for k, v in fields.items() if k not in {"id", "status", "revision"}},
    }
    block = yaml.safe_dump([item], allow_unicode=True, sort_keys=False, width=100)

    text = _append_block_to_list(path.read_text(encoding="utf-8"), spec.list_key, block)
    text = _patch_meta_scalar(text, "total", len(existing) + 1)
    text = _patch_meta_scalar(
        text, "next_seq", allocated_next_seq if allocated_next_seq is not None else next_seq + 1
    )
    atomic_write_text(path, text)
    return _item_dict(spec, _read_doc(spec, path), new_id)
