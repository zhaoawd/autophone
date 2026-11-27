"""Lease-bound VM recovery, with an independent guest readiness check."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from autophone.contracts import validate_or_raise
from autophone.device import VPhoneDeviceSession
from autophone.device.stage_guard import OperationClass, StageGuard
from autophone.recovery_observation import emit_recovery_event, last_failed_stage, recovery_event_log
from .artifacts import seal_artifacts


class RecoveryStartupError(RuntimeError):
    def __init__(self, exit_code):
        self.exit_code = exit_code
        super().__init__(f'Recovery startup exited {exit_code}')


class RecoveryScript:
    def __init__(self, script=None, timeout=600):
        self.script = Path(script) if script else Path(__file__).resolve().parents[2] / 'scripts/start_vphone.sh'
        self.timeout = timeout

    def __call__(self, assignment, context, evidence):
        environment = {**os.environ, **context.target.process_environment(),
                       'AUTOPHONE_RECOVERY_ASSIGNMENT_ID': assignment['assignment_id'],
                       'FRIDA_PY': os.environ.get('FRIDA_PY') or sys.executable,
                       'AUTOPHONE_CONTROL_PY': os.environ.get('AUTOPHONE_CONTROL_PY') or sys.executable}
        environment.pop('AUTOPHONE_STAGE_LOG', None)
        environment['AUTOPHONE_STAGE_LOG'] = str(recovery_event_log(environment))
        context.write_fence.check()
        with StageGuard.current().operation(OperationClass.VM), (evidence / 'startup.log').open('wb') as log:
            process = subprocess.Popen(['bash', str(self.script)], env=environment,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + self.timeout
            try:
                while process.poll() is None:
                    context.write_fence.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Recovery startup deadline exceeded')
                    time.sleep(.2)
                context.write_fence.check()
                if process.returncode:
                    raise RecoveryStartupError(process.returncode)
            except BaseException:
                # Kill the owned process group before returning control to the Agent.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise


class RecoveryAssignmentExecution:
    def __init__(self, recover=None, device_factory=None, closure_recovery=None):
        self.recover = recover or RecoveryScript()
        self.device_factory = device_factory or (lambda context: VPhoneDeviceSession(context.target, context.write_fence))
        self.closure_recovery = closure_recovery

    def execute(self, assignment, context):
        desired = assignment.get('desired_state') or {}
        if assignment.get('kind') != 'recovery' or not desired.get('recovery_id'):
            raise ValueError('Recovery Assignment identity is required')
        if desired.get('generation') != context.target.rig_generation:
            raise ValueError('Recovery generation does not match the Agent target')
        assignment_id = assignment['assignment_id']
        attempt = assignment['attempt']
        owner = f"{assignment_id}:{attempt['attempt_no']}"
        # 收尾释放已持有资源只核对租约与 fencing，取消不阻止释放（BL-220 第二轮，同 AssignmentExecution）。
        guard = StageGuard(owner_id=owner, rig_id=context.target.rig_id, recovery=True,
                           generation=context.target.rig_generation, check_lease=context.write_fence.check,
                           check_release=getattr(context.write_fence, 'check_release', None),
                           socket_path=context.target.socket, frida_endpoint=context.target.frida_endpoint)
        evidence = context.target.log_dir / 'recovery' / assignment_id / f"attempt-{attempt['attempt_no']}"
        evidence.mkdir(parents=True, exist_ok=True)
        receipt = {'recovery_id': desired['recovery_id'], 'rig_id': context.target.rig_id,
                   'generation': context.target.rig_generation,
                   'readiness': {'available': False, 'state': 'not_checked'}}
        device = None
        errors = []
        status = 'error'
        sealed = None
        closed = False
        event_environment = {**os.environ, **context.target.process_environment()}
        # A stage log selected for the Agent's own startup must not split one
        # recovery attempt away from the target's managed recovery event log.
        event_environment.pop('AUTOPHONE_STAGE_LOG', None)
        event_path = recovery_event_log(event_environment)
        try:
            event_offset = event_path.stat().st_size
        except OSError:
            event_offset = 0
        event_environment['AUTOPHONE_STAGE_LOG'] = str(event_path)
        attempt_no = attempt['attempt_no']
        started_at = time.monotonic()
        start_script = getattr(self.recover, 'script', None)
        event_started = False
        failure = None
        with guard.scope('recovery'):
            try:
                context.write_fence.check()
                emit_recovery_event(event_environment, 'recovery_started', attempt=attempt_no,
                                    start_script=str(start_script or type(self.recover).__name__))
                event_started = True
                device = self.device_factory(context)
                self.recover(assignment, context, evidence)
                with guard.operation(OperationClass.READ):
                    receipt['readiness'] = dict(device.probe_uds())
                context.write_fence.check()
                readiness = receipt['readiness']
                if readiness.get('available') is not True or readiness.get('state') != 'guest_ready':
                    raise RuntimeError('Independent guest readiness check failed')
                if self.closure_recovery is not None:
                    receipt['closure'] = self.closure_recovery(assignment, context)
                status = 'passed'
            except Exception as exc:
                failure = exc
                errors.append(f'{type(exc).__name__}: {exc}')
            finally:
                guard.transition('cleanup')
                try:
                    if device is not None:
                        device.close()
                    closed = True
                except Exception as exc:
                    errors.append(f'Device close: {type(exc).__name__}: {exc}')
                try:
                    (evidence / 'recovery-receipt.json').write_text(json.dumps(receipt, sort_keys=True), encoding='utf-8')
                    sealed = seal_artifacts(evidence, context.target.log_dir / 'artifact-spool' / assignment_id / f"attempt-{attempt['attempt_no']}", owner_id=owner)
                except Exception as exc:
                    errors.append(f'Artifact sealing: {type(exc).__name__}: {exc}')
        duration_s = round(time.monotonic() - started_at, 3)
        if event_started and status == 'passed' and not errors:
            emit_recovery_event(event_environment, 'recovery_succeeded', attempt=attempt_no,
                                duration_s=duration_s,
                                health_state=receipt['readiness'].get('state', 'unknown'))
        elif event_started:
            emit_recovery_event(event_environment, 'recovery_failed',
                                stage=last_failed_stage(event_path, start_offset=event_offset),
                                attempt=attempt_no,
                                exit_code=getattr(failure, 'exit_code', None),
                                error='; '.join(errors) or 'recovery failed')
        result = {'schema_version': 'assignment-result/v1', 'assignment_id': assignment_id,
                  'attempt_no': attempt['attempt_no'], 'fencing': attempt['fencing'],
                  'status': 'error' if errors else status, 'product_verdict': 'not_run', 'run_result': None,
                  'cleanup_receipt': {'status': 'passed' if closed and sealed else 'error',
                                      'artifacts_sealed': sealed is not None, 'device_access_released': closed},
                  'rig_disposition': 'reusable' if not errors and status == 'passed' else 'quarantine',
                  'fixture_disposition': 'unknown', 'artifact_refs': list(sealed.refs) if sealed else [],
                  'closure': None, 'recovery_receipt': receipt}
        if errors:
            result['detail'] = '; '.join(errors)
        validate_or_raise('assignment-result.v1.json', result)
        return result


class RecoveryAwareExecutor:
    def __init__(self, ordinary, recovery=None, closure=None):
        self.ordinary = ordinary
        self.recovery = recovery or RecoveryAssignmentExecution()
        # BL-076：`rig recover` 为 Lease 过期中断的 Attempt 创建的闭包恢复 Assignment。
        self.closure = closure

    def execute(self, assignment, context):
        if assignment.get('kind') != 'recovery':
            return self.ordinary.execute(assignment, context)
        from .interrupted_closure import CLOSURE_MODE
        if (assignment.get('desired_state') or {}).get('mode') == CLOSURE_MODE:
            closure = self.closure
            if closure is None:
                raise ValueError('interrupted closure executor is not configured')
            return closure.execute(assignment, context)
        return self.recovery.execute(assignment, context)

    def screen_quarantine_report(self, target):
        """隔离标记只读汇总（BL-224），由普通执行器按其 ScreenLock 锁根读取。"""
        return self.ordinary.screen_quarantine_report(target)


class InterruptedRunCollection:
    """Resume only the exact interrupted Attempt's frozen Host collection."""
    def __init__(self, service):
        self.service = service

    def __call__(self, assignment, context):
        from .closure_journal import ClosureJournal
        interrupted = (assignment.get('desired_state') or {}).get('interrupted_attempt')
        if not interrupted:
            return {'status': 'not_required'}
        expected = {key: interrupted[key] for key in ('assignment_id', 'attempt_no', 'fencing', 'rig_id', 'generation')}
        if expected['rig_id'] != context.target.rig_id or expected['generation'] != context.target.rig_generation:
            raise ValueError('Interrupted Attempt target mismatch')

        def authority(binding):
            context.write_fence.check()
            if binding != expected:
                raise ValueError('Interrupted Attempt closure binding mismatch')

        matches = []
        for path in self.service.run_root.rglob('closure-intent.json'):
            journal = ClosureJournal(path.parent)
            row = journal.read()
            if row['run_id'] == interrupted['run_id'] and row.get('assignment') == expected:
                matches.append(journal)
        if len(matches) != 1:
            return {'status': 'unavailable', 'reason': 'exact_closure_not_found', 'run_id': interrupted['run_id']}
        journal = matches[0]
        authority(journal.read()['assignment'])
        try:
            self.service.resume_collection(journal.root, assignment_authority=authority)
        except Exception as exc:
            return {'status': 'unavailable', 'reason': type(exc).__name__, 'run_id': interrupted['run_id'],
                    'projection': journal.projection()}
        context.write_fence.check()
        return {'status': 'collected', 'run_id': interrupted['run_id'], 'projection': journal.projection(),
                'resource_reset': 'unverified'}
