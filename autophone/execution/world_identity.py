"""RunWorldIdentity v0: executor observations, never deployment claims from Git."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace

from autophone.device.runtime_probe import RuntimeCapabilityProbe, RuntimeAdmissionError, deployed_identity_complete
from autophone.device.session import VPhoneDeviceSession
from autophone.device.stage_guard import StageAccessDenied, StageGuard
from autophone.paths import CODE_ROOT

_DEVICE = ContextVar('world_device', default=None)
_ASSIGNMENT = ContextVar('world_assignment', default=None)

_GUEST_TIME_CONTEXT = r'''set -eu
guest_epoch=$(date +%s)
guest_offset=$(date +%z)
guest_timezone=$(date +%Z)
printf '%s\n%s\n%s\n' "$guest_epoch" "$guest_offset" "$guest_timezone"
'''



def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def source_ref(root):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, timeout=10).stdout
    try:
        commit = git('rev-parse', 'HEAD').decode().strip()
        branch = git('symbolic-ref', '--short', '-q', 'HEAD').decode().strip()
    except subprocess.CalledProcessError:
        try:
            commit, branch = git('rev-parse', 'HEAD').decode().strip(), None
        except (OSError, subprocess.SubprocessError):
            return {'state': 'unprobed', 'repo_root': str(root), 'reason': 'git_unavailable'}
    except (OSError, subprocess.SubprocessError):
        return {'state': 'unprobed', 'repo_root': str(root), 'reason': 'git_unavailable'}
    try:
        diff = git('diff', '--binary', 'HEAD', '--')
        # Index flags hide changes from status/diff. Hash their actual bytes separately.
        hidden = {}
        hidden_dirty = False
        for entry in git('ls-files', '-v', '-z').split(b'\0'):
            if entry and (entry[:1].islower() or entry[:1] == b'S'):
                name = os.fsdecode(entry[2:])
                hidden[name] = file_digest(root / name)
                try:
                    baseline = hashlib.sha256(git('show', 'HEAD:' + name)).hexdigest()
                except subprocess.CalledProcessError:
                    baseline = None
                hidden_dirty = hidden_dirty or hidden[name] != baseline
        untracked = {}
        for entry in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if entry:
                name = os.fsdecode(entry)
                # Source/asset evidence excludes generated Run and experiment output.
                if name.endswith(('.py', '.yaml', '.yml', '.toml', '.json', '.js', '.ts', '.swift')) and not name.startswith(('evidence/', 'runs/', 'experiments/')):
                    untracked[name] = file_digest(root / name)
        facts = dict(commit=commit, branch=branch, tracked_diff_sha256=hashlib.sha256(diff).hexdigest(),
                     dirty=bool(diff or untracked or hidden_dirty), hidden_index_files=hidden, untracked=untracked)
        return dict(state='observed', repo_root=str(root), **facts, fingerprint=digest(facts))
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(state='partial', repo_root=str(root), commit=commit, branch=branch,
                    reason=type(exc).__name__)


@contextmanager
def assignment_scope(binding):
    token = _ASSIGNMENT.set(copy.deepcopy(binding))
    try:
        yield
    finally:
        _ASSIGNMENT.reset(token)


@contextmanager
def device_scope(device):
    token = _DEVICE.set(device)
    try:
        yield
    finally:
        _DEVICE.reset(token)


def explicit_session():
    session = _DEVICE.get()
    if session is not None:
        return session
    from autophone.device.target import resolve_target
    target = resolve_target()
    guard = StageGuard.current()
    endpoint = SimpleNamespace(host_id='compatibility', rig_id=guard.rig_id,
        rig_generation=guard.generation, cli_dir=target.cli_dir, vm_dir=target.vm_dir,
        socket=target.sock, frida_endpoint=guard.frida_endpoint or '127.0.0.1:27042',
        ssh_host=(guard.ssh_endpoint or ('127.0.0.1', 2222))[0],
        ssh_port=(guard.ssh_endpoint or ('127.0.0.1', 2222))[1],
        config_path=target.vm_dir / 'config.plist')
    return VPhoneDeviceSession(endpoint, SimpleNamespace(check=lambda: guard.assert_allowed('device_read')))


def _offset_text(value: str) -> str:
    if len(value) != 5 or value[0] not in '+-' or not value[1:].isdigit():
        raise ValueError('guest UTC offset is invalid')
    hours, minutes = int(value[1:3]), int(value[3:5])
    if hours > 23 or minutes > 59:
        raise ValueError('guest UTC offset is invalid')
    return f'{value[:3]}:{value[3:]}'


def capture_time_context(session, *, wall_clock=None):
    """Observe Host and Guest clocks without changing either clock."""
    wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
    before = wall_clock().astimezone(timezone.utc)
    try:
        result = session.shell(_GUEST_TIME_CONTEXT, timeout_ms=3_000)
    except StageAccessDenied:
        raise
    except Exception as exc:
        after = wall_clock().astimezone(timezone.utc)
        midpoint = datetime.fromtimestamp(
            (before.timestamp() + after.timestamp()) / 2,
            tz=timezone.utc,
        )
        host_local = midpoint.astimezone()
        return {
            'state': 'unprobed',
            'sampled_at': midpoint.isoformat(),
            'host_wall_clock': midpoint.isoformat(),
            'host_timezone': str(host_local.tzinfo),
            'host_utc_offset': _offset_text(host_local.strftime('%z')),
            'guest': {'state': 'unprobed', 'reason': f'{type(exc).__name__}: {exc}'[:300]},
            'observation_source': 'guest_shell',
            'time_control_source': 'unprobed',
        }
    after = wall_clock().astimezone(timezone.utc)
    midpoint = datetime.fromtimestamp(
        (before.timestamp() + after.timestamp()) / 2,
        tz=timezone.utc,
    )
    host_local = midpoint.astimezone()
    base = {
        'sampled_at': midpoint.isoformat(),
        'host_wall_clock': midpoint.isoformat(),
        'host_timezone': str(host_local.tzinfo),
        'host_utc_offset': _offset_text(host_local.strftime('%z')),
        'observation_source': 'guest_shell',
        'time_control_source': 'unprobed',
    }
    try:
        if result.code != 0 or result.timed_out or result.truncated:
            raise ValueError(
                f'guest time command incomplete: exit={result.code} '
                f'timed_out={result.timed_out} truncated={result.truncated}'
            )
        parts = result.stdout.strip().splitlines()
        if len(parts) != 3:
            raise ValueError('guest time output is invalid')
        epoch = int(parts[0])
        if epoch <= 0 or not parts[2].strip():
            raise ValueError('guest time output is invalid')
        offset = _offset_text(parts[1].strip())
        sign = 1 if offset[0] == '+' else -1
        guest_timezone = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))
        guest_wall_clock = datetime.fromtimestamp(epoch, tz=guest_timezone)
    except (AttributeError, TypeError, ValueError) as exc:
        return {
            **base,
            'state': 'unprobed',
            'guest': {'state': 'unprobed', 'reason': str(exc)[:300]},
        }
    skew_lower_ms = (epoch - after.timestamp()) * 1_000
    skew_upper_ms = (epoch + 1 - before.timestamp()) * 1_000
    return {
        **base,
        'state': 'observed',
        'guest': {
            'state': 'observed',
            'wall_clock': guest_wall_clock.isoformat(),
            'timezone': parts[2].strip(),
            'utc_offset': offset,
            'epoch_s': epoch,
        },
        'clock_skew_ms': round((skew_lower_ms + skew_upper_ms) / 2, 3),
        'clock_skew_basis': 'guest_minus_host',
        'sampling_uncertainty_ms': round((skew_upper_ms - skew_lower_ms) / 2, 3),
    }


GUEST_GLOBAL_PREFERENCES = '/var/mobile/Library/Preferences/.GlobalPreferences.plist'


def capture_locale_context(session):
    """Read the device language settings without changing them (BL-207/BL-213).

    Source: the mobile user's global preferences file, fetched read-only via UDS
    ``file_get`` and parsed on the host. ``preferred_language`` is the first
    ``AppleLanguages`` entry; ``current_locale`` is ``AppleLocale``, the device-level
    region setting. An App's in-process ``NSLocale.currentLocale`` also depends on the
    App's localizations and may differ (on vm, BL-207 read ``en_CN`` inside the App on
    2026-10-02; this file held ``zh_CN`` on 2026-10-08), hence
    ``current_locale_scope='device'``. Any failure records
    ``unknown`` with a reason and never blocks the Run.
    """
    base = {
        'observation_source': 'guest_global_preferences',
        'source_path': GUEST_GLOBAL_PREFERENCES,
        'current_locale_scope': 'device',
    }

    def unknown(reason):
        return {**base, 'state': 'unprobed', 'preferred_language': 'unknown',
                'preferred_languages': [], 'current_locale': 'unknown', 'reason': str(reason)[:300]}

    try:
        with tempfile.TemporaryDirectory(prefix='autophone-locale-') as temporary:
            local = session.get_file(GUEST_GLOBAL_PREFERENCES, Path(temporary) / 'GlobalPreferences.plist')
            data = plistlib.loads(Path(local).read_bytes())
    except StageAccessDenied:
        raise
    except Exception as exc:
        return unknown(f'{type(exc).__name__}: {exc}')
    if not isinstance(data, dict):
        return unknown('global preferences is not a dictionary')
    languages = data.get('AppleLanguages')
    languages = [str(item) for item in languages if isinstance(item, str) and item] \
        if isinstance(languages, list) else []
    locale = data.get('AppleLocale')
    locale = locale if isinstance(locale, str) and locale else None
    missing = [key for key, value in (('AppleLanguages', languages), ('AppleLocale', locale)) if not value]
    context = {
        **base,
        'state': 'observed' if not missing else 'partial' if len(missing) == 1 else 'unprobed',
        'preferred_language': languages[0] if languages else 'unknown',
        'preferred_languages': languages,
        'current_locale': locale or 'unknown',
    }
    if missing:
        context['reason'] = 'missing ' + ', '.join(missing)
    return context


def info_plist_observation(expected_ref, *, observation, device_sha256, root=None, code_root=None):
    """BL-241：记录设备与 IPA 的 Info.plist 字节摘要。

    ``comparison`` 为 ``equal``、``different`` 或 ``not_compared``；任何取值都不改变
    ``subject_build_ref.state``（该字段仍只表示 build marker 与版本元组）。宿主侧取值失败、
    设备回读失败或没有比较基准时记 ``not_compared`` 与原因，不抛异常。``kind=unknown`` 的
    Build 由 ``run_submission.compare_installed_build`` 结合 ``info_plist_basis`` 判定
    ``info_plist`` 粒度（BL-241 第二步）。
    """
    if not (isinstance(device_sha256, str) and len(device_sha256) == 64
            and all(ch in '0123456789abcdef' for ch in device_sha256)):
        device_sha256 = None
    record = {'comparison': 'not_compared', 'device_sha256': device_sha256,
              'expected_sha256': None,
              'basis_build_id': expected_ref.get('build_id') if expected_ref else None,
              'reason': None}
    if expected_ref:
        try:
            from autophone.packages.build_identity import expected_info_plist_sha256
            record['expected_sha256'] = expected_info_plist_sha256(
                expected_ref, root=root, code_root=code_root)
        except Exception as exc:  # 宿主侧不可核对：只记录原因
            record['reason'] = f'expected_info_plist_unavailable:{type(exc).__name__}: {exc}'
    else:
        record['reason'] = 'comparison_basis_missing'
    if record['reason'] is None and not observation:
        record['reason'] = 'device_observation_missing'
    elif record['reason'] is None and not record['device_sha256']:
        record['reason'] = 'device_info_plist_unavailable'
    if record['reason'] is None:
        record['comparison'] = ('equal' if record['device_sha256'] == record['expected_sha256']
                                else 'different')
    return record


def info_plist_basis_record(expected_ref, *, root=None, code_root=None):
    """BL-241 第二步：比较基准 Build 的宿主侧 Info.plist 基准（只读，不写回任何记录）。

    取值见 ``autophone.packages.build_identity.info_plist_basis``。没有比较基准或宿主侧
    不可核对（IPA 缺失、复算 sha256 不符、Info.plist 不可读）时记原因，``uniqueness`` 为
    ``not_evaluated``，不抛异常；判定方见到原因即不产出 ``match``。
    """
    record = {'build_id': expected_ref.get('build_id') if expected_ref else None,
              'source_revision_kind': None, 'info_plist_sha256': None,
              'ipa_short_version': None, 'ipa_build_version': None,
              'uniqueness': 'not_evaluated', 'same_info_plist_build_ids': [],
              'unverified_build_ids': [], 'reason': None}
    if not expected_ref:
        record['reason'] = 'comparison_basis_missing'
        return record
    try:
        from autophone.packages.build_identity import info_plist_basis
        return info_plist_basis(expected_ref, root=root, code_root=code_root)
    except Exception as exc:  # 宿主侧不可核对：只记录原因
        record['reason'] = f'info_plist_basis_unavailable:{type(exc).__name__}: {exc}'
        return record


def capture_world(spec, profile, run_dir, *, session=None):
    session = session or explicit_session()
    target = session.target
    probe = RuntimeCapabilityProbe()
    requirements = spec.get('runtime_requirements') or {}
    snapshot = probe.probe(session, requirements)
    deployed = snapshot['runtime_identity']
    registry, installed_ref, candidate_ref = {}, None, None
    submission = Path(run_dir) / 'run-submission.json'
    if submission.is_file():
        submission_data = json.loads(submission.read_text())
        registry = submission_data.get('registry_build_ref') or {}
        candidate_ref = submission_data.get('candidate_build_ref')
        installed_ref = candidate_ref or submission_data.get('installed_build_ref')
    observation, reason = None, None
    device_info_plist_sha256 = None
    try:
        installed = session.read_installed_build(str(spec['app']), str(profile.bundle_id))
        observation = installed.to_dict() if installed is not None else None
        device_info_plist_sha256 = getattr(installed, 'info_plist_sha256', None)
    except StageAccessDenied:
        raise
    except Exception as exc:
        reason = f'{type(exc).__name__}: {exc}'
    match = 'unprobed'
    if observation:
        pairs = [((candidate_ref or registry).get(k), observation.get(k)) for k in ('bundle_id', 'short_version', 'build_version')]
        if any(a is not None and b is not None and a != b for a, b in pairs):
            match = 'mismatch'
        else:
            match = 'partial'
    expected_ref = candidate_ref or (registry if registry.get('artifact_sha256') else installed_ref)
    if observation and expected_ref:
        if any(expected_ref.get(key) is not None and observation.get(key) != expected_ref[key]
               for key in ('short_version', 'build_version')):
            match = 'mismatch'
    if observation and match != 'mismatch' and expected_ref:
        try:
            from autophone.packages.build_identity import expected_build_marker
            expected = expected_build_marker(expected_ref,
                root=getattr(profile, 'host_state_root', None),
                code_root=getattr(profile, 'root', None))
            actual = session.read_build_marker(str(profile.bundle_id))
            observation['build_marker_sha256'] = hashlib.sha256(actual).hexdigest()
            match = 'observed' if actual == expected else 'mismatch'
            if match == 'mismatch':
                reason = 'installed_build_marker_mismatch'
        except StageAccessDenied:
            raise
        except Exception as exc:
            match, reason = 'partial', f'build_marker_unverified:{type(exc).__name__}: {exc}'
    info_plist = info_plist_observation(
        expected_ref, observation=observation, device_sha256=device_info_plist_sha256,
        root=getattr(profile, 'host_state_root', None), code_root=getattr(profile, 'root', None))
    info_plist_basis = info_plist_basis_record(
        expected_ref, root=getattr(profile, 'host_state_root', None),
        code_root=getattr(profile, 'root', None))
    root = Path(getattr(profile, 'root', None) or CODE_ROOT)
    app_dir = root / 'apps' / str(spec['app'])
    assets = {str(p.relative_to(root)): file_digest(p) for p in sorted(app_dir.rglob('*'))
              if p.is_file() and p.suffix in {'.yaml', '.yml', '.py', '.json'}}
    executor = source_ref(CODE_ROOT)
    try:
        process_start = subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'lstart='],
                                       capture_output=True, text=True, timeout=3).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        process_start = None
    if process_start is None:
        executor['state'] = 'partial'
    executor.update(pid=os.getpid(), process_start=process_start)
    world = dict(schema_version='run-world-identity/v0', run_id=str(spec['run_id']),
        observed_at=datetime.now(timezone.utc).isoformat(),
        platform_source_ref=dict(state='partial', executor_code_ref=executor,
            asset_catalog_ref=dict(state='partial', root=str(root), files=assets, sha256=digest(assets),
                                   reason='app_assets_observed; global_assets_not_frozen')),
        vphone_source_ref=source_ref(target.cli_dir),
        deployed_runtime_ref=dict(state=('observed' if deployed_identity_complete(deployed) else
                                        'partial' if deployed else 'unprobed'), value=deployed),
        vm_instance_ref=dict(state='partial', host_id=target.host_id, rig_id=target.rig_id,
            rig_generation=target.rig_generation, vm_dir=str(target.vm_dir), socket=str(target.socket),
            config_sha256=file_digest(target.vm_dir / 'config.plist'),
            boot_id=(deployed or {}).get('guest', {}).get('boot_id'),
            reason='disk_lineage_unprobed'),
        subject_build_ref=dict(state=match, registry=registry, installed_build_ref=installed_ref, device_observation=observation,
                               reason=reason, subject_epoch=None, info_plist=info_plist,
                               info_plist_basis=info_plist_basis),
        runtime_capabilities=snapshot,
        time_context=capture_time_context(session),
        locale_context=capture_locale_context(session),
        evidence_cutoff={})
    from .closure_journal import ClosureJournal
    journal = ClosureJournal(run_dir)
    if not journal.path.exists():
        journal.open(run_id=spec['run_id'], device=spec['device'], assignment=_ASSIGNMENT.get())
    row = journal.read()
    world['assignment_binding'] = row.get('assignment')
    world['record_sha256'] = digest(world)
    previous = row.get('world_identity')
    if previous is not None:
        assert_world_unchanged(previous, world)
        world = previous
    else:
        journal.freeze_world(world, expected_version=row['version'])
    journal.materialize_evidence()
    probe.assert_admitted(snapshot)
    blockers = [name for name, mode in (spec.get('identity_requirements') or {}).items()
                if mode == 'required' and world[name]['state'] != 'observed']
    if match == 'mismatch':
        blockers.append('subject_build_ref')
    if blockers:
        raise RuntimeAdmissionError(blockers)
    return world


def assert_world_unchanged(frozen, observed):
    """Re-entry into an execution boundary cannot silently adopt a new world."""
    changed = []
    for name in ('vphone_source_ref', 'deployed_runtime_ref', 'vm_instance_ref', 'subject_build_ref'):
        before, after = frozen[name], observed[name]
        if name == 'subject_build_ref':
            # info_plist_basis 是宿主 Package Store 的派生事实（含唯一性扫描），不是设备世界；
            # 运行中入库同一 App 的其他 Build 不构成漂移，沿用首次冻结的值（BL-241）。
            before, after = ({k: v for k, v in (value or {}).items() if k != 'info_plist_basis'}
                             for value in (before, after))
        if before != after:
            changed.append(name)
    for name in ('executor_code_ref', 'asset_catalog_ref'):
        if frozen['platform_source_ref'][name] != observed['platform_source_ref'][name]:
            changed.append(name)
    if frozen['assignment_binding'] != observed['assignment_binding']:
        changed.append('assignment_binding')
    if changed:
        raise RuntimeAdmissionError(['identity_drift:' + name for name in changed])
