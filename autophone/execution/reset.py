"""Two independent reset axes over explicitly configured action/readback adapters."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from urllib.request import Request

from autophone.device.stage_guard import StageAccessDenied, StageGuard
from autophone.reset_contract import checksum, disposition, same_json_value, verify_readback, validate_reset_receipt


def reset_binding(assignment, context):
    return dict(assignment_id=str(assignment['assignment_id']),
                attempt_no=int(assignment['attempt']['attempt_no']),
                fencing=int(assignment['attempt']['fencing']),
                host_id=context.target.host_id, rig_id=context.target.rig_id,
                rig_generation=context.target.rig_generation)


class DeviceResetAdapter:
    """v0 supports read-only readiness and termination with independent app_list."""
    def __init__(self, session):
        self.session = session

    def act(self, plan):
        strategy = plan['strategy']
        if strategy == 'terminate_app':
            self.session.terminate_app(plan['bundle_id'])
        elif strategy != 'verify_ready':
            raise ValueError('unsupported device reset strategy')
        return {'status': 'succeeded' if strategy == 'terminate_app' else 'not_required'}

    def readback(self, plan):
        target = self.session.target
        apps = self.session.app_list()
        if not isinstance(apps, list):
            raise ValueError('invalid app_list readback')
        observed = {'guest_ready': True}
        if plan['strategy'] == 'terminate_app':
            observed['app_stopped'] = not any(
                (x.get('bundle_id') or x.get('bundleIdentifier')) == plan['bundle_id']
                and int(x.get('pid') or 0) > 0 for x in apps)
        return dict(ok=True, target={'host_id': target.host_id, 'rig_id': target.rig_id,
                    'rig_generation': target.rig_generation}, observed=observed,
                    allocatable=True, authority='device.app_list', apps=apps)


class HttpFixtureResetAdapter:
    """Profile-owned endpoints; the action response is never the authority readback."""
    def __init__(self, config):
        self.config = copy.deepcopy(config)

    def _request(self, kind, plan):
        from urllib.parse import quote, urlparse
        from urllib.request import HTTPRedirectHandler, build_opener
        if plan['target'] not in self.config.get('allowed_targets', []):
            raise ValueError('Fixture target outside configured operation scope')
        if not same_json_value(plan['expected'], self.config.get('expected')):
            raise ValueError('Fixture baseline outside configured operation scope')
        endpoint = self.config[kind]
        if urlparse(endpoint).scheme not in {'http', 'https'}:
            raise ValueError('fixture endpoint must be HTTP(S)')
        # The plan cannot substitute a host, method or credential.
        url = endpoint.replace('{resource_id}', quote(plan['target']['resource_id'], safe=''))
        headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}
        if self.config.get('token_env'):
            headers['Authorization'] = 'Bearer ' + os.environ[self.config['token_env']]
        data = None if kind == 'readback_url' else json.dumps({
            'target': plan['target'], 'expected': plan['expected'],
        }).encode()
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Fixture redirects are not authorized')
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method='GET' if data is None else 'POST'),
                     timeout=float(self.config.get('timeout_s', 5))) as response:
            raw = json.loads(response.read(1024 * 1024))
        if kind == 'readback_url':
            raw['authority'] = url
        return raw

    def act(self, plan):
        raw = self._request('action_url', plan)
        return {'status': 'succeeded' if raw.get('ok') is True else 'failed', 'response': raw}

    def readback(self, plan):
        return self._request('readback_url', plan)



class ResetCoordinator:
    def __init__(self, device, *, fixture_adapters=None):
        self.session = device
        self.device = DeviceResetAdapter(device)
        self.fixture_adapters = fixture_adapters or {}

    def reset(self, policy, *, binding):
        from autophone.contracts import validate_or_raise
        validate_or_raise('reset-spec.v0.json', policy)
        receipt = dict(schema_version='reset-receipt/v0', binding=copy.deepcopy(binding),
                       policy_sha256=checksum(policy), observed_at=datetime.now(timezone.utc).isoformat())
        adapters = {}
        for axis in ('device_axis', 'external_fixture_axis'):
            plan = policy[axis]
            required = axis == 'device_axis' or plan.get('required', False)
            row = dict(required=required, target=copy.deepcopy(plan.get('target', {})),
                       expected=copy.deepcopy(plan.get('expected', {})),
                       action_result={'status': 'not_required'},
                       verification={'status': 'not_required'})
            adapter = self.device if axis == 'device_axis' else self.fixture_adapters.get(plan.get('adapter'))
            adapters[axis] = adapter
            if required:
                if adapter is None:
                    row['action_result'] = {'status': 'failed', 'reason': 'reset_adapter_unavailable'}
                else:
                    self.session.write_fence.check()
                    try:
                        row['action_result'] = adapter.act(plan)
                    except StageAccessDenied:
                        raise
                    except Exception as exc:
                        row['action_result'] = {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}
            receipt[axis] = row
        # Final readbacks follow *both* actions: a Fixture compensation may change
        # device state, so a device observation made before it cannot admit reuse.
        for axis in ('device_axis', 'external_fixture_axis'):
            row, adapter = receipt[axis], adapters[axis]
            if row['required']:
                raw = None
                if adapter is not None:
                    self.session.write_fence.check()
                    try:
                        raw = adapter.readback(policy[axis])
                    except StageAccessDenied:
                        raise
                    except Exception:
                        pass
                row['verification'] = verify_readback(row['target'], row['expected'], raw)
            row['disposition'] = disposition(axis, row['verification'], required=row['required'])
        receipt['runtime_readiness'] = {name: 'unprobed' for name in ('host', 'guest', 'ssh', 'vphoned', 'frida', 'app')}
        if receipt['device_axis']['verification']['status'] == 'passed':
            receipt['runtime_readiness'].update(host='ready', guest='ready', vphoned='ready')
        receipt['record_sha256'] = checksum(receipt)
        return validate_reset_receipt(receipt, binding=binding, policy=policy)


def reset_not_performed(policy, *, binding, reason):
    """复位未执行时的回执（BL-224）：未取得 ScreenLock，不向设备或 Fixture 发动作与回读。

    每条必需轴记 ``action_result={'status': 'failed', 'reason': reason}``，校验按「无回读」
    得出 ``failed``，处置随之为 ``quarantine`` / ``failed``——与回读失败同一判定，不声称
    设备已复位，也不放宽控制面对回执的独立复核。
    """
    from autophone.contracts import validate_or_raise
    validate_or_raise('reset-spec.v0.json', policy)
    receipt = dict(schema_version='reset-receipt/v0', binding=copy.deepcopy(binding),
                   policy_sha256=checksum(policy), observed_at=datetime.now(timezone.utc).isoformat())
    for axis in ('device_axis', 'external_fixture_axis'):
        plan = policy[axis]
        required = axis == 'device_axis' or plan.get('required', False)
        row = dict(required=required, target=copy.deepcopy(plan.get('target', {})),
                   expected=copy.deepcopy(plan.get('expected', {})),
                   action_result={'status': 'not_required'}, verification={'status': 'not_required'})
        if required:
            row['action_result'] = {'status': 'failed', 'reason': reason}
            row['verification'] = verify_readback(row['target'], row['expected'], None)
        row['disposition'] = disposition(axis, row['verification'], required=required)
        receipt[axis] = row
    receipt['runtime_readiness'] = {name: 'unprobed' for name in ('host', 'guest', 'ssh', 'vphoned', 'frida', 'app')}
    receipt['record_sha256'] = checksum(receipt)
    return validate_reset_receipt(receipt, binding=binding, policy=policy)
