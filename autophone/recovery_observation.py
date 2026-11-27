"""Persist health down edges before attempting Control Plane delivery."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.observation-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def observation_root(environment):
    explicit = environment.get('AUTOPHONE_RECOVERY_EVENT_DIR')
    if explicit:
        return Path(explicit).resolve()
    logs = environment.get('AUTOPHONE_RIG_LOG_DIR') or environment.get('VPHONE_LOG_DIR')
    if logs:
        return Path(logs).resolve() / 'recovery-events'
    return Path.home() / '.autophone' / 'recovery-events'


def recovery_event_log(environment, *, now=None):
    """Return the managed daily JSONL partition for recovery visibility events."""
    explicit = environment.get('AUTOPHONE_STAGE_LOG')
    if explicit:
        return Path(explicit).resolve()
    current = now or datetime.now(timezone.utc)
    return observation_root(environment) / f'{current:%Y-%m-%d}.jsonl'


def emit_recovery_event(environment, event, **payload):
    """Append one recovery visibility event without affecting recovery control flow.

    PostgreSQL remains the Control State source.  This JSONL stream is an Artifact
    projection consumed by ``GET /api/vm/recovery``.  Like the shell stage-event
    channel, an unavailable artifact path is fail-open.
    """
    now = datetime.now(timezone.utc)
    path = recovery_event_log(environment, now=now)
    row = {
        'event': event,
        **payload,
        'ts': now.replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'rig_id': environment.get('AUTOPHONE_RIG_ID', ''),
        'vm_dir': environment.get('VM_DIR', ''),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / '.events.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        return None
    return path


def last_failed_stage(path, *, start_offset=0):
    """Read the last stage_error/open stage after one recovery attempt started."""
    if path is None or not Path(path).is_file():
        return 'unknown'
    open_stage = None
    failed_stage = None
    try:
        with Path(path).open(encoding='utf-8') as stream:
            stream.seek(start_offset)
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = row.get('event')
                stage = row.get('stage')
                if event == 'stage_start' and stage:
                    open_stage = stage
                elif event == 'stage_end' and stage == open_stage:
                    open_stage = None
                elif event == 'stage_error' and stage:
                    failed_stage = stage
    except OSError:
        return 'unknown'
    return failed_stage or open_stage or 'unknown'


def persist_edge(environment):
    root = observation_root(environment)
    root.mkdir(parents=True, exist_ok=True)
    rig = environment.get('AUTOPHONE_RIG_ID')
    generation = environment.get('AUTOPHONE_RIG_GENERATION')
    runtime = environment.get('AUTOPHONE_RIG_RUNTIME_DIR')
    if not generation and runtime:
        target_path = Path(runtime) / 'device-target.json'
        if target_path.is_file():
            target = json.loads(target_path.read_text())
            if target.get('rig_id') == rig:
                generation = target.get('rig_generation')
    with (root / '.edge.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        counter = root / 'edge-sequence.json'
        sequence = (json.loads(counter.read_text()) if counter.exists() else 0) + 1
        atomic_json(counter, sequence)
        path = root / f'edge-{sequence}.json'
        detected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        automatic = environment.get('VPHONE_AUTORESTART', '1') == '1'
        diagnostics = {'collector_log': str(path.with_suffix('.diagnostics.log'))}
        atomic_json(path, {'rig_id': rig, 'generation': int(generation) if generation else None,
                           'down_edge_sequence': sequence, 'edge_id': str(uuid.uuid4()),
                           'actor': 'vsock-health', 'detected_at': detected_at,
                           'automatic': automatic, 'diagnostics': diagnostics,
                           'delivery': 'pending'})
        emit_recovery_event(environment, 'down_detected', detected_at=detected_at,
                            sock=environment.get('VPHONE_SOCK', ''),
                            diagnostics=diagnostics['collector_log'])
        emit_recovery_event(environment, 'guard_decision',
                            decision='restart' if automatic else 'disabled')
    return path


def deliver(path, control):
    path = Path(path)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        if record['delivery'] == 'delivered':
            return record
        if not record.get('rig_id') or not record.get('generation'):
            raise ValueError('Down observation has no bound Rig generation; automatic recovery refused')
        if not record.get('edge_id'):
            # Persist an identity for pending observations written before BL-156.
            record['edge_id'] = str(uuid.uuid4())
            atomic_json(path, record)
        fields = ('rig_id', 'generation', 'down_edge_sequence', 'edge_id',
                  'actor', 'automatic', 'diagnostics')
        result = control.observe_vphone_down(**{key: record[key] for key in fields})
        record.update(delivery='delivered', recovery_id=result['recovery_id'])
        atomic_json(path, record)
        return record


def main():
    environment = dict(os.environ)
    runtime = environment.get('AUTOPHONE_RIG_RUNTIME_DIR')
    if runtime:
        config = Path(runtime) / 'control-plane.env'
        if config.is_file():
            for line in config.read_text().splitlines():
                key, separator, value = line.partition('=')
                if separator and key in {'AUTOPHONE_DATABASE_URL', 'AUTOPHONE_CONTROL_WS'}:
                    os.environ.setdefault(key, value)
    flush_only = '--flush' in sys.argv[1:]
    if flush_only:
        pending = sorted(observation_root(environment).glob('edge-[0-9]*.json'))
        if not pending:
            return 0
        path = pending[0]
    else:
        path = Path(environment['AUTOPHONE_DOWN_OBSERVATION']) if environment.get('AUTOPHONE_DOWN_OBSERVATION') else persist_edge(environment)
    collector = Path(environment.get('VPHONE_DOWN_COLLECT_SCRIPT') or Path(__file__).resolve().parents[1] / 'scripts/collect_vphone_crash.sh')
    log_path = path.with_suffix('.diagnostics.log')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        if not flush_only and record['delivery'] != 'delivered':
            record['diagnostics'] = {'collector_log': str(log_path)}
            atomic_json(path, record)
    status = 0
    try:
        from autophone.application.control_plane import require_control_plane
        control = require_control_plane()
        for pending in sorted(path.parent.glob('edge-[0-9]*.json')):
            try:
                deliver(pending, control)
            except ValueError:
                status = 1
                continue
    except Exception as exc:
        print(f'Down observation retained at {path}: {type(exc).__name__}')
        status = 1
    # Cancellation and quarantine must not wait for the diagnostic collector.
    if flush_only:
        return status
    anomaly = _record_down_anomaly(environment, path)
    collector_env = None
    if anomaly is not None:
        collector_env = {**os.environ, 'VPHONE_CRASH_OUTPUT_ROOT': str(anomaly.parent / 'bundle')}
    bundle_status, bundle_error = 'complete', ''
    try:
        with log_path.open('wb') as output:
            result = subprocess.run([str(collector), environment.get('VPHONE_DOWN_COLLECT_MIN', '15')],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=90, check=False,
                                    env=collector_env)
        if result.returncode != 0:
            bundle_status, bundle_error = 'failed', f'collector exit {result.returncode}'
    except subprocess.TimeoutExpired:
        bundle_status, bundle_error = 'truncated', 'collector exceeded 90s'
    except OSError as exc:
        bundle_status, bundle_error = 'failed', type(exc).__name__
    _finish_down_anomaly(anomaly, bundle_status, bundle_error)
    return status


def _record_down_anomaly(environment, path):
    """Record the edge as a Host anomaly. Recovery must not depend on any of this."""
    try:
        from autophone import host_anomaly

        target = _down_target(environment)
        if target is None:
            print('Host anomaly not recorded: no target or host-id for this Rig')
            return None
        edge = json.loads(Path(path).read_text())
        try:
            from autophone.application.control_plane import require_control_plane
            control = require_control_plane()
        except Exception:
            control = None
        rig = {'rig_id': edge.get('rig_id'), 'generation': edge.get('generation')}
        targets = [target]
        if control is not None:
            # 指纹要和样本同一个口径，所以要知道同机其他 Rig；取不到就只用自己。
            try:
                targets = control.get_host(target['host_id']).get('targets') or targets
            except Exception as exc:
                print(f'Host targets unavailable: {type(exc).__name__}: {exc}')
        anomaly = host_anomaly.write_record(
            host_anomaly.anomaly_root(environment),
            kind='rig_down',
            detected_at=datetime.fromisoformat(edge['detected_at'].replace('Z', '+00:00')) if edge.get('detected_at') else None,
            host_id=target['host_id'],
            rigs=[rig],
            details={'edge_id': edge.get('edge_id'),
                     'down_edge_sequence': edge.get('down_edge_sequence'),
                     'automatic': edge.get('automatic'),
                     'vm_alive': host_anomaly.vm_process_alive(target, run=host_anomaly.run_text)},
            targets=targets,
            control=control,
        )
        # 诊断包要几十秒才采完，熔断和重跑不能等它，先把记录送进去。
        if control is not None:
            try:
                host_anomaly.deliver_pending(host_anomaly.anomaly_root(environment), target['host_id'], control)
            except Exception as exc:
                print(f'Host anomaly delivery deferred: {type(exc).__name__}: {exc}')
        return anomaly
    except Exception as exc:
        print(f'Host anomaly not recorded: {type(exc).__name__}: {exc}')
        return None


def _down_target(environment):
    """Find this Rig's identity. upstream Rigs have target-N.json, legacy ones device-target.json."""
    runtime = environment.get('AUTOPHONE_RIG_RUNTIME_DIR')
    candidates = [environment.get('AUTOPHONE_RIG_TARGET'),
                  Path(runtime) / 'device-target.json' if runtime else None]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return json.loads(Path(candidate).read_text())
    host_file = Path(runtime) / 'host-id' if runtime else None
    if host_file is None or not host_file.is_file():
        return None
    return {'host_id': host_file.read_text().strip(),
            'rig_id': environment.get('AUTOPHONE_RIG_ID'),
            'vm_dir': environment.get('VM_DIR'),
            'cli_dir': environment.get('VPHONE_CLI_DIR')}


def _finish_down_anomaly(anomaly, status, error):
    if anomaly is None:
        return
    try:
        from autophone import host_anomaly

        host_anomaly.finish_bundle(anomaly, status=status, error=error)
        # 没有 watcher 时也要尽量入库；失败就留在本地，由 watcher 下一轮补投。
        from autophone.application.control_plane import require_control_plane

        record = json.loads(anomaly.read_text())
        host_anomaly.deliver_pending(anomaly.parents[2].parent, record['host_id'], require_control_plane())
    except Exception as exc:
        print(f'Host anomaly delivery deferred: {type(exc).__name__}: {exc}')


if __name__ == '__main__':
    raise SystemExit(main())
