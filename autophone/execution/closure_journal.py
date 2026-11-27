"""Versioned Run closure inputs and crash-safe terminal-result publication.

This journal owns no Rig lease or resource disposition. Only explicit callers
freeze product inputs; a successful finalization never means reset succeeded.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from autophone.contracts import ContractError, validate_or_raise

_BOUND_ASSIGNMENT: ContextVar[tuple[Path, str] | None] = ContextVar('closure_assignment', default=None)


class ClosureConflict(RuntimeError):
    pass


class ClosureQuarantined(RuntimeError):
    pass


class ClosureSettled(ClosureConflict):
    """The journal was settled by Assignment terminal authority; nothing is left to collect."""


#: `frozen_inputs.kind` of a journal settled by the Control Plane Assignment terminal record
#: (BL-194). Such a journal holds no product inputs and never publishes a Run Result.
SETTLEMENT_KIND = 'assignment-terminal-settlement/v1'

#: `frozen_inputs.kind` of a journal whose Attempt lost its Lease (`lease_expired`) and ended
#: `indeterminate` without an Assignment Result (BL-076 G1). It records the interrupted, unclosed
#: resource state and never publishes a Run Result.
INTERRUPTED_SETTLEMENT_KIND = 'interrupted-attempt-settlement/v1'

_SETTLEMENT_KINDS = frozenset({SETTLEMENT_KIND, INTERRUPTED_SETTLEMENT_KIND})


def is_settled(row) -> bool:
    return (row.get('frozen_inputs') or {}).get('kind') in _SETTLEMENT_KINDS


def is_interrupted(row) -> bool:
    return (row.get('frozen_inputs') or {}).get('kind') == INTERRUPTED_SETTLEMENT_KIND


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class ClosureJournal:
    def __init__(self, run_dir: Path, *, crash_point=lambda point: None):
        self.root = Path(run_dir)
        self.path = self.root / 'closure-intent.json'
        self.quarantine = self.root / 'closure-intent.quarantine.json'
        self.result_path = self.root / 'platform-run-result.json'
        self.crash_point = crash_point

    @contextmanager
    def owner(self):
        """Exclude live execution and other finalizers; process death releases ownership."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.closure-owner.lock').open('a+b') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ClosureConflict('closure has an active owner') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def bind_assignment(self, run_id: str):
        token = _BOUND_ASSIGNMENT.set((self.root.resolve(), run_id))
        try:
            yield self.root
        finally:
            _BOUND_ASSIGNMENT.reset(token)

    @staticmethod
    def bound_directory(run_id: str) -> Path | None:
        bound = _BOUND_ASSIGNMENT.get()
        return bound[0] if bound is not None and bound[1] == run_id else None

    def is_assignment_bound(self, run_id: str) -> bool:
        return self.bound_directory(run_id) == self.root.resolve()

    @contextmanager
    def execution_owner(self, run_id: str):
        if self.is_assignment_bound(run_id):
            yield
        else:
            with self.owner():
                yield

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.closure-intent.lock').open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                if self.quarantine.exists():
                    raise ClosureQuarantined(str(self.quarantine))
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _atomic(self, path, data):
        fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def _isolate(self, reason):
        self._atomic(self.quarantine, _encoded({'reason': reason}))
        raise ClosureQuarantined(reason)

    def _validate(self, row):
        validate_or_raise('closure-intent.v1.json', row)
        if row.get('reset_receipt') is not None:
            from autophone.reset_contract import validate_reset_receipt
            validate_reset_receipt(row['reset_receipt'])
        if row.get('world_identity') is not None:
            world = row['world_identity']
            validate_or_raise('run-world-identity.v0.json', world)
            if world['run_id'] != row['run_id']:
                raise ValueError('world Run identity mismatch')
            if world['assignment_binding'] != row.get('assignment'):
                raise ValueError('world Assignment binding mismatch')
            from .world_identity import digest
            if world['record_sha256'] != digest({k: v for k, v in world.items() if k != 'record_sha256'}):
                raise ValueError('world identity checksum mismatch')
        unsigned = {k: v for k, v in row.items() if k != 'record_sha256'}
        if row['record_sha256'] != _digest(_encoded(unsigned)):
            raise ValueError('journal checksum mismatch')
        if (row['closure_state'] == 'frozen') != (row['frozen_inputs'] is not None):
            raise ValueError('frozen input state mismatch')
        candidate = row['terminal_candidate']
        if candidate is None:
            if row['terminal_sha256'] is not None or row['finalization_state'] != 'pending':
                raise ValueError('terminal candidate missing')
        else:
            validate_or_raise('run-result.v1.json', candidate)
            if candidate['run_id'] != row['run_id']:
                raise ValueError('terminal run identity mismatch')
            if candidate['device'] != row['device']:
                raise ValueError('terminal device identity mismatch')
            if row.get('world_identity') is not None and candidate.get('world_identity') != row['world_identity']:
                raise ValueError('terminal world identity mismatch')
            collect = (candidate.get('lock_phase') or {}).get('collect') or {}
            if collect.get('status') not in {'ok', 'failed', 'skipped'}:
                raise ValueError('terminal collect status missing')
            if row['closure_state'] != 'frozen' or row['finalization_state'] == 'pending':
                raise ValueError('terminal state mismatch')
            if row['terminal_sha256'] != _digest(_encoded(candidate)):
                raise ValueError('terminal candidate checksum mismatch')

    def inspect_readonly(self):
        """Validate the journal without locks, quarantine or any other write (BL-194 dry run).

        Raises ``ValueError`` when the journal is missing or invalid; unlike ``read`` it never
        isolates the journal, so a read-only caller leaves the directory byte-for-byte unchanged.
        """
        try:
            row = json.loads(self.path.read_bytes())
            self._validate(row)
            for field, name in (('world_identity', 'world-identity.json'), ('reset_receipt', 'reset-receipt.json')):
                path = self.root / name
                if row.get(field) is not None and path.exists() and path.read_bytes() != _encoded(row[field]):
                    raise ValueError(f'{name} differs from frozen journal')
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, ContractError) as exc:
            raise ValueError(f'journal invalid: {exc}') from exc
        return row

    def _read(self):
        try:
            row = json.loads(self.path.read_bytes())
            self._validate(row)
            for field, name in (('world_identity', 'world-identity.json'), ('reset_receipt', 'reset-receipt.json')):
                path = self.root / name
                if row.get(field) is not None and path.exists() and path.read_bytes() != _encoded(row[field]):
                    raise ValueError(f'{name} differs from frozen journal')
        except (ValueError, TypeError, KeyError, UnicodeError, ContractError) as exc:
            self._isolate(f'journal invalid: {exc}')
        return row

    def materialize_evidence(self):
        """Rebuild missing derivative receipts; preserve and isolate different bytes."""
        with self._locked():
            row = self._read()
            for field, name in (('world_identity', 'world-identity.json'), ('reset_receipt', 'reset-receipt.json')):
                if row.get(field) is not None and not (self.root / name).exists():
                    self._atomic(self.root / name, _encoded(row[field]))

    def _store(self, row):
        row = copy.deepcopy(row)
        row.pop('record_sha256', None)
        row['record_sha256'] = _digest(_encoded(row))
        self._validate(row)
        self._atomic(self.path, _encoded(row))
        return copy.deepcopy(row)

    @staticmethod
    def _cas(row, expected_version):
        if type(expected_version) is not int or row['version'] != expected_version:
            raise ClosureConflict('closure version conflict')

    def open(self, *, run_id: str, device: str, assignment: dict | None = None):
        with self._locked():
            if self.path.exists() or self.result_path.exists():
                raise ClosureConflict('closure or terminal result already exists')
            row = dict(schema_version='closure-intent/v1', run_id=run_id, device=device,
                       version=0, closure_state='open', recovery_state='not_required',
                       finalization_state='pending', frozen_inputs=None,
                       terminal_candidate=None, terminal_sha256=None,
                       expected_reset_axes=['device', 'fixture'])
            if assignment is not None:
                row['assignment'] = copy.deepcopy(assignment)
            return self._store(row)

    def read(self):
        with self._locked():
            return copy.deepcopy(self._read())

    def quarantine_inputs(self, reason: str):
        with self._locked():
            self._isolate(reason)

    def projection(self):
        """Expose lifecycle state without query identities or raw response data."""
        if not self.path.exists() and not self.quarantine.exists():
            return None
        try:
            row = self.read()
        except ClosureQuarantined:
            return {'quarantined': True}
        projection = {**{key: row[key] for key in ('run_id', 'device', 'version', 'closure_state', 'recovery_state',
                                            'finalization_state', 'record_sha256')},
                'quarantined': False}
        if row.get('assignment') is not None:
            projection['assignment'] = copy.deepcopy(row['assignment'])
        if row.get('world_identity') is not None:
            projection['world_identity_sha256'] = row['world_identity']['record_sha256']
        if row.get('reset_receipt') is not None:
            projection['reset_receipt'] = copy.deepcopy(row['reset_receipt'])
            projection['closure_completion'] = ('complete' if row['finalization_state'] == 'complete' else 'pending')
        if is_settled(row):
            # BL-194: settled on the Assignment terminal record; no product result will follow.
            # BL-076: an interrupted Attempt is settled as unclosed, with its residual resources.
            settlement = row['frozen_inputs']
            projection['settlement'] = {key: settlement.get(key) for key in (
                'authority', 'assignment_status', 'product_verdict', 'settled_at')}
            if is_interrupted(row):
                projection['closure_completion'] = 'interrupted_unclosed'
                projection['settlement']['closure_outcome'] = settlement.get('closure_outcome')
                projection['settlement']['residual_resource_kinds'] = sorted(
                    str(item.get('kind')) for item in settlement.get('residual_resources') or [])
            else:
                projection['closure_completion'] = 'settled'
        return projection

    def freeze(self, inputs: dict, *, expected_version: int):
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row['closure_state'] != 'open':
                raise ClosureConflict('closure inputs already frozen')
            row.update(frozen_inputs=copy.deepcopy(inputs), closure_state='frozen',
                       version=row['version'] + 1)
            return self._store(row)

    def settle(self, record: dict, *, expected_version: int):
        """Close an input-less Assignment journal on the Assignment terminal record (BL-194).

        Only an open journal that is bound to an Assignment, froze no product inputs and
        published no Run Result qualifies. The caller verifies the terminal authority;
        this method records it once under CAS and never writes a Run Result.
        ``kind=INTERRUPTED_SETTLEMENT_KIND`` records a Lease-expired, ``indeterminate``
        Attempt as interrupted and unclosed (BL-076 G1) under the same rules.
        """
        kind = record.get('kind') or SETTLEMENT_KIND
        if kind not in _SETTLEMENT_KINDS:
            raise ValueError('settlement record kind mismatch')
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if (row['closure_state'] != 'open' or row['frozen_inputs'] is not None
                    or row['terminal_candidate'] is not None):
                raise ClosureConflict('closure is not an open input-less journal')
            if not row.get('assignment'):
                raise ClosureConflict('settlement requires an Assignment-bound journal')
            if record.get('assignment') != row['assignment']:
                raise ClosureConflict('settlement Assignment binding mismatch')
            if self.result_path.exists():
                raise ClosureConflict('terminal result already exists')
            row.update(frozen_inputs={**copy.deepcopy(record), 'kind': kind},
                       closure_state='frozen', version=row['version'] + 1)
            return self._store(row)

    def freeze_world(self, world: dict, *, expected_version: int):
        """Freeze admission identity before business effects, once, under CAS."""
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row['closure_state'] != 'open' or row.get('world_identity') is not None:
                raise ClosureConflict('world identity already frozen')
            row['world_identity'] = copy.deepcopy(world)
            row['version'] += 1
            return self._store(row)

    def record_reset(self, receipt: dict, *, expected_version: int):
        """Persist resource disposition independently of immutable product bytes."""
        from autophone.reset_contract import validate_reset_receipt
        validate_reset_receipt(receipt)
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row.get('reset_receipt') is not None:
                raise ClosureConflict('reset receipt already frozen')
            binding = row.get('assignment')
            actual = receipt['binding']
            if binding and any(binding[k] != actual[{'generation': 'rig_generation'}.get(k, k)] for k in binding):
                raise ClosureConflict('reset receipt Assignment mismatch')
            row['reset_receipt'] = copy.deepcopy(receipt)
            row['version'] += 1
            return self._store(row)

    def prepare_result(self, result: dict, *, expected_version: int):
        """CAS claim and persist the exact terminal bytes before publishing."""
        validate_or_raise('run-result.v1.json', result)
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row['closure_state'] != 'frozen' or row['finalization_state'] != 'pending':
                raise ClosureConflict('finalization already claimed or inputs not frozen')
            if is_settled(row):
                raise ClosureSettled('closure settled without a product result')
            row.update(terminal_candidate=copy.deepcopy(result),
                       terminal_sha256=_digest(_encoded(result)),
                       finalization_state='running', version=row['version'] + 1)
            return self._store(row)

    def checkpoint(self, name: str, payload: dict, *, expected_version: int):
        """Append one immutable Host collection checkpoint under CAS."""
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row['closure_state'] != 'frozen' or row['terminal_candidate'] is not None:
                raise ClosureConflict('collection is not pending')
            if is_settled(row):
                raise ClosureSettled('closure settled without a product result')
            checkpoints = row.setdefault('collection_checkpoints', {})
            if name in checkpoints:
                raise ClosureConflict('collection checkpoint already exists')
            checkpoints[name] = copy.deepcopy(payload)
            row['version'] += 1
            return self._store(row)

    def resume(self, *, expected_version: int):
        """Publish prepared bytes once, or reconcile an already published result.

        This resumes publication only. It never reruns device work, queries a
        backend, rebuilds reports, releases a lease, or clears quarantine.
        """
        with self._locked():
            row = self._read()
            self._cas(row, expected_version)
            if row['terminal_candidate'] is None:
                raise ClosureConflict('no prepared terminal result')
            if self.result_path.exists():
                if _digest(self.result_path.read_bytes()) != row['terminal_sha256']:
                    self._isolate('terminal result hash mismatch')
            elif row['finalization_state'] in {'complete', 'failed'}:
                self._isolate('published terminal result is missing')
            else:
                self.crash_point('before_result_replace')
                self._atomic(self.result_path, _encoded(row['terminal_candidate']))
            self.crash_point('after_result_replace')
            if row['finalization_state'] == 'running':
                collect = row['terminal_candidate']['lock_phase']['collect']
                row.update(finalization_state='failed' if collect.get('status') == 'failed' else 'complete',
                           version=row['version'] + 1)
                row = self._store(row)
            self.crash_point('after_journal_complete')
            return row

    def publish(self, result: dict):
        row = self.read()
        if row['terminal_candidate'] is None:
            row = self.prepare_result(result, expected_version=row['version'])
        elif _encoded(result) != _encoded(row['terminal_candidate']):
            raise ClosureConflict('terminal result is immutable')
        return self.resume(expected_version=row['version'])


def _settle(args) -> int:
    """BL-194: dry-run (default) or apply settlements on the Control Plane terminal record."""
    from autophone.execution.closure_settlement import (
        SettlementRefused, residual_summary, settle_assignment_closures, summarize)

    read_assignment, authority_error = None, None
    try:
        from autophone.application.control_plane import require_control_plane
        read_assignment = require_control_plane().get_assignment
    except Exception as exc:  # noqa: BLE001 - unreadable authority defers every candidate
        authority_error = f'{type(exc).__name__}: {exc}'
    if args.apply and read_assignment is None:
        print(json.dumps({'refused': 'control_plane_unavailable', 'detail': authority_error}, ensure_ascii=False))
        return 2
    from autophone.device.lock import default_screen_lock_root
    try:
        rows = settle_assignment_closures(args.run_root, read_assignment, apply=args.apply, actor=args.actor,
                                          authority_error=authority_error,
                                          screen_lock_root=default_screen_lock_root(),
                                          spool_root=args.spool_root,
                                          run_ids=args.run_id, assignment_ids=args.assignment_id)
    except SettlementRefused as exc:
        print(json.dumps({'refused': exc.code, 'detail': str(exc), **exc.extra}, ensure_ascii=False))
        return 2
    summary = summarize(rows)
    if not args.all:
        rows = [row for row in rows if row['action'] != 'skip' or row['reason'] != 'inputs_frozen']
    print(json.dumps({'mode': 'apply' if args.apply else 'dry_run', 'summary': summary,
                      'residual': residual_summary(rows), 'journals': rows},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'resume', 'collect', 'scan', 'settle'])
    parser.add_argument('run_dir', type=Path, nargs='?')
    parser.add_argument('--expected-version', type=int)
    parser.add_argument('--run-root', type=Path)
    parser.add_argument('--apply', action='store_true',
                        help='settle: write the settlement records. Without it the scan creates, '
                             'locks or quarantines no file; --apply needs the platform run root')
    parser.add_argument('--actor', help='settle --apply: who authorizes the settlement (audit field)')
    parser.add_argument('--all', action='store_true', help='settle: also list journals with frozen inputs')
    parser.add_argument('--spool-root', type=Path,
                        help='settle: Rig Artifact spool root (<rig log_dir>/artifact-spool) for the residual '
                             'inventory of interrupted Attempts; omitted means not checked')
    parser.add_argument('--run-id', action='append', metavar='RUN_ID',
                        help='settle: only journals of this Run (repeatable). Without --run-id/--assignment-id '
                             'the dry run and --apply cover every journal under --run-root; a value that selects '
                             'no journal refuses the whole call (exit 2) before any write')
    parser.add_argument('--assignment-id', action='append', metavar='ASSIGNMENT_ID',
                        help='settle: only journals bound to this Assignment (repeatable; combines with '
                             '--run-id as AND)')
    args = parser.parse_args(argv)
    if args.command == 'settle':
        if args.run_root is None:
            parser.error('settle requires --run-root')
        if args.apply and not str(args.actor or '').strip():
            parser.error('settle --apply requires --actor')
        for flag, values in (('--run-id', args.run_id), ('--assignment-id', args.assignment_id)):
            if values is not None and not all(str(value).strip() for value in values):
                parser.error(f'settle {flag} needs a non-empty value')
        return _settle(args)
    if args.run_dir is None:
        parser.error(f'{args.command} requires run_dir')
    journal = ClosureJournal(args.run_dir)
    if args.command == 'resume' and args.expected_version is None:
        parser.error('resume requires --expected-version')
    if args.command in {'collect', 'scan'}:
        if args.run_root is None:
            parser.error('collect and scan require --run-root')
        from autophone.execution.run_service import RunService
        service = RunService(run_root=args.run_root)
        output = (service.recover_pending_closures() if args.command == 'scan'
                  else {'result_path': str(service.resume_collection(args.run_dir))})
        print(json.dumps(output, ensure_ascii=False))
        return 0
    row = journal.read() if args.command == 'inspect' else journal.resume(expected_version=args.expected_version)
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
