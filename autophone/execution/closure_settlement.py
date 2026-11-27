"""Settle input-less Assignment closure journals on the Control Plane terminal record (BL-194).

An Assignment Attempt opens a closure journal before it may touch the Rig. When the
Attempt ends without host product inputs -- the Run Spec was rejected before execution,
or a plugin Run type (``delivery_device_work``) delivered its result only through the
Assignment Result -- no host caller ever freezes or publishes that journal, so every
closure scan reports it as deferred.

This module closes exactly that shape, and only on the Control Plane's own terminal
record for the same Assignment Attempt:

* the Attempt named by the journal is the Assignment's current Attempt and is terminal;
* the Attempt's Lease has been released, so no current Lease holder can still write;
* the recorded Assignment Result carries a closure projection whose Assignment binding,
  Run identity, version and ``record_sha256`` equal the journal on disk, which proves the
  journal is unchanged since the Attempt reported its terminal state.

Anything else stays deferred. Settlement freezes an audit record into the journal; it
never publishes a Run Result, reruns collection, touches a device or changes a Lease.

BL-076 G1 adds one more settleable shape: the Attempt is current, terminal ``indeterminate``
(Assignment and Attempt), its Lease was released with ``release_reason='lease_expired'`` (or
``'agent_interrupted'`` when a restarted Agent reported the Attempt, BL-240) and no
Assignment Result exists. Such a journal is settled as interrupted and unclosed
(``settle_interrupted``) together with a read-only residual resource inventory; the
``indeterminate`` terminal state and the absent Run Result are left unchanged.

The default scan is a dry run: it validates journals without locks, never quarantines
an invalid journal and creates no file. ``apply`` requires the platform run root (the
directory that owns ``index.sqlite``), takes each journal's owner lock, and re-reads the
Assignment record under that lock before writing.

BL-242: ``run_ids`` / ``assignment_ids`` limit both the dry run and ``apply`` to the journals
whose Run id / Assignment id are listed (each filter repeatable, the two filters combine with
AND). Selection reads only the journal file's identity fields, before any Control Plane read or
lock. Every listed value must select at least one journal, otherwise the whole call is refused
(``filter_unmatched``) before anything is written. Without filters every journal under
``run_root`` is scanned, as before.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any, Callable, Iterable, Mapping

from autophone.logging import get_logger

from .closure_journal import (
    INTERRUPTED_SETTLEMENT_KIND,
    SETTLEMENT_KIND,
    ClosureConflict,
    ClosureJournal,
    ClosureQuarantined,
    _digest,
    _encoded,
)

#: 与控制面 `_TERMINAL_ASSIGNMENTS` / `_TERMINAL_ATTEMPTS` 一致。
TERMINAL_STATUSES = frozenset({'passed', 'failed', 'error', 'blocked', 'cancelled', 'indeterminate'})

#: 这些 Run 类型在 host 侧冻结产品输入并经 journal 发布 Run Result（`visual_calibration`
#: 见 `autophone.application.delivery_calibration.RUN_TYPE`）。控制面记有它们的 Run Result
#: 而 journal 却没有冻结输入时，两份记录互相矛盾，不在这里收尾。
HOST_PUBLISHED_RUN_TYPES = frozenset({'regression', 'verification', 'operation', 'visual_calibration'})

AUTHORITY = 'control_plane_assignment_terminal'
INTERRUPTED_AUTHORITY = 'control_plane_lease_expiry'
#: Agent 重连时报告中断、控制面以 ``release_reason='agent_interrupted'`` 释放 Lease 的 Attempt（BL-240）。
INTERRUPTED_REPORT_AUTHORITY = 'control_plane_agent_interrupt'
#: 与控制面 ``INTERRUPTED_RELEASE_REASONS`` 一致：Lease 释放原因 → 结算记录的 ``authority``。
INTERRUPTED_AUTHORITIES = {'lease_expired': INTERRUPTED_AUTHORITY, 'agent_interrupted': INTERRUPTED_REPORT_AUTHORITY}

#: 控制面权威暂不可读或尚未到终态：权威就绪后再跑一次即可收尾。
#: 其余 defer 原因在现行 closure 语义下不会自行消失，汇总为残留形态。
AWAITING_AUTHORITY_REASONS = frozenset({
    'control_plane_unavailable', 'assignment_unreadable', 'assignment_not_terminal',
    'attempt_not_terminal', 'lease_not_released', 'journal_conflict',
    'journal_changed_before_settle', 'authority_changed_before_settle',
})

logger = get_logger(__name__)


class SettlementDeferred(Exception):
    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason = reason
        self.detail = detail


class SettlementRefused(ValueError):
    def __init__(self, code: str, message: str, extra: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.extra = dict(extra or {})


def _journal_identity(path: Path) -> tuple[str | None, str | None]:
    """Run id and Assignment id from the raw journal file; unreadable fields are ``None``. Never writes."""
    try:
        row = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None, None
    if not isinstance(row, dict):
        return None, None
    assignment = row.get('assignment')
    run_id = row.get('run_id') if isinstance(row.get('run_id'), str) else None
    assignment_id = assignment.get('assignment_id') if isinstance(assignment, dict) else None
    return run_id, assignment_id if isinstance(assignment_id, str) else None


def _filter_values(name: str, values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    selected = frozenset(str(value).strip() for value in values)
    if not selected or '' in selected:
        raise ValueError(f'{name} filter needs non-empty values')
    return selected


def select_journals(paths: list[Path], *, run_ids: Iterable[str] | None = None,
                    assignment_ids: Iterable[str] | None = None) -> list[Path]:
    """BL-242: keep journals whose Run id and Assignment id are listed; refuse a value that selects none."""
    wanted_runs = _filter_values('run_ids', run_ids)
    wanted_assignments = _filter_values('assignment_ids', assignment_ids)
    if wanted_runs is None and wanted_assignments is None:
        return paths
    selected, seen_runs, seen_assignments = [], set(), set()
    for path in paths:
        run_id, assignment_id = _journal_identity(path)
        if wanted_runs is not None and run_id not in wanted_runs:
            continue
        if wanted_assignments is not None and assignment_id not in wanted_assignments:
            continue
        selected.append(path)
        seen_runs.add(run_id)
        seen_assignments.add(assignment_id)
    unmatched = {}
    if wanted_runs is not None and wanted_runs - seen_runs:
        unmatched['run_ids'] = sorted(wanted_runs - seen_runs)
    if wanted_assignments is not None and wanted_assignments - seen_assignments:
        unmatched['assignment_ids'] = sorted(wanted_assignments - seen_assignments)
    if unmatched:
        raise SettlementRefused('filter_unmatched', f'filter values select no journal: {unmatched}',
                                {'unmatched': unmatched})
    return selected


def _journal_shape(journal: ClosureJournal) -> tuple[dict | None, str | None]:
    """Return the journal row when its shape qualifies, else a skip reason. Never writes."""
    if journal.quarantine.exists():
        return None, 'quarantined'
    try:
        row = journal.inspect_readonly()
    except ValueError:
        return None, 'invalid_journal'
    if not row.get('assignment'):
        return None, 'no_assignment_binding'
    if row['frozen_inputs'] is not None or row['closure_state'] != 'open':
        return None, 'inputs_frozen'
    if row['terminal_candidate'] is not None or journal.result_path.exists():
        return None, 'terminal_result_present'
    return row, None


def terminal_basis(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """Verify one Control Plane Assignment record against the journal; raise when not settleable."""
    binding = row['assignment']
    if record.get('assignment_id') != binding['assignment_id']:
        raise SettlementDeferred('assignment_identity_mismatch')
    assignment_status = record.get('status')
    if assignment_status not in TERMINAL_STATUSES:
        raise SettlementDeferred('assignment_not_terminal', str(assignment_status))
    attempt = record.get('attempt')
    if not isinstance(attempt, Mapping):
        raise SettlementDeferred('attempt_unavailable')
    if attempt.get('attempt_no') != binding['attempt_no']:
        # 控制面只返回当前 Attempt；被取代的旧 Attempt 的终态在这里读不到，不猜。
        raise SettlementDeferred('journal_attempt_not_current',
                                 f"journal={binding['attempt_no']} current={attempt.get('attempt_no')}")
    actual = (attempt.get('fencing'), attempt.get('rig_id'), attempt.get('rig_generation'))
    expected = (binding['fencing'], binding['rig_id'], binding['generation'])
    if actual != expected:
        raise SettlementDeferred('attempt_binding_mismatch', f'journal={expected} attempt={actual}')
    if attempt.get('status') not in TERMINAL_STATUSES:
        raise SettlementDeferred('attempt_not_terminal', str(attempt.get('status')))
    lease = record.get('lease')
    if not isinstance(lease, Mapping) or not lease.get('released_at'):
        raise SettlementDeferred('lease_not_released')
    result = attempt.get('result')
    if result is None and _lease_expired_interrupted(record, attempt, lease):
        from .interrupted_closure import interrupted_settlement
        run_spec = record.get('run_spec') if isinstance(record.get('run_spec'), Mapping) else {}
        reason = str(lease.get('release_reason'))
        basis = interrupted_settlement(binding, authority=INTERRUPTED_AUTHORITIES[reason],
                                       lease_released_at=str(lease['released_at']), residual_resources=[],
                                       run_type=run_spec.get('type'), lease_release_reason=reason)
        basis.pop('residual_resources')
        return basis
    if not isinstance(result, Mapping) or result.get('schema_version') != 'assignment-result/v1':
        raise SettlementDeferred('assignment_result_unavailable')
    identity = (result.get('assignment_id'), result.get('attempt_no'), result.get('fencing'))
    if identity != (binding['assignment_id'], binding['attempt_no'], binding['fencing']):
        raise SettlementDeferred('assignment_result_identity_mismatch')
    if result.get('status') != attempt.get('status'):
        raise SettlementDeferred('assignment_result_status_mismatch')
    closure = result.get('closure')
    if closure is None:
        # Cleanup 未拿到 journal（例如 preflight blocked 时 artifact_source 为空），
        # 完成回报里没有可比对的 closure 投影。
        raise SettlementDeferred('completion_closure_missing')
    if not isinstance(closure, Mapping) or closure.get('quarantined') is not False:
        raise SettlementDeferred('completion_closure_unavailable')
    if closure.get('assignment') != binding or closure.get('run_id') != row['run_id']:
        raise SettlementDeferred('completion_closure_binding_mismatch')
    if (closure.get('version'), closure.get('record_sha256')) != (row['version'], row['record_sha256']):
        raise SettlementDeferred('journal_changed_after_completion',
                                 f"journal v{row['version']} completion v{closure.get('version')}")
    run_spec = record.get('run_spec') if isinstance(record.get('run_spec'), Mapping) else {}
    run_type = run_spec.get('type')
    run_result = result.get('run_result')
    if isinstance(run_result, Mapping):
        if run_result.get('run_id') != row['run_id']:
            raise SettlementDeferred('run_result_identity_mismatch')
        if run_type in HOST_PUBLISHED_RUN_TYPES:
            raise SettlementDeferred('host_run_result_expected',
                                     f'{run_type} Run Result exists but the journal froze no inputs')
    return {
        'authority': AUTHORITY,
        'assignment': copy.deepcopy(binding),
        'assignment_status': assignment_status,
        'attempt_status': attempt['status'],
        'product_verdict': result.get('product_verdict'),
        'run_type': run_type,
        'run_result_sha256': _digest(_encoded(run_result)) if isinstance(run_result, Mapping) else None,
        'assignment_result_sha256': _digest(_encoded(result)),
        'lease_released_at': str(lease['released_at']),
        'completion_journal': {'version': row['version'], 'record_sha256': row['record_sha256']},
        'product_result_published': False,
        'evidence_complete': False,
    }


def _lease_expired_interrupted(record: Mapping[str, Any], attempt: Mapping[str, Any],
                               lease: Mapping[str, Any]) -> bool:
    """控制面按 Lease 过期回收（BL-076 G1）或按 Agent 中断报告释放（BL-240）、终态 indeterminate 的 Attempt。"""
    return (record.get('status') == 'indeterminate' and attempt.get('status') == 'indeterminate'
            and lease.get('release_reason') in INTERRUPTED_AUTHORITIES)


def _basis(row, read_assignment, authority_error):
    if read_assignment is None:
        raise SettlementDeferred('control_plane_unavailable', authority_error or '')
    try:
        record = read_assignment(row['assignment']['assignment_id'])
    except Exception as exc:  # noqa: BLE001 - unreadable authority defers
        raise SettlementDeferred('assignment_unreadable', f'{type(exc).__name__}: {exc}') from exc
    return terminal_basis(row, record)


def platform_run_root_check(run_root: Path) -> None:
    """``apply`` refreshes ``index.sqlite`` under ``run_root``; a sub-directory would split the index."""
    from autophone.paths import STATE_ROOT
    from autophone.application.run_index import INDEX_NAME

    root = Path(run_root).resolve()
    if root == (STATE_ROOT / 'runs' / 'platform').resolve() or (root / INDEX_NAME).is_file():
        return
    raise SettlementRefused('run_root_not_platform_root',
                            f'{root} is neither the platform run root nor a directory holding {INDEX_NAME}')


def settle_assignment_closures(
    run_root: Path,
    read_assignment: Callable[[str], Mapping[str, Any]] | None,
    *,
    apply: bool = False,
    actor: str | None = None,
    authority_error: str | None = None,
    now: Callable[[], str] | None = None,
    screen_lock_root: Path | None = None,
    spool_root: Path | None = None,
    run_ids: Iterable[str] | None = None,
    assignment_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan (default) or apply settlements for every journal under ``run_root``.

    ``run_ids`` / ``assignment_ids`` (BL-242) restrict the scan to the listed Runs / Assignments;
    a listed value that selects no journal raises ``SettlementRefused('filter_unmatched')``
    before any Control Plane read or write.

    ``read_assignment`` returns the Control Plane Assignment record; ``None`` means no
    authority is readable (``authority_error`` explains why) and every candidate defers.
    An applied settlement refreshes the journal's ``index.sqlite`` closure projection.
    An interrupted settlement (BL-076) carries a read-only residual inventory; ``screen_lock_root``
    and ``spool_root`` locate the ScreenLock state files and Artifact spool (``None``: not checked).
    """
    if apply:
        if not str(actor or '').strip():
            raise ValueError('settlement apply requires an actor')
        platform_run_root_check(run_root)
    now = now or (lambda: datetime.now(timezone.utc).isoformat())
    paths = select_journals(sorted(Path(run_root).rglob('closure-intent.json')),
                            run_ids=run_ids, assignment_ids=assignment_ids)
    rows: list[dict[str, Any]] = []
    for path in paths:
        journal = ClosureJournal(path.parent)
        entry: dict[str, Any] = {'run_dir': str(path.parent)}
        row, skip = _journal_shape(journal)
        if row is None:
            rows.append({**entry, 'action': 'skip', 'reason': skip})
            continue
        entry.update(run_id=row['run_id'], assignment=copy.deepcopy(row['assignment']),
                     journal={'version': row['version'], 'record_sha256': row['record_sha256']})
        try:
            basis = _basis(row, read_assignment, authority_error)
        except SettlementDeferred as exc:
            rows.append(_deferred(entry, exc))
            continue
        entry['basis'] = basis
        interrupted = basis.get('kind') == INTERRUPTED_SETTLEMENT_KIND
        action = 'settle_interrupted' if interrupted else 'settle'
        if interrupted:
            entry['residual_resources'] = _inventory(journal, row, screen_lock_root, spool_root)
        if not apply:
            rows.append({**entry, 'action': action, 'applied': False})
            continue
        try:
            with journal.owner():
                # 读控制面与取 owner 锁之间，同一目录可能被重试的 Attempt 改写：锁内重读
                # journal 与 Assignment，两次依据必须一致才写入。
                current, skip = _journal_shape(journal)
                if current is None or current['record_sha256'] != row['record_sha256']:
                    raise SettlementDeferred('journal_changed_before_settle', skip or 'journal record changed')
                if _basis(current, read_assignment, authority_error) != basis:
                    raise SettlementDeferred('authority_changed_before_settle')
                record = {**basis, 'settled_at': now(), 'settled_by': str(actor)}
                if interrupted:
                    entry['residual_resources'] = _inventory(journal, current, screen_lock_root, spool_root)
                    record['residual_resources'] = copy.deepcopy(entry['residual_resources'])
                settled = journal.settle(record, expected_version=row['version'])
        except SettlementDeferred as exc:
            rows.append(_deferred(entry, exc))
            continue
        except (ClosureConflict, ClosureQuarantined) as exc:
            rows.append(_deferred(entry, SettlementDeferred('journal_conflict', f'{type(exc).__name__}: {exc}')))
            continue
        _refresh_index(path.parent, Path(run_root))
        rows.append({**entry, 'action': action, 'applied': True,
                     'settled_journal': {'version': settled['version'],
                                         'record_sha256': settled['record_sha256']}})
    return rows


def _inventory(journal: ClosureJournal, row: Mapping[str, Any], screen_lock_root, spool_root) -> list[dict[str, Any]]:
    from .interrupted_closure import residual_inventory
    return residual_inventory(journal.root, row, screen_lock_root=screen_lock_root, spool_root=spool_root)


def _deferred(entry: dict[str, Any], exc: SettlementDeferred) -> dict[str, Any]:
    category = 'awaiting_authority' if exc.reason in AWAITING_AUTHORITY_REASONS else 'residual'
    return {**entry, 'action': 'defer', 'reason': exc.reason, 'category': category, 'detail': exc.detail}


def _refresh_index(run_dir: Path, run_root: Path) -> None:
    try:
        from autophone.application import run_index
        run_index.ingest_closure(run_dir, run_root=run_root)
    except Exception:  # noqa: BLE001 - the journal record is authoritative; the index is a projection
        logger.warning('closure index refresh failed: %s', run_dir, exc_info=True)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row['action'] if row['action'] in {'settle', 'settle_interrupted'} else f"{row['action']}:{row['reason']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def residual_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Deferred journals that current closure semantics will never settle, by reason."""
    counts: dict[str, int] = {}
    for row in rows:
        if row['action'] == 'defer' and row.get('category') == 'residual':
            counts[row['reason']] = counts.get(row['reason'], 0) + 1
    return dict(sorted(counts.items()))


__all__ = ['AUTHORITY', 'HOST_PUBLISHED_RUN_TYPES', 'INTERRUPTED_AUTHORITIES', 'INTERRUPTED_AUTHORITY',
           'INTERRUPTED_REPORT_AUTHORITY', 'SETTLEMENT_KIND', 'SettlementDeferred',
           'SettlementRefused', 'residual_summary', 'select_journals', 'settle_assignment_closures', 'summarize',
           'terminal_basis']
