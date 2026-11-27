"""PostgreSQL owns Fixture admission and consumes fenced ResetReceipt results."""
from __future__ import annotations

from types import SimpleNamespace
from ._psycopg import jsonb as Jsonb

from autophone.reset_contract import checksum, effective_policy, same_json_value, verify_readback, validate_reset_receipt


def fixture_plan(assignment):
    policy = assignment.get('reset_policy') or {}
    plan = policy.get('external_fixture_axis') or {}
    return plan if plan.get('required') is True else None


def wait_reason(connection, assignment):
    plan = fixture_plan(assignment)
    if plan is None:
        return None
    if not plan.get('target') or not plan.get('expected'):
        return 'fixture_identity_missing'
    row = connection.execute('SELECT * FROM reset_fixtures WHERE resource_key = %s FOR UPDATE',
                             (checksum(plan['target']),)).fetchone()
    if row is None:
        return 'fixture_unregistered'
    if row['owner_assignment_id'] is not None:
        return 'fixture_busy'
    if row['disposition'] != 'restored':
        return 'fixture_quarantine'
    # Revalidate the stored authority observation; corruption never restores availability.
    proof = row['verification']
    if verify_readback(plan['target'], row['expected'], proof.get('readback')) != proof or proof.get('status') != 'passed':
        return 'fixture_readback_invalid'
    if not same_json_value(row['expected'], plan['expected']):
        return 'fixture_baseline_mismatch'
    return None


def claim(connection, assignment, attempt_no):
    plan = fixture_plan(assignment)
    if plan is not None:
        row = connection.execute('''UPDATE reset_fixtures SET owner_assignment_id=%s, owner_attempt_no=%s,
            disposition='unknown', updated_at=now() WHERE resource_key=%s AND owner_assignment_id IS NULL
            AND disposition='restored' RETURNING resource_key''',
            (assignment['assignment_id'], attempt_no, checksum(plan['target']))).fetchone()
        if row is None:
            raise ValueError('fixture admission lost')


def complete(connection, assignment_id, attempt_no, fencing, lease, result):
    assignment = connection.execute('SELECT * FROM assignments WHERE assignment_id=%s',
                                    (assignment_id,)).fetchone()
    receipt = result.get('reset_receipt')
    # Legacy results and R0 do not acquire X0 claims; explicit X0 must never downgrade.
    if receipt is None:
        if assignment['reset_policy']:
            raise ValueError('reset receipt missing for declared policy')
        return
    host = connection.execute('SELECT host_id FROM assignment_attempts WHERE assignment_id=%s AND attempt_no=%s',
                              (assignment_id, attempt_no)).fetchone()['host_id']
    binding = dict(assignment_id=assignment_id, attempt_no=attempt_no, fencing=fencing,
                   host_id=host, rig_id=lease['rig_id'], rig_generation=lease['rig_generation'])
    product = result.get('run_result') or {}
    world = product.get('world_identity')
    if world is not None:
        from autophone.contracts import validate_or_raise
        validate_or_raise('run-world-identity.v0.json', world)
        world_binding = dict(assignment_id=assignment_id, attempt_no=attempt_no, fencing=fencing,
                             rig_id=lease['rig_id'], generation=lease['rig_generation'])
        if (world['run_id'] != product.get('run_id') or world['assignment_binding'] != world_binding
                or world['record_sha256'] != checksum({k: v for k, v in world.items() if k != 'record_sha256'})):
            raise ValueError('Run world identity does not match current Assignment')
    policy = effective_policy(assignment['reset_policy'], SimpleNamespace(**binding))
    validate_reset_receipt(receipt, binding=binding, policy=policy)
    if result['rig_disposition'] != receipt['device_axis']['disposition']:
        # A cleanup failure can tighten, never relax, the reset result.
        if result['rig_disposition'] != 'quarantine':
            raise ValueError('Rig disposition disagrees with ResetReceipt')
    if result['fixture_disposition'] != receipt['external_fixture_axis']['disposition']:
        raise ValueError('Fixture disposition disagrees with ResetReceipt')
    plan = fixture_plan(assignment)
    if plan is not None:
        row = receipt['external_fixture_axis']
        updated = connection.execute('''UPDATE reset_fixtures SET disposition=%s, verification=%s,
            receipt=%s, owner_assignment_id=NULL, owner_attempt_no=NULL, updated_at=now()
            WHERE resource_key=%s AND owner_assignment_id=%s AND owner_attempt_no=%s RETURNING resource_key''',
            (row['disposition'], Jsonb(row['verification']), Jsonb(receipt), checksum(plan['target']),
             assignment_id, attempt_no)).fetchone()
        if updated is None:
            raise ValueError('Fixture reset does not own current claim')
