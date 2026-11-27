"""Control State for the resource closure of Lease-expired Attempts (BL-076 G1～G3).

Lease 回收（``reap_expired_leases``）把被中断的非恢复 Attempt 登记为 ``pending``；Agent 重连时报告
本地只有「开始」记录的 Attempt（``report_interrupted_attempt``，BL-240）走同一登记。``rig recover``
据此关联被中断 Attempt：Probe Result 通过时创建一个闭包恢复 Assignment（``kind='recovery'``、
``desired_state.mode='interrupted_closure'``），由 Rig Agent 以新的 Lease 与 Fencing 执行；完成回执
逐 Attempt 记录 journal 结算、残留资源清单与 App 终止回读。这里只写新表、新回执行与新 Assignment，
不改写被中断 Attempt、Assignment 与 ``runs`` 行的终态和结果。

所有函数都在调用方的事务内执行（传入 ``connection``），由调用方持有 ``rigs`` 行锁或 Lease 锁。
"""
from __future__ import annotations

import uuid
from typing import Any, Mapping, Sequence

from ._psycopg import jsonb as Jsonb

CLOSURE_MODE = 'interrupted_closure'
_IDENTITY = ('assignment_id', 'attempt_no', 'fencing', 'rig_id', 'generation')
_TERMINAL = ('passed', 'failed', 'error', 'blocked', 'cancelled', 'indeterminate')


def is_closure_assignment(assignment: Mapping[str, Any]) -> bool:
    return (assignment.get('kind') == 'recovery'
            and (assignment.get('desired_state') or {}).get('mode') == CLOSURE_MODE)


def register_interrupted(connection, assignment_id: str, attempt_no: int) -> None:
    """Lease 过期回收同一事务内登记被中断的非恢复 Attempt；重复回收不重复登记。"""
    connection.execute(
        """
        INSERT INTO interrupted_attempt_closures(
            assignment_id, attempt_no, rig_id, rig_generation, fencing, run_id, state
        )
        SELECT a.assignment_id, a.attempt_no, a.rig_id, a.rig_generation, a.fencing, r.run_id, 'pending'
        FROM assignment_attempts a LEFT JOIN runs r ON r.assignment_id = a.assignment_id
        WHERE a.assignment_id = %s AND a.attempt_no = %s
        ON CONFLICT (assignment_id, attempt_no) DO NOTHING
        """,
        (assignment_id, attempt_no),
    )


def fail_expired_closure(connection, assignment_id: str, attempt_no: int,
                         reason: str = 'closure_lease_expired') -> None:
    """闭包恢复 Assignment 自身的 Lease 过期或被 Agent 报告中断（BL-240，``closure_agent_interrupted``）：
    它负责的行置为 failed，等待下一次 ``rig recover``。"""
    connection.execute(
        """
        UPDATE interrupted_attempt_closures
        SET state = 'failed', receipt = %s, version = version + 1, updated_at = now()
        WHERE closure_assignment_id = %s AND closure_attempt_no = %s AND state = 'closing'
        """,
        (Jsonb({'reason': reason, 'closure_assignment_id': assignment_id,
                'closure_attempt_no': attempt_no}), assignment_id, attempt_no),
    )


def _attempt_rows(connection, rig_id: str, generation: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT c.*, a.status AS closure_assignment_status
        FROM interrupted_attempt_closures c
        LEFT JOIN assignments a ON a.assignment_id = c.closure_assignment_id
        WHERE c.rig_id = %s AND c.rig_generation = %s AND c.state <> 'closed'
        ORDER BY c.created_at, c.assignment_id, c.attempt_no
        FOR UPDATE OF c
        """,
        (rig_id, generation),
    ).fetchall()
    return [dict(row) for row in rows]


def open_closures(connection, rig_id: str, generation: int) -> list[dict[str, Any]]:
    """该 Rig 当前 Generation 下未闭包的行（加行锁）。

    闭包恢复 Assignment 已到终态却没有完成回执（例如排队时被取消）的行先改为 failed，
    以免 ``rig recover`` 永久判为进行中。
    """
    rows = _attempt_rows(connection, rig_id, generation)
    for row in rows:
        if row['state'] in {'waiting_lease', 'closing'} and row['closure_assignment_status'] in _TERMINAL:
            connection.execute(
                """
                UPDATE interrupted_attempt_closures
                SET state = 'failed', receipt = %s, version = version + 1, updated_at = now()
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (Jsonb({'reason': f"closure_assignment_{row['closure_assignment_status']}",
                        'closure_assignment_id': row['closure_assignment_id']}),
                 row['assignment_id'], row['attempt_no']),
            )
            row['state'] = 'failed'
    return rows


def summaries(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{'assignment_id': row['assignment_id'], 'attempt_no': row['attempt_no'], 'fencing': row['fencing'],
             'rig_id': row['rig_id'], 'generation': row['rig_generation'], 'run_id': row['run_id'],
             'state': row['state']} for row in rows]


def request_closure(connection, store, rig: Mapping[str, Any], actor: str,
                    rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """创建闭包恢复 Assignment 并把行置为 waiting_lease；同一事务内完成。"""
    assignment_id = f'A-IAC-{uuid.uuid4().hex}'
    attempts = []
    for row in rows:
        detail = connection.execute(
            """
            SELECT a.run_spec, a.desired_state, l.released_at, l.release_reason
            FROM assignments a
            LEFT JOIN rig_leases l ON l.assignment_id = a.assignment_id AND l.attempt_no = %s
            WHERE a.assignment_id = %s
            ORDER BY l.created_at DESC NULLS LAST LIMIT 1
            """,
            (row['attempt_no'], row['assignment_id']),
        ).fetchone()
        run_spec = (detail or {}).get('run_spec') or {}
        desired = (detail or {}).get('desired_state') or {}
        released = (detail or {}).get('released_at')
        attempts.append({
            'assignment_id': row['assignment_id'], 'attempt_no': int(row['attempt_no']),
            'fencing': int(row['fencing']), 'rig_id': row['rig_id'], 'generation': int(row['rig_generation']),
            'run_id': row['run_id'], 'run_type': run_spec.get('type'),
            'app': run_spec.get('app') or desired.get('app'),
            'lease_released_at': released.isoformat() if released is not None else None,
            # `lease_expired`（回收）或 `agent_interrupted`（Agent 重连时报告，BL-240）。
            'lease_release_reason': (detail or {}).get('release_reason'),
        })
    desired_state = {'mode': CLOSURE_MODE, 'generation': int(rig['generation']), 'requested_by': actor,
                     'interrupted_attempts': attempts}
    connection.execute(
        """
        INSERT INTO assignments(
            assignment_id, idempotency_key, kind, priority, requires_rig, target_rig_id,
            required_capabilities, desired_state, reset_policy, status
        ) VALUES (%s, %s, 'recovery', 'high', true, %s, '[]'::jsonb, %s, '{}'::jsonb, 'queued')
        """,
        (assignment_id, assignment_id, rig['rig_id'], Jsonb(desired_state)),
    )
    for row in rows:
        connection.execute(
            """
            UPDATE interrupted_attempt_closures
            SET state = 'waiting_lease', closure_assignment_id = %s, closure_attempt_no = NULL,
                closure_fencing = NULL, version = version + 1, updated_at = now()
            WHERE assignment_id = %s AND attempt_no = %s
            """,
            (assignment_id, row['assignment_id'], row['attempt_no']),
        )
    store._insert_host_event(connection, host_id=str(rig['host_id']), rig_id=str(rig['rig_id']),
                             kind='interrupted_closure_requested', actor=actor,
                             details={'closure_assignment_id': assignment_id, 'generation': int(rig['generation']),
                                      'attempts': [{key: item[key] for key in ('assignment_id', 'attempt_no', 'fencing')}
                                                   for item in attempts]})
    return {'status': 'requested', 'assignment_id': assignment_id,
            'attempts': [{**item, 'state': 'waiting_lease'} for item in attempts]}


def authorized_closure(connection, assignment: Mapping[str, Any], rig: Mapping[str, Any]) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM interrupted_attempt_closures
        WHERE closure_assignment_id = %s AND rig_id = %s AND rig_generation = %s AND state = 'waiting_lease'
        LIMIT 1
        """,
        (assignment['assignment_id'], rig['rig_id'], rig['generation']),
    ).fetchone()
    return row is not None


def bind_closure_attempt(connection, assignment_id: str, attempt_no: int, fencing: int) -> None:
    connection.execute(
        """
        UPDATE interrupted_attempt_closures
        SET state = 'closing', closure_attempt_no = %s, closure_fencing = %s, version = version + 1,
            updated_at = now()
        WHERE closure_assignment_id = %s AND state = 'waiting_lease'
        """,
        (attempt_no, fencing, assignment_id),
    )


def _entry_identity(entry: Mapping[str, Any]) -> tuple:
    attempt = entry.get('interrupted_attempt') or {}
    return tuple(attempt.get(key) for key in _IDENTITY)


def complete_closure(connection, store, assignment_id: str, attempt_no: int, fencing: int,
                     lease: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """记录闭包恢复 Assignment 的完成；不是闭包恢复 Assignment 时返回 False。"""
    from autophone.contracts import ContractError, validate_or_raise
    from .postgres import ControlStateError

    rows = connection.execute(
        'SELECT * FROM interrupted_attempt_closures WHERE closure_assignment_id = %s '
        'ORDER BY assignment_id, attempt_no FOR UPDATE',
        (assignment_id,),
    ).fetchall()
    if not rows:
        return False
    if any((row['state'], row['closure_attempt_no'], row['closure_fencing']) != ('closing', attempt_no, fencing)
           for row in rows):
        raise ControlStateError('interrupted closure completion authority mismatch')
    receipt = result.get('recovery_receipt') or {}
    if (receipt.get('mode'), receipt.get('rig_id'), receipt.get('generation')) != (
        CLOSURE_MODE, lease['rig_id'], lease['rig_generation']
    ):
        raise ControlStateError('interrupted closure receipt identity mismatch')
    entries = receipt.get('closures')
    if not isinstance(entries, list):
        raise ControlStateError('interrupted closure receipt has no closure entries')
    for entry in entries:
        try:
            validate_or_raise('interrupted-attempt-closure.v1.json', entry)
        except ContractError as exc:
            raise ControlStateError(f'interrupted closure entry invalid: {exc}') from exc
    expected = {(row['assignment_id'], row['attempt_no'], row['fencing'], row['rig_id'], row['rig_generation'])
                for row in rows}
    actual = [_entry_identity(entry) for entry in entries]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ControlStateError('interrupted closure entries do not match the requested Attempts')
    all_closed = all(entry['closure_state'] == 'closed' for entry in entries)
    readiness = receipt.get('readiness') or {}
    if result.get('status') == 'passed':
        if not all_closed:
            raise ControlStateError('interrupted closure passed with unclosed Attempts')
        if readiness.get('available') is not True or readiness.get('state') != 'guest_ready':
            raise ControlStateError('interrupted closure requires independent guest readiness')
    recovered = result.get('status') == 'passed' and result.get('rig_disposition') == 'reusable' and all_closed
    by_identity = {_entry_identity(entry): entry for entry in entries}
    for row in rows:
        entry = by_identity[(row['assignment_id'], row['attempt_no'], row['fencing'], row['rig_id'],
                             row['rig_generation'])]
        connection.execute(
            """
            UPDATE interrupted_attempt_closures
            SET state = %s, receipt = %s, version = version + 1, updated_at = now()
            WHERE assignment_id = %s AND attempt_no = %s
            """,
            ('closed' if entry['closure_state'] == 'closed' else 'failed', Jsonb(entry),
             row['assignment_id'], row['attempt_no']),
        )
    rig_id = lease['rig_id']
    generation = lease['rig_generation']
    incident = connection.execute(
        "SELECT 1 FROM recovery_incidents WHERE rig_id = %s AND state NOT IN ('recovered','isolation_confirmed')",
        (rig_id,),
    ).fetchone()
    remaining = connection.execute(
        "SELECT 1 FROM interrupted_attempt_closures WHERE rig_id = %s AND rig_generation = %s AND state <> 'closed'",
        (rig_id, generation),
    ).fetchone()
    online = recovered and incident is None and remaining is None
    connection.execute('UPDATE rigs SET status = %s WHERE rig_id = %s', ('online' if online else 'quarantine', rig_id))
    closure = {'status': 'closed' if all_closed else 'failed', 'assignment_id': assignment_id,
               'attempt_no': attempt_no, 'fencing': fencing, 'attempts': entries}
    connection.execute(
        """
        INSERT INTO rig_recovery_receipts(
            receipt_id, rig_id, rig_generation, actor, probe_result, decision, interrupted_closure
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (f'RR-IAC-{assignment_id}-{attempt_no}', rig_id, generation, 'interrupted-closure-assignment',
         Jsonb({'mode': CLOSURE_MODE, 'readiness': readiness, 'status': result.get('status'),
                'rig_disposition': result.get('rig_disposition')}),
         'recovered' if online else 'remain_quarantined', Jsonb(closure)),
    )
    rig = connection.execute('SELECT host_id FROM rigs WHERE rig_id = %s', (rig_id,)).fetchone()
    store._insert_host_event(connection, host_id=str(rig['host_id']), rig_id=rig_id,
                             kind='interrupted_closure_closed' if all_closed else 'interrupted_closure_failed',
                             actor='interrupted-closure-assignment',
                             details={'closure_assignment_id': assignment_id, 'attempt_no': attempt_no,
                                      'fencing': fencing, 'rig_online': online,
                                      'attempts': [{'assignment_id': entry['interrupted_attempt']['assignment_id'],
                                                    'attempt_no': entry['interrupted_attempt']['attempt_no'],
                                                    'closure_state': entry['closure_state']} for entry in entries]})
    return True


__all__ = ['CLOSURE_MODE', 'authorized_closure', 'bind_closure_attempt', 'complete_closure',
           'fail_expired_closure', 'is_closure_assignment', 'open_closures', 'register_interrupted',
           'request_closure', 'summaries']
