"""PostgreSQL-owned down-edge observation and recovery Assignment coordination."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from ._psycopg import jsonb as Jsonb


class RecoveryCoordinator:
    def __init__(self, store):
        self.store = store

    def observe(self, *, rig_id, generation, down_edge_sequence, edge_id, actor, diagnostics, automatic=True):
        from .postgres import ControlStateError, StaleFencingError
        if not actor or type(down_edge_sequence) is not int or down_edge_sequence < 1 or type(automatic) is not bool:
            raise ControlStateError('recovery observation requires actor and positive edge sequence')
        try:
            edge_id = str(UUID(edge_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ControlStateError('recovery observation requires a valid edge ID') from exc
        key = json.dumps([rig_id, generation, down_edge_sequence, edge_id], separators=(',', ':'))
        recovery_id = 'REC-' + hashlib.sha256(key.encode()).hexdigest()[:32]
        with self.store._connection() as connection:
            rig = connection.execute('SELECT * FROM rigs WHERE rig_id = %s FOR UPDATE', (rig_id,)).fetchone()
            if rig is None or rig['generation'] != generation:
                raise StaleFencingError('recovery observation has stale Rig generation')
            existing = connection.execute('SELECT * FROM recovery_incidents WHERE recovery_id = %s', (recovery_id,)).fetchone()
            if existing is not None:
                return self.store._jsonable(dict(existing))
            if actor == 'vsock-health':
                active = connection.execute(
                    "SELECT * FROM recovery_incidents "
                    "WHERE rig_id = %s AND rig_generation = %s "
                    "AND actor = 'rig-agent' "
                    "AND state IN ('observed', 'abort_requested', 'deferred', 'waiting_lease', 'recovering') "
                    "AND created_at >= now() - interval '60 seconds' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (rig_id, generation),
                ).fetchone()
                if active is not None:
                    return self.store._jsonable(dict(active))
            owner = connection.execute(
                'SELECT * FROM rig_leases WHERE rig_id = %s AND released_at IS NULL ORDER BY created_at DESC LIMIT 1',
                (rig_id,),
            ).fetchone()
            interrupted = owner
            if interrupted is None and rig['status'] == 'quarantine':
                interrupted = connection.execute(
                    "SELECT l.* FROM rig_leases l "
                    "JOIN assignment_attempts a ON a.assignment_id = l.assignment_id AND a.attempt_no = l.attempt_no "
                    "WHERE l.rig_id = %s AND l.rig_generation = %s "
                    "AND NOT EXISTS (SELECT 1 FROM recovery_incidents ri WHERE ri.old_assignment_id = l.assignment_id AND ri.old_attempt_no = l.attempt_no) "
                    "AND ("
                    "    (l.release_reason IN ('lease_expired', 'agent_interrupted') AND a.status = 'indeterminate') "
                    "    OR (COALESCE(a.result->>'rig_disposition', '') = 'quarantine' AND l.released_at >= now() - interval '120 seconds')"
                    ") "
                    "ORDER BY l.released_at DESC NULLS LAST LIMIT 1",
                    (rig_id, generation),
                ).fetchone()
            state = 'observed'
            if owner:
                request = {'actor': actor, 'reason': f'vphone_down:{recovery_id}',
                           'requested_at': datetime.now(timezone.utc).isoformat()}
                connection.execute("UPDATE assignments SET status = 'cancel_requested', cancel_request = %s, updated_at = now() "
                                   "WHERE assignment_id = %s AND status IN ('assigned','running','cancel_requested')",
                                   (Jsonb(request), owner['assignment_id']))
                connection.execute("UPDATE rig_leases SET expires_at = LEAST(expires_at, now() + interval '30 seconds') WHERE lease_id = %s",
                                   (owner['lease_id'],))
                state = 'abort_requested'
                owner_assignment = connection.execute('SELECT kind FROM assignments WHERE assignment_id = %s',
                                                      (owner['assignment_id'],)).fetchone()
                if owner_assignment and owner_assignment['kind'] == 'recovery':
                    state = 'quarantined'
            connection.execute("UPDATE rigs SET status = 'quarantine' WHERE rig_id = %s", (rig_id,))
            row = connection.execute(
                'INSERT INTO recovery_incidents(recovery_id, rig_id, rig_generation, down_edge_sequence, edge_id, state, actor, diagnostics, old_assignment_id, old_attempt_no, automatic) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (recovery_id, rig_id, generation, down_edge_sequence, edge_id, state, actor, Jsonb(diagnostics),
                 interrupted['assignment_id'] if interrupted else None,
                 interrupted['attempt_no'] if interrupted else None,
                 automatic),
            ).fetchone()
            self.store._insert_host_event(connection, host_id=rig['host_id'], rig_id=rig_id,
                                          kind='recovery_observed', actor=actor,
                                          details={'recovery_id': recovery_id, 'generation': generation,
                                                   'old_assignment_id': row['old_assignment_id'], 'state': state})
            return self.store._jsonable(dict(row))

    def get(self, recovery_id):
        from .postgres import ControlStateError
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM recovery_incidents WHERE recovery_id = %s', (recovery_id,)).fetchone()
            if row is None:
                raise ControlStateError('recovery not found')
            return self.store._jsonable(dict(row))

    def resume(self, recovery_id, *, retry=False):
        from .postgres import ControlStateError, StaleFencingError
        self.store.reap_expired_leases()
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM recovery_incidents WHERE recovery_id = %s FOR UPDATE', (recovery_id,)).fetchone()
            if row is None:
                raise ControlStateError('recovery not found')
            if row['state'] in {'recovered', 'isolation_confirmed', 'recovering'}:
                return self.store._jsonable(dict(row))
            if row['state'] == 'quarantined' and not retry:
                raise ControlStateError('quarantined recovery requires explicit retry')
            rig = connection.execute('SELECT * FROM rigs WHERE rig_id = %s', (row['rig_id'],)).fetchone()
            if rig['generation'] != row['rig_generation']:
                raise StaleFencingError('recovery Rig generation changed')
            owner = connection.execute('SELECT 1 FROM rig_leases WHERE rig_id = %s AND released_at IS NULL', (row['rig_id'],)).fetchone()
            if owner:
                connection.execute("UPDATE recovery_incidents SET state = 'deferred', automatic = true, retry_authorized = retry_authorized OR %s, version = version + 1, updated_at = now() WHERE recovery_id = %s", (retry, recovery_id))
                return {**self.store._jsonable(dict(row)), 'state': 'deferred', 'version': row['version'] + 1}
            assignment_id = row['recovery_assignment_id'] or f'A-{recovery_id}'
            connection.execute("UPDATE recovery_incidents SET state = 'waiting_lease', automatic = true, retry_authorized = retry_authorized OR %s, recovery_assignment_id = %s, version = version + 1, updated_at = now() WHERE recovery_id = %s",
                               (retry, assignment_id, recovery_id))
        plan = {'assignment_id': assignment_id, 'kind': 'recovery', 'requires_rig': True,
                'priority': 'high', 'target_rig_id': row['rig_id'],
                'desired_state': {'recovery_id': recovery_id, 'generation': row['rig_generation']}}
        with self.store._connection() as connection:
            interrupted = connection.execute(
                "SELECT a.assignment_id, a.attempt_no, a.fencing, a.rig_id, a.rig_generation AS generation, r.run_id "
                "FROM assignment_attempts a JOIN runs r ON r.assignment_id = a.assignment_id "
                "WHERE a.assignment_id = %s AND a.attempt_no = %s "
                "AND a.rig_id = %s AND a.rig_generation = %s "
                "AND a.status = 'indeterminate'",
                (row['old_assignment_id'], row['old_attempt_no'], row['rig_id'], row['rig_generation']),
            ).fetchone()
        if interrupted:
            plan['desired_state']['interrupted_attempt'] = dict(interrupted)
        assignment = self.store.submit_assignment(plan, assignment_id)
        if (retry or row['retry_authorized']) and assignment['status'] in {
            'passed', 'error', 'failed', 'cancelled', 'indeterminate', 'blocked'
        }:
            with self.store._connection() as connection:
                connection.execute(
                    'UPDATE assignments SET desired_state = %s, updated_at = now() WHERE assignment_id = %s',
                    (Jsonb(plan['desired_state']), assignment_id),
                )
            self.store.retry_assignment(assignment_id)
        return self.get(recovery_id)

    def pending(self):
        with self.store._connection() as connection:
            rows = connection.execute("SELECT recovery_id FROM recovery_incidents WHERE automatic AND state IN ('observed','abort_requested','deferred','waiting_lease') ORDER BY created_at").fetchall()
        return [row['recovery_id'] for row in rows]

    def confirm_isolation(self, recovery_id, *, actor, reason):
        from .postgres import ControlStateError, StaleFencingError
        if not actor or not reason:
            raise ControlStateError('isolation confirmation requires actor and reason')
        self.store.reap_expired_leases()
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM recovery_incidents WHERE recovery_id = %s FOR UPDATE',
                                     (recovery_id,)).fetchone()
            if row is None:
                raise ControlStateError('recovery not found')
            rig = connection.execute('SELECT * FROM rigs WHERE rig_id = %s FOR UPDATE', (row['rig_id'],)).fetchone()
            if rig['generation'] != row['rig_generation']:
                raise StaleFencingError('isolation confirmation has stale Rig generation')
            if row['state'] == 'isolation_confirmed':
                return self.store._jsonable(dict(row))
            if row['state'] != 'quarantined':
                raise ControlStateError('isolation confirmation requires quarantined recovery')
            owner = connection.execute('SELECT 1 FROM rig_leases WHERE rig_id = %s AND released_at IS NULL',
                                       (row['rig_id'],)).fetchone()
            if owner:
                raise ControlStateError('isolation confirmation requires released or expired Lease')
            receipt = {'actor': actor, 'reason': reason, 'generation': rig['generation'],
                       'rig_disposition': 'quarantine', 'confirmed_at': datetime.now(timezone.utc).isoformat()}
            connection.execute("UPDATE rigs SET status = 'quarantine' WHERE rig_id = %s", (row['rig_id'],))
            updated = connection.execute("UPDATE recovery_incidents SET state = 'isolation_confirmed', isolation_receipt = %s, version = version + 1, updated_at = now() WHERE recovery_id = %s RETURNING *",
                                         (Jsonb(receipt), recovery_id)).fetchone()
            self.store._insert_host_event(connection, host_id=rig['host_id'], rig_id=row['rig_id'],
                                          kind='recovery_isolation_confirmed', actor=actor,
                                          details={'recovery_id': recovery_id, **receipt})
            return self.store._jsonable(dict(updated))


def authorized_recovery(connection, assignment, rig):
    if assignment['kind'] != 'recovery':
        return False
    from .interrupted_closure import authorized_closure, is_closure_assignment
    if is_closure_assignment(assignment):
        return authorized_closure(connection, assignment, rig)
    row = connection.execute("SELECT 1 FROM recovery_incidents WHERE recovery_assignment_id = %s AND rig_id = %s AND rig_generation = %s AND state = 'waiting_lease'",
                             (assignment['assignment_id'], rig['rig_id'], rig['generation'])).fetchone()
    return row is not None


def bind_recovery_attempt(connection, assignment_id, attempt_no, fencing):
    from .interrupted_closure import bind_closure_attempt
    bind_closure_attempt(connection, assignment_id, attempt_no, fencing)
    connection.execute("UPDATE recovery_incidents SET state = 'recovering', retry_authorized = false, recovery_attempt_no = %s, recovery_fencing = %s, version = version + 1, updated_at = now() WHERE recovery_assignment_id = %s AND state = 'waiting_lease'",
                       (attempt_no, fencing, assignment_id))


def complete_recovery(connection, store, assignment_id, attempt_no, fencing, lease, result):
    from .interrupted_closure import complete_closure, summaries
    from .postgres import ControlStateError
    if complete_closure(connection, store, assignment_id, attempt_no, fencing, lease, result):
        return
    row = connection.execute('SELECT * FROM recovery_incidents WHERE recovery_assignment_id = %s FOR UPDATE',
                             (assignment_id,)).fetchone()
    if row is None:
        return
    if (row['state'], row['recovery_attempt_no'], row['recovery_fencing']) != ('recovering', attempt_no, fencing):
        raise ControlStateError('recovery completion authority mismatch')
    receipt = result.get('recovery_receipt') or {}
    if (receipt.get('recovery_id'), receipt.get('rig_id'), receipt.get('generation')) != (
        row['recovery_id'], lease['rig_id'], lease['rig_generation']
    ):
        raise ControlStateError('recovery receipt identity mismatch')
    probe = receipt.get('readiness') or {}
    recovered = result.get('status') == 'passed'
    if recovered and (probe.get('available') is not True or probe.get('state') != 'guest_ready'):
        raise ControlStateError('recovery requires independent guest readiness')
    if recovered and result.get('rig_disposition') != 'reusable':
        raise ControlStateError('successful recovery requires reusable Rig disposition')
    guest_ready = (
        result.get('status') == 'passed'
        and probe.get('available') is True
        and probe.get('state') == 'guest_ready'
    )
    # Guest readiness alone cannot discharge an interrupted Run's resource closure.
    old_run = (
        connection.execute(
            "SELECT r.run_id FROM runs r "
            "JOIN assignment_attempts a ON a.assignment_id = r.assignment_id AND a.attempt_no = %s "
            "WHERE r.assignment_id = %s AND a.status = 'indeterminate'",
            (row['old_attempt_no'], row['old_assignment_id']),
        ).fetchone()
        if row['old_assignment_id'] and row['old_attempt_no'] is not None
        else None
    )
    if recovered and old_run:
        recovered = False
        receipt = {**receipt, 'closure_recovery': 'required', 'interrupted_run_id': old_run['run_id']}
    # BL-076：Lease 过期中断且未闭包的 Attempt 由 incident 关闭后的 `rig recover` 处置；在此之前保持隔离。
    pending = connection.execute(
        "SELECT * FROM interrupted_attempt_closures WHERE rig_id = %s AND rig_generation = %s AND state <> 'closed' "
        'ORDER BY created_at, assignment_id, attempt_no', (row['rig_id'], row['rig_generation'])).fetchall()
    interrupted_closure = None
    if pending:
        interrupted_closure = {'status': 'required', 'attempts': summaries([dict(item) for item in pending])}
        if recovered:
            recovered = False
            receipt = {**receipt, 'interrupted_closure': 'required'}
    state = 'recovered' if recovered else 'quarantined'
    diagnostics_patch = Jsonb({} if guest_ready else {'guest_unresponsive': True})
    connection.execute(
        "UPDATE recovery_incidents "
        "SET state = %s, diagnostics = COALESCE(diagnostics, '{}'::jsonb) || %s, version = version + 1, updated_at = now() "
        "WHERE recovery_id = %s",
        (state, diagnostics_patch, row['recovery_id']),
    )
    other = connection.execute("SELECT 1 FROM recovery_incidents WHERE rig_id = %s AND recovery_id <> %s AND state NOT IN ('recovered','isolation_confirmed')",
                               (row['rig_id'], row['recovery_id'])).fetchone()
    connection.execute('UPDATE rigs SET status = %s WHERE rig_id = %s',
                       ('online' if recovered and not other else 'quarantine', row['rig_id']))
    rig = connection.execute('SELECT host_id FROM rigs WHERE rig_id = %s', (row['rig_id'],)).fetchone()
    connection.execute('INSERT INTO rig_recovery_receipts(receipt_id, rig_id, rig_generation, actor, probe_result, decision, interrupted_closure) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                       (f"RR-{row['recovery_id']}-{attempt_no}", row['rig_id'], row['rig_generation'], 'recovery-assignment',
                        Jsonb(receipt), 'recovered' if recovered else 'remain_quarantined',
                        Jsonb(interrupted_closure) if interrupted_closure is not None else None))
    store._insert_host_event(connection, host_id=rig['host_id'], rig_id=row['rig_id'], kind=f'recovery_{state}',
                             actor='recovery-assignment', details={'recovery_id': row['recovery_id'],
                                                                  'assignment_id': assignment_id, 'attempt_no': attempt_no,
                                                                  'generation': row['rig_generation']})
    if not guest_ready:
        store._evaluate_breaker(connection, str(rig['host_id']))
