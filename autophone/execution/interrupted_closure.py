"""Resource closure of Lease-expired, ``indeterminate`` Attempts (BL-076 G1～G4).

被中断 Attempt：Agent 失联后控制面按 Lease 过期回收，Attempt 与 Assignment 终态为
``indeterminate``，没有 Assignment Result。它的 ClosureJournal 停在 open，App 进程、App 内 Frida
映射、录屏帧与 ScreenLock 状态文件留在原处。

本模块提供三件事：

* ``residual_inventory``：以 journal 已登记的身份（Assignment 绑定、设备键、world identity 中的
  bundle_id、Run 目录）为准，只读列出残留资源。不删除、不合成、不改写任何文件（G4 只登记）。
* ``interrupted_settlement``：中断未闭包结算记录（``closure_outcome='interrupted_unclosed'``）。
  它不发布 Run Result，不改写 ``indeterminate`` 终态。
* ``InterruptedAttemptClosureExecution``：闭包恢复 Assignment（``desired_state.mode='interrupted_closure'``）
  的执行器。它只用本 Attempt 的新 Fencing（``write_fence``）与 StageGuard ``recovery`` 阶段访问设备：
  独立 guest 就绪检查 → 残留清单 → journal 结算 → 取 ScreenLock 后按 bundle_id 终止目标 App 并独立回读。
  任一被中断 Attempt 未闭包时以 ``error`` 与 ``rig_disposition='quarantine'`` 完成，Rig 保持隔离。

设计说明：``evidence/outputs/bl076-attempt-timeout-closure-20261014/closure-design.md``。
"""
from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.contracts import validate_or_raise
from autophone.logging import get_logger

from .closure_journal import (
    INTERRUPTED_SETTLEMENT_KIND,
    ClosureConflict,
    ClosureJournal,
    ClosureQuarantined,
    is_settled,
)

logger = get_logger(__name__)

#: 闭包恢复 Assignment 的 ``desired_state.mode``。
CLOSURE_MODE = 'interrupted_closure'
ENTRY_SCHEMA = 'interrupted-attempt-closure.v1.json'
ENTRY_VERSION = 'interrupted-attempt-closure/v1'
RECEIPT_NAME = 'interrupted-closure-receipt.json'
IDENTITY_KEYS = ('assignment_id', 'attempt_no', 'fencing', 'rig_id', 'generation')

#: journal 处置与 App 终止中视为「已闭包」的取值。
_JOURNAL_CLOSED = frozenset({'settled', 'already_settled', 'collected', 'result_published', 'not_found'})
_APP_CLOSED = frozenset({'terminated', 'not_running', 'skipped', 'not_applicable'})

_RECORDING_DIR = 'planes/ui/recording'
_CRASH_EVENTS = 'planes/crash/crash_trap_events.jsonl'
_SCREEN_MARKER = '.screen-execution-started.json'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {key: binding[key] for key in IDENTITY_KEYS}


# ── 残留资源清单（只读）─────────────────────────────────────────────────────

def journal_bundle_id(row: Mapping[str, Any] | None) -> str | None:
    """journal 冻结的 world identity 中设备回读到的被测 App bundle_id。"""
    world = (row or {}).get('world_identity') or {}
    observed = ((world.get('subject_build_ref') or {}).get('device_observation') or {})
    value = str(observed.get('bundle_id') or '').strip()
    return value or None


def _pid_alive(pid: Any) -> bool | None:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _frida_session(run_dir: Path | None) -> dict[str, Any]:
    item: dict[str, Any] = {'kind': 'frida_session', 'events_path': _CRASH_EVENTS}
    if run_dir is None:
        return {**item, 'state': 'not_checked'}
    path = run_dir / _CRASH_EVENTS
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return {**item, 'state': 'no_events'}
    except OSError as exc:
        return {**item, 'state': 'unreadable', 'detail': f'{type(exc).__name__}: {exc}'}
    last = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('ev') in {'ready', 'detached', 'attach_error'}:
            last = event
    if last is None:
        return {**item, 'state': 'not_attached'}
    item['last_event'] = last['ev']
    if last['ev'] == 'ready':
        # 推断：最后一次 attach 成功后没有 detached 记录，会话在 Agent 退出时仍持有。
        return {**item, 'state': 'ready_without_detach_record', 'pid': last.get('pid')}
    if last['ev'] == 'detached':
        return {**item, 'state': 'detached'}
    return {**item, 'state': 'not_attached'}


def _recording(run_dir: Path | None) -> dict[str, Any]:
    item: dict[str, Any] = {'kind': 'recording', 'frames_dir': _RECORDING_DIR}
    if run_dir is None:
        return {**item, 'state': 'not_checked'}
    directory = run_dir / _RECORDING_DIR
    frames = sorted(path.relative_to(run_dir).as_posix() for path in directory.glob('*') if path.is_file()) \
        if directory.is_dir() else []
    mp4 = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob('*.mp4'))
    return {**item, 'state': 'retained' if frames else 'absent', 'frame_count': len(frames),
            'frames': frames, 'mp4_present': bool(mp4), 'mp4': mp4}


def _screen_lock(root: Path | None, device: str) -> dict[str, Any]:
    item: dict[str, Any] = {'kind': 'screen_lock', 'device': device}
    if root is None:
        return {**item, 'state': 'not_checked'}
    root = Path(root)
    state_path = root / f'{device}.json'
    item['state_file'] = str(state_path)
    item['quarantine_marker'] = (root / f'{device}.quarantine.json').exists()
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {**item, 'state': 'free', 'locked': False}
    except (OSError, json.JSONDecodeError) as exc:
        return {**item, 'state': 'unreadable', 'detail': f'{type(exc).__name__}: {exc}'}
    locked = bool(state.get('locked'))
    item.update(locked=locked, pid=state.get('pid'), owner_kind=state.get('owner_kind'),
                owner_id=state.get('owner_id'))
    if not locked:
        return {**item, 'state': 'free'}
    alive = _pid_alive(state.get('pid'))
    item['pid_alive'] = alive
    # flock 随进程退出由内核释放；只读判活，不像 ScreenLock.live_status 那样就地改写状态文件。
    return {**item, 'state': 'held' if alive else 'stale_state_file'}


def residual_inventory(
    run_dir: Path | None,
    row: Mapping[str, Any],
    *,
    screen_lock_root: Path | None = None,
    spool_root: Path | None = None,
) -> list[dict[str, Any]]:
    """只读列出被中断 Attempt 的残留资源；``row`` 是 journal 记录（找不到 journal 时只含绑定）。"""
    run_dir = Path(run_dir) if run_dir is not None else None
    binding = row.get('assignment') or {}
    bundle_id = journal_bundle_id(row)
    device = str(row.get('device') or binding.get('rig_id') or '')
    items: list[dict[str, Any]] = [
        {'kind': 'app_process', 'bundle_id': bundle_id,
         'source': 'world_identity' if bundle_id else 'none', 'state': 'not_probed'},
        _frida_session(run_dir),
        _recording(run_dir),
        _screen_lock(screen_lock_root, device),
        {'kind': 'stage_guard_token', 'state': 'process_scoped',
         'detail': 'token lived in the Agent process; device writes of this fencing are refused by the Control Plane'},
    ]
    assignment_id = str(binding.get('assignment_id') or '')
    spool = {'kind': 'artifact_spool', 'path': f'artifact-spool/{assignment_id}'}
    if spool_root is None or not assignment_id:
        spool['state'] = 'not_checked'
    else:
        spool['state'] = 'present' if (Path(spool_root) / assignment_id).exists() else 'absent'
    items.append(spool)
    if run_dir is None:
        items.append({'kind': 'screen_execution_marker', 'state': 'not_checked'})
    else:
        items.append({'kind': 'screen_execution_marker',
                      'state': 'present' if (run_dir / _SCREEN_MARKER).exists() else 'absent'})
    return items


def interrupted_settlement(
    binding: Mapping[str, Any],
    *,
    authority: str,
    lease_released_at: str | None,
    residual_resources: list[dict[str, Any]],
    run_type: str | None = None,
    closure_assignment: Mapping[str, Any] | None = None,
    lease_release_reason: str | None = None,
) -> dict[str, Any]:
    """中断未闭包结算记录；不含 ``settled_at``/``settled_by``，由写入方补齐。

    ``lease_release_reason`` 为控制面释放 Lease 的原因：``lease_expired``（回收）或
    ``agent_interrupted``（Agent 重连时报告，BL-240）；未给出时按 ``lease_expired`` 记录。
    """
    record = {
        'kind': INTERRUPTED_SETTLEMENT_KIND,
        'authority': authority,
        'assignment': identity(binding),
        'assignment_status': 'indeterminate',
        'attempt_status': 'indeterminate',
        'lease_release_reason': lease_release_reason or 'lease_expired',
        'lease_released_at': lease_released_at,
        'closure_outcome': 'interrupted_unclosed',
        'product_verdict': None,
        'run_type': run_type,
        'product_result_published': False,
        'evidence_complete': False,
        'residual_resources': copy.deepcopy(residual_resources),
    }
    if closure_assignment is not None:
        record['closure_assignment'] = dict(closure_assignment)
    return record


def find_assignment_journals(run_root: Path, binding: Mapping[str, Any], run_id: str | None = None) -> list[ClosureJournal]:
    """按 Assignment 绑定只读查找 journal；不加锁、不隔离无效 journal。"""
    root = Path(run_root)
    if run_id:
        paths = {*root.glob(f'*/{run_id}/closure-intent.json'),
                 *root.glob(f"*/{run_id}.attempt-{int(binding['attempt_no'])}/closure-intent.json")}
    else:
        paths = set(root.rglob('closure-intent.json'))
    wanted = identity(binding)
    matches = []
    for path in sorted(paths):
        journal = ClosureJournal(path.parent)
        try:
            row = journal.inspect_readonly()
        except ValueError:
            continue
        if row.get('assignment') == wanted:
            matches.append(journal)
    return matches


# ── 闭包恢复 Assignment 执行器（Agent 侧）────────────────────────────────────

def _running_pid(device: Any, bundle_id: str) -> int:
    for app in device.app_list():
        if app.get('bundle_id') == bundle_id:
            try:
                pid = int(app.get('pid') or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0:
                return pid
    return 0


class InterruptedAttemptClosureExecution:
    """Execute one interrupted-closure Recovery Assignment under its own new Fencing."""

    def __init__(
        self,
        run_root: Path,
        *,
        screen_lock: Any = None,
        collect: Callable[..., Any] | None = None,
        device_factory: Callable[[Any], Any] | None = None,
        profile_loader: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        terminate_timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        self.run_root = Path(run_root)
        self._screen_lock = screen_lock
        self.collect = collect
        self.device_factory = device_factory
        self.profile_loader = profile_loader
        self.sleep = sleep
        self.terminate_timeout_s = terminate_timeout_s
        self.poll_interval_s = poll_interval_s

    @property
    def screen_lock(self):
        if self._screen_lock is None:
            from autophone.device.lock import ScreenLock
            self._screen_lock = ScreenLock()
        return self._screen_lock

    def _device(self, context):
        if self.device_factory is not None:
            return self.device_factory(context)
        from autophone.device import VPhoneDeviceSession
        return VPhoneDeviceSession(context.target, context.write_fence)

    def _bundle_for(self, item: Mapping[str, Any], row: Mapping[str, Any] | None) -> tuple[str | None, str, str | None]:
        """返回 (bundle_id, 来源, 失败原因)。journal 登记优先，其次按 App 名加载 profile。"""
        bundle_id = journal_bundle_id(row)
        if bundle_id:
            return bundle_id, 'world_identity', None
        app = str(item.get('app') or '').strip()
        if not app:
            return None, 'none', None
        try:
            loader = self.profile_loader
            if loader is None:
                from autophone.assets.apps import load_app_profile as loader
            value = str(getattr(loader(app), 'bundle_id', '') or '').strip()
        except Exception as exc:  # noqa: BLE001 - recorded as an unresolved target
            return None, 'app_profile', f'bundle_unresolved: {type(exc).__name__}: {exc}'
        return (value or None), 'app_profile', (None if value else 'bundle_unresolved')

    def execute(self, assignment: Mapping[str, Any], context: Any) -> dict[str, Any]:
        from autophone.device.stage_guard import OperationClass, StageGuard
        from .artifacts import seal_artifacts

        desired = assignment.get('desired_state') or {}
        if assignment.get('kind') != 'recovery' or desired.get('mode') != CLOSURE_MODE:
            raise ValueError('interrupted closure Assignment identity is required')
        target = context.target
        assignment_id = str(assignment['assignment_id'])
        attempt = assignment['attempt']
        owner = f"{assignment_id}:{attempt['attempt_no']}"
        guard = StageGuard(owner_id=owner, rig_id=target.rig_id, recovery=True,
                           generation=target.rig_generation, check_lease=context.write_fence.check,
                           check_release=getattr(context.write_fence, 'check_release', None),
                           socket_path=target.socket, frida_endpoint=target.frida_endpoint)
        evidence = target.log_dir / 'recovery' / assignment_id / f"attempt-{attempt['attempt_no']}"
        evidence.mkdir(parents=True, exist_ok=True)
        receipt: dict[str, Any] = {'mode': CLOSURE_MODE, 'rig_id': target.rig_id, 'generation': target.rig_generation,
                                   'readiness': {'available': False, 'state': 'not_checked'}, 'closures': []}
        errors: list[str] = []
        device = None
        closed = False
        sealed = None
        with guard.scope('recovery'):
            try:
                if desired.get('generation') != target.rig_generation:
                    raise ValueError('Closure generation does not match the Agent target')
                items = list(desired.get('interrupted_attempts') or [])
                for item in items:
                    if (item.get('rig_id'), item.get('generation')) != (target.rig_id, target.rig_generation):
                        raise ValueError('Interrupted Attempt target mismatch')
                context.write_fence.check()
                device = self._device(context)
                with guard.operation(OperationClass.READ):
                    receipt['readiness'] = dict(device.probe_uds())
                readiness = receipt['readiness']
                if readiness.get('available') is not True or readiness.get('state') != 'guest_ready':
                    raise RuntimeError('Independent guest readiness check failed')
                for item in items:
                    context.write_fence.check()
                    entry = self._close_one(item, assignment_id, attempt, owner, context, device)
                    validate_or_raise(ENTRY_SCHEMA, entry)
                    receipt['closures'].append(entry)
                unclosed = [entry['interrupted_attempt']['assignment_id'] for entry in receipt['closures']
                            if entry['closure_state'] != 'closed']
                if unclosed:
                    errors.append('interrupted closure failed: ' + ', '.join(unclosed))
            except Exception as exc:  # noqa: BLE001 - the receipt records the failure; the Rig stays quarantined
                errors.append(f'{type(exc).__name__}: {exc}')
            finally:
                guard.transition('cleanup')
                try:
                    if device is not None:
                        device.close()
                    closed = True
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'Device close: {type(exc).__name__}: {exc}')
                try:
                    (evidence / RECEIPT_NAME).write_text(
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
                    sealed = seal_artifacts(
                        evidence, target.log_dir / 'artifact-spool' / assignment_id / f"attempt-{attempt['attempt_no']}",
                        owner_id=owner)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'Artifact sealing: {type(exc).__name__}: {exc}')
        result = {'schema_version': 'assignment-result/v1', 'assignment_id': assignment_id,
                  'attempt_no': attempt['attempt_no'], 'fencing': attempt['fencing'],
                  'status': 'error' if errors else 'passed', 'product_verdict': 'not_run', 'run_result': None,
                  'cleanup_receipt': {'status': 'passed' if closed and sealed else 'error',
                                      'artifacts_sealed': sealed is not None, 'device_access_released': closed},
                  'rig_disposition': 'quarantine' if errors else 'reusable',
                  'fixture_disposition': 'unknown', 'artifact_refs': list(sealed.refs) if sealed else [],
                  'closure': None, 'recovery_receipt': receipt}
        if errors:
            result['detail'] = '; '.join(errors)
            logger.warning('interrupted closure %s: %s', assignment_id, result['detail'])
        validate_or_raise('assignment-result.v1.json', result)
        return result

    def _close_one(self, item, assignment_id, attempt, owner, context, device) -> dict[str, Any]:
        target = context.target
        binding = identity(item)
        entry: dict[str, Any] = {
            'schema_version': ENTRY_VERSION, 'interrupted_attempt': binding, 'run_id': item.get('run_id'),
            'journal': {'state': 'not_found', 'run_dir': None, 'version': None, 'record_sha256': None, 'reason': None},
            'residual_resources': [], 'app_termination': {}, 'closure_state': 'failed', 'reasons': [],
        }
        journals = find_assignment_journals(self.run_root, binding, item.get('run_id'))
        journal = None
        row: dict[str, Any] | None = None
        if len(journals) > 1:
            entry['journal'].update(state='failed', reason='journal_ambiguous')
        elif journals:
            journal = journals[0]
            row = journal.inspect_readonly()
            entry['journal'].update(run_dir=str(journal.root), version=row['version'],
                                    record_sha256=row['record_sha256'])
        inventory = residual_inventory(journal.root if journal else None, row or {'assignment': binding},
                                       screen_lock_root=self.screen_lock.root,
                                       spool_root=target.log_dir / 'artifact-spool')
        bundle_id, source, bundle_error = self._bundle_for(item, row)
        app = next(resource for resource in inventory if resource['kind'] == 'app_process')
        app.update(bundle_id=bundle_id, source=source)
        if bundle_id:
            try:
                pid = _running_pid(device, bundle_id)
                app.update(state='running' if pid > 0 else 'not_running', pid=pid)
            except Exception as exc:  # noqa: BLE001 - recorded; termination reads back again
                app.update(state='unknown', detail=f'{type(exc).__name__}: {exc}')
        entry['residual_resources'] = inventory
        if journal is not None:
            entry['journal'] = self._settle(journal, binding, item, inventory, assignment_id, attempt, context)
        device_key = str((row or {}).get('device') or target.rig_id)
        entry['app_termination'] = self._terminate(bundle_id, bundle_error, device_key, owner, context, device)
        reasons = []
        if entry['journal']['state'] not in _JOURNAL_CLOSED:
            reasons.append(str(entry['journal']['reason'] or 'journal_not_closed'))
        if entry['app_termination']['result'] not in _APP_CLOSED:
            reasons.append(str(entry['app_termination']['reason'] or 'app_not_closed'))
        entry['reasons'] = reasons
        entry['closure_state'] = 'failed' if reasons else 'closed'
        return entry

    def _settle(self, journal, binding, item, inventory, assignment_id, attempt, context) -> dict[str, Any]:
        state = {'state': 'failed', 'run_dir': str(journal.root), 'version': None, 'record_sha256': None,
                 'reason': None}
        collect_needed = False
        try:
            with journal.owner():
                context.write_fence.check()
                row = journal.read()
                state.update(version=row['version'], record_sha256=row['record_sha256'])
                if is_settled(row):
                    state['state'] = 'already_settled'
                elif row['terminal_candidate'] is not None or journal.result_path.exists():
                    state['state'] = 'result_published'
                elif row['frozen_inputs'] is not None:
                    collect_needed = True
                elif row.get('assignment') != binding:
                    state['reason'] = 'journal_binding_mismatch'
                else:
                    record = interrupted_settlement(
                        binding, authority='interrupted_closure_assignment',
                        lease_released_at=item.get('lease_released_at'), residual_resources=inventory,
                        run_type=item.get('run_type'), lease_release_reason=item.get('lease_release_reason'),
                        closure_assignment={'assignment_id': assignment_id, 'attempt_no': attempt['attempt_no'],
                                            'fencing': attempt['fencing']})
                    record.update(settled_at=_now(), settled_by=f'closure-assignment:{assignment_id}')
                    context.write_fence.check()
                    settled = journal.settle(record, expected_version=row['version'])
                    state.update(state='settled', version=settled['version'], record_sha256=settled['record_sha256'])
        except ClosureQuarantined as exc:
            state['reason'] = f'journal_quarantined: {exc}'
            return state
        except ClosureConflict as exc:
            state['reason'] = 'journal_owner_active' if 'active owner' in str(exc) else f'journal_conflict: {exc}'
            return state
        if collect_needed:
            if self.collect is None:
                state['reason'] = 'inputs_frozen'
                return state

            def authority(actual):
                context.write_fence.check()
                if actual != binding:
                    raise ValueError('Interrupted Attempt closure binding mismatch')

            try:
                self.collect(journal.root, assignment_authority=authority)
            except Exception as exc:  # noqa: BLE001 - collection stays unavailable; the Rig stays quarantined
                state['reason'] = f'collection_unavailable: {type(exc).__name__}: {exc}'
                return state
            state['state'] = 'collected'
        return state

    def _terminate(self, bundle_id, bundle_error, device_key, owner, context, device) -> dict[str, Any]:
        from autophone.device.lock import ScreenQuarantineMarkerPresent
        from autophone.device.session import DeviceProtocolError
        from .app_frida_hygiene import protected_reason

        record: dict[str, Any] = {'result': 'failed', 'bundle_id': bundle_id, 'pid_before': None,
                                  'pid_after': None, 'readback': None, 'reason': None}
        if not bundle_id:
            if bundle_error:
                record['reason'] = bundle_error
            else:
                record.update(result='not_applicable', reason='target_app_not_registered')
            return record
        protected = protected_reason(bundle_id)
        if protected:
            record.update(result='skipped', reason=protected)
            return record
        try:
            lease = self.screen_lock.try_lease(device=device_key, owner_kind='run', owner_id=owner)
        except ScreenQuarantineMarkerPresent:
            record['reason'] = 'screen_quarantined'
            return record
        if lease is None:
            record['reason'] = 'screen_lock_busy'
            return record
        try:
            pid = _running_pid(device, bundle_id)
            record['pid_before'] = pid
            if pid > 0:
                context.write_fence.check()
                try:
                    device.terminate_app(bundle_id)
                except DeviceProtocolError as exc:
                    # app_terminate 的 ok=false 不是结论：guest 只采样一次；以回读为准。
                    record['terminate_error'] = str(exc)
                for _ in range(max(1, int(self.terminate_timeout_s / self.poll_interval_s))):
                    if _running_pid(device, bundle_id) <= 0:
                        break
                    self.sleep(self.poll_interval_s)
            after = _running_pid(device, bundle_id)
            record.update(pid_after=after, readback={'source': 'app_list', 'pid': after, 'checked_at': _now()})
            if after > 0:
                record.update(result='failed', reason='app_still_running')
            else:
                record['result'] = 'terminated' if pid > 0 else 'not_running'
        except DeviceProtocolError as exc:
            record['reason'] = f'device_read_failed: {exc}'
        finally:
            lease.release()
        return record


__all__ = ['CLOSURE_MODE', 'ENTRY_SCHEMA', 'InterruptedAttemptClosureExecution', 'find_assignment_journals',
           'interrupted_settlement', 'journal_bundle_id', 'residual_inventory']
