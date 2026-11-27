"""Shared ResetReceipt semantics for executors and the control authority."""
from __future__ import annotations

import copy
import hashlib
import json

from autophone.contracts import validate_or_raise


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def same_json_value(left, right):
    """Compare JSON values recursively; booleans and numbers are distinct JSON types."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_json_value(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_json_value(a, b) for a, b in zip(left, right))
    return left == right


def disposition(axis, verification, *, required):
    if not required:
        return 'not_required'
    if verification.get('status') != 'passed':
        return 'quarantine' if axis == 'device_axis' else 'failed'
    return 'reusable' if axis == 'device_axis' else 'restored'


def verify_readback(target, expected, raw):
    if not isinstance(raw, dict):
        return {'status': 'failed', 'reason': 'readback_missing', 'readback': raw}
    valid = (raw.get('ok') is True and same_json_value(raw.get('target'), target)
             and isinstance(raw.get('observed'), dict) and bool(expected)
             and all(k in raw['observed'] and same_json_value(raw['observed'][k], v) for k, v in expected.items())
             and raw.get('allocatable') is True and bool(raw.get('authority')))
    return {'status': 'passed' if valid else 'failed',
            'reason': None if valid else 'readback_identity_or_state_mismatch', 'readback': raw}


def validate_reset_receipt(receipt, *, binding=None, policy=None):
    validate_or_raise('reset-receipt.v0.json', receipt)
    unsigned = {k: v for k, v in receipt.items() if k != 'record_sha256'}
    if checksum(unsigned) != receipt['record_sha256']:
        raise ValueError('reset receipt checksum mismatch')
    if binding is not None and receipt['binding'] != binding:
        raise ValueError('reset receipt binding mismatch')
    if policy is not None and checksum(policy) != receipt['policy_sha256']:
        raise ValueError('reset receipt policy mismatch')
    for axis in ('device_axis', 'external_fixture_axis'):
        row = receipt[axis]
        required = row['required']
        if policy is not None:
            plan = policy[axis]
            if required != (axis == 'device_axis' or plan.get('required', False)):
                raise ValueError('reset required axis mismatch')
            if not same_json_value(row['target'], plan.get('target', {})) or not same_json_value(row['expected'], plan.get('expected', {})):
                raise ValueError('reset target or expected baseline mismatch')
        if required:
            actual = verify_readback(row['target'], row['expected'], row['verification'].get('readback'))
            if actual != row['verification']:
                raise ValueError('reset verification does not match independent readback')
        elif row['verification']['status'] != 'not_required':
            raise ValueError('unrequested reset axis cannot claim verification')
        if row['disposition'] != disposition(axis, row['verification'], required=required):
            raise ValueError('reset disposition does not follow verification')
    return receipt

def validate_reset_spec(raw):
    if raw is not None and not isinstance(raw, dict):
        raise ValueError('reset policy must be an object')
    raw = copy.deepcopy(dict(raw or {}))
    if raw and raw.get('schema_version') != 'reset-spec/v0':
        raise ValueError('unsupported reset policy; explicit reset-spec/v0 required')
    raw.setdefault('schema_version', 'reset-spec/v0')
    validate_or_raise('reset-spec.v0.json', raw)
    raw.setdefault('device_axis', {'strategy': 'verify_ready'})
    device = raw['device_axis']
    expected = {'guest_ready': True, **(
        {'app_stopped': True} if device['strategy'] == 'terminate_app' else {})}
    if not same_json_value(device.get('expected', expected), expected):
        raise ValueError('unsupported device reset baseline')
    device['expected'] = expected
    raw.setdefault('external_fixture_axis', {'required': False})
    return raw


def effective_policy(raw, target):
    raw = validate_reset_spec(raw)
    raw['device_axis']['target'] = {'host_id': target.host_id, 'rig_id': target.rig_id,
                                  'rig_generation': target.rig_generation}
    return raw
