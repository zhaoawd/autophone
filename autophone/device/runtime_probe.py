"""Read-only, target-bound admission evidence. No cross-Run cache in v0."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
import time

from .stage_guard import OperationClass, StageAccessDenied, StageGuard


class RuntimeAdmissionError(RuntimeError):
    def __init__(self, blockers):
        self.blockers = blockers
        super().__init__('runtime_admission_blocked: ' + ', '.join(blockers))


def runtime_identity(response):
    """Validate observed process identity; retain legacy manifest compatibility."""
    if not isinstance(response, dict) or response.get('ok') is not True:
        return None
    if response.get('source') == 'autophone-process-observation/v1':
        host, guest, binding = (response.get(key) for key in ('host', 'guest', 'binding'))
        def process_valid(value):
            return (isinstance(value, dict) and type(value.get('pid')) is int and value['pid'] > 0
                    and isinstance(value.get('process_start'), str) and bool(value['process_start'])
                    and isinstance(value.get('path'), str) and value['path'].startswith('/')
                    and re.fullmatch('[0-9a-f]{64}', str(value.get('binary_sha256') or '')))
        if not (process_valid(host) and process_valid(guest) and guest.get('boot_id')
                and isinstance(binding, dict) and binding.get('path') and binding.get('rig_id')
                and type(binding.get('rig_generation')) is int and binding['rig_generation'] >= 0
                and type(binding.get('device')) is int and type(binding.get('inode')) is int
                and response.get('contract_version') == 'v1'):
            return None
        return copy.deepcopy({key: response[key] for key in
                              ('source', 'contract_version', 'host', 'guest', 'binding')})
    host, guest = response.get('host'), response.get('guest')
    if not isinstance(host, dict) or not isinstance(guest, dict):
        return None
    if not all(re.fullmatch('[0-9a-f]{64}', str(v or '')) for v in (
        host.get('binary_sha256'), guest.get('binary_sha256'), guest.get('expected_sha256'),
    )):
        return None
    if not (host.get('contract_version') and guest.get('protocol') and guest.get('boot_id')
            and type(guest.get('connection_generation')) is int
            and guest['connection_generation'] >= 0 and guest.get('connected') is True):
        return None
    if guest['binary_sha256'] != guest['expected_sha256']:
        return None
    return {'host': {k: host.get(k) for k in ('binary_sha256', 'contract_version', 'pid', 'session')},
            'guest': {k: guest.get(k) for k in ('binary_sha256', 'expected_sha256', 'protocol',
                                             'boot_id', 'connection_generation')},
            'frida': copy.deepcopy(response.get('frida') or {})}


def deployed_identity_complete(identity):
    if identity and identity.get('source') == 'autophone-process-observation/v1':
        return runtime_identity(dict(identity, ok=True)) is not None
    return bool(identity and type(identity['host'].get('pid')) is int
                and identity['host']['pid'] > 0 and identity['host'].get('session')
                and identity['frida'].get('host_version') and identity['frida'].get('server_version'))


class RuntimeCapabilityProbe:
    def probe(self, session, requirements, *, timeout_s=3.0):
        if timeout_s <= 0:
            raise ValueError('positive runtime probe timeout required')
        guard = StageGuard.for_device_call()
        if guard is not None:
            guard.assert_allowed(OperationClass.READ, rig_id=session.target.rig_id,
                                 generation=session.target.rig_generation,
                                 socket_path=session.target.socket)
        started = time.monotonic()
        observed_at = datetime.now(timezone.utc).isoformat()
        raw, reason = {}, None
        try:
            raw = session.runtime_info(timeout_s=timeout_s)
        except StageAccessDenied:
            raise
        except Exception as exc:
            reason = f'{type(exc).__name__}: {exc}'
            raw = {'ok': False, 'error': reason}
        identity = runtime_identity(raw)
        results = {}
        identity_drift = False
        for name, requirement in requirements.items():
            mode = requirement.get('mode', 'required')
            version = requirement.get('contract_version', 'v1')
            if mode not in {'required', 'advisory', 'disabled'}:
                raise ValueError('invalid runtime admission mode')
            item = dict(state='unprobed', contract_version=version, mode=mode,
                        declaration=None, readback=None, observed_at=observed_at,
                        connection_generation=(identity or {}).get('guest', {}).get('connection_generation'),
                        reason=reason or 'runtime_manifest_unavailable')
            if mode == 'disabled':
                item['reason'] = 'disabled'
            elif identity is not None:
                local_observation = identity.get('source') == 'autophone-process-observation/v1'
                # This is Autophone's adapter contract, not an upstream declaration.
                declaration = ({'contract_version': 'v1', 'source': 'autophone-adapter'}
                               if local_observation and name in {'runtime_deployed', 'app_list'}
                               else raw.get('capabilities', {}).get(name))
                item['declaration'] = copy.deepcopy(declaration)
                if name == 'runtime_deployed':
                    observed_version = (identity.get('contract_version') if local_observation
                                        else identity['host']['contract_version'])
                    state = ('mismatch' if observed_version != version else
                             'supported' if deployed_identity_complete(identity) else 'unprobed')
                    item.update(state=state, readback=copy.deepcopy(identity),
                                reason=None if state == 'supported' else 'deployed_identity_incomplete_or_mismatch')
                elif not isinstance(declaration, dict):
                    item.update(state='unsupported', reason='capability_not_declared')
                elif declaration.get('contract_version') != version:
                    item.update(state='mismatch', reason='contract_version_mismatch')
                elif declaration.get('supported') is False:
                    item.update(state='unsupported', reason='runtime_declared_unsupported')
                elif name != 'app_list':
                    item['reason'] = 'semantic_probe_unavailable'
                elif time.monotonic() - started >= timeout_s:
                    item['reason'] = 'probe_deadline_exceeded'
                else:
                    try:
                        apps = session.app_list(timeout_s=max(.01, timeout_s - (time.monotonic() - started)))
                        # Re-observe process/socket identity across the semantic roundtrip.
                        after = session.runtime_info(timeout_s=max(.01, timeout_s - (time.monotonic() - started)))
                        item['readback'] = {'apps': apps, 'runtime_info': after}
                        after_identity = runtime_identity(after)
                        if after_identity != identity:
                            identity_drift = after_identity is not None
                            item.update(state='mismatch', reason='runtime_identity_drift')
                        elif not isinstance(apps, list):
                            item['reason'] = 'invalid_app_list'
                        else:
                            item.update(state='supported', reason=None)
                    except StageAccessDenied:
                        raise
                    except Exception as exc:
                        item['reason'] = f'{type(exc).__name__}: {exc}'
            results[name] = item
        if identity_drift:
            # All capability results share this observation window. A known new
            # identity invalidates the initial runtime claim even when app_list
            # was advisory; preserve both raw observations as evidence.
            identity = None
            deployed = results.get('runtime_deployed')
            if deployed is not None and deployed['mode'] != 'disabled':
                deployed.update(state='mismatch', reason='runtime_identity_drift')
        snapshot = dict(schema_version='runtime-capability/v0', observed_at=observed_at,
                        runtime_info=raw, runtime_identity=identity, capabilities=results)
        from autophone.contracts import validate_or_raise
        validate_or_raise('runtime-capability.v0.json', snapshot)
        return snapshot

    @staticmethod
    def assert_admitted(snapshot):
        blockers = [name for name, item in snapshot['capabilities'].items()
                    if item['mode'] == 'required' and item['state'] != 'supported']
        if blockers:
            raise RuntimeAdmissionError(blockers)
