"""模拟 B 端集成验收：真实 PostgreSQL/HTTP/WebSocket/ScreenLock，替身设备。"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest

from tests.support.device_console_simulation import Simulation, wait_for


@pytest.fixture(scope='module')
def sim():
    dsn = os.environ.get('AUTOPHONE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('AUTOPHONE_TEST_POSTGRES_DSN is required')
    with Simulation(dsn) as value:
        yield value


@pytest.fixture(autouse=True)
def release(sim):
    yield
    for driver in sim.drivers.values():
        driver.fail_cleanup = False
    for grant in sim.registry.grants.list(statuses=('creating', 'active', 'closing')):
        sim.service.revoke_session(None, grant.grant_id)
    for device, manager in sim.managers.items():
        if manager.device_status(device).get('unavailable'):
            manager.clear_unavailable(device, actor='simulation-teardown')


def create(sim, **payload):
    return httpx.post(sim.url + '/api/access/sessions', headers=sim.headers,
        json={'mode': 'control', **payload}, timeout=30)


def parallel(function, count=20):
    barrier = threading.Barrier(count)
    def run(index):
        barrier.wait(timeout=10)
        return function(index)
    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def exchange(sim, result):
    ticket = parse_qs(urlsplit(result['embed_url']).query)['launch'][0]
    response = httpx.post(sim.url + '/api/access/embed-sessions', json={'launch_token': ticket})
    assert response.status_code == 200, response.text
    doc = response.json()
    assert 'controller_token' not in doc
    return doc, {'X-Console-Access-Token': doc['access_token']}


def live_counts(sim):
    with sim.store._connection() as db:
        rows = db.execute("SELECT rig_id, count(*) AS n FROM rig_reservations WHERE status='active' GROUP BY rig_id").fetchall()
    return {r['rig_id']: r['n'] for r in rows}


@pytest.mark.parametrize("round_id", range(5))
def test_twenty_users_compete_for_three_devices(sim, round_id):
    results = parallel(lambda i: create(sim, request_id=f'pool-{round_id}-{i}', user=f'pool-{round_id}-{i}'))
    successes = [r.json() for r in results if r.status_code == 200]
    assert len(successes) == 3, [(r.status_code, r.text) for r in results]
    assert len({r['device'] for r in successes}) == 3
    assert all(r.status_code == 200 or (r.status_code == 409 and r.json()['detail']['code'] == 'device_busy') for r in results)
    assert live_counts(sim) == {d: 1 for d in sim.devices}
    assert all(sim.lock.live_status(device=d)['locked'] for d in sim.devices)


def test_same_request_concurrent_retries_never_allocate_twice(sim):
    results = parallel(lambda i: create(sim, request_id='same-request', user='same-user'))
    assert all(r.status_code in (200, 409) for r in results)
    final = create(sim, request_id='same-request', user='same-user')
    assert final.status_code == 200, final.text
    ids = {r.json()['session_id'] for r in results if r.status_code == 200}
    assert ids == {final.json()['session_id']}
    assert sum(live_counts(sim).values()) == 1
    assert len([g for g in sim.registry.grants.list() if g.request_id == 'same-request']) == 1


def test_same_user_distinct_requests_gets_only_one_device(sim):
    results = parallel(lambda i: create(sim, request_id=f'subject-{i}', user='single-user'))
    assert sum(r.status_code == 200 for r in results) == 1
    assert all(r.status_code in (200, 409) for r in results)
    assert sum(live_counts(sim).values()) == 1


def test_actions_and_frames_are_isolated_and_retry_is_idempotent(sim):
    sessions = []
    for i, device in enumerate(sim.devices):
        response = create(sim, request_id=f'actions-{i}', user=f'actions-{i}', device=device)
        assert response.status_code == 200, response.text
        doc, headers = exchange(sim, response.json())
        base = f'{sim.url}/api/devices/{device}/console/{doc["session_id"]}'
        frame = httpx.get(base + '/frame', headers=headers)
        assert frame.status_code == 200
        sessions.append((device, base, headers, int(frame.headers['x-frame-seq'])))
    before = {d: len(sim.drivers[d].calls) for d in sim.devices}
    def act(i):
        device, base, headers, seq = sessions[i]
        payload = {'event_id': f'action-{i}', 'frame_seq': seq, 'kind': 'tap', 'px': 30+i, 'py': 200}
        first = httpx.post(base+'/event', json=payload, headers=headers)
        second = httpx.post(base+'/event', json=payload, headers=headers)
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        assert sim.drivers[device].calls[before[device]:] == [('tap', 30+i, 200)]
    parallel(act, 3)
    assert httpx.get(sessions[1][1]+'/frame', headers=sessions[0][2]).status_code in (401, 403)


def test_multiple_viewers_cannot_act_or_release_controller(sim):
    control = create(sim, request_id='watch-control', user='operator', device='sim-1').json()
    results = parallel(lambda i: create(sim, request_id=f'watch-{i}', user=f'watch-{i}', mode='view', device='sim-1'), 8)
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    for result in results:
        doc, headers = exchange(sim, result.json())
        base = f'{sim.url}/api/devices/sim-1/console/{doc["session_id"]}'
        assert httpx.get(base+'/frame', headers=headers).status_code == 200
        denied = httpx.post(base+'/event', headers=headers, json={'event_id':'forbidden','kind':'key','name':'home'})
        assert denied.status_code == 403, denied.text
        assert httpx.delete(sim.url+'/api/access/sessions/'+result.json()['session_id'], headers=sim.headers).status_code == 200
    assert live_counts(sim) == {'sim-1': 1}
    assert sim.managers['sim-1'].device_status('sim-1')['active']
    assert control['device'] == 'sim-1'


def test_revoke_invalidates_credentials_and_releases_reservation(sim):
    result = create(sim, request_id='revoke', user='revoke').json()
    doc, headers = exchange(sim, result)
    assert httpx.delete(sim.url+'/api/access/sessions/'+result['session_id'], headers=sim.headers).status_code == 200
    base = f'{sim.url}/api/devices/{result["device"]}/console/{doc["session_id"]}'
    assert httpx.get(base+'/frame', headers=headers).status_code == 401
    assert live_counts(sim) == {}
    assert not sim.lock.live_status(device=result['device'])['locked']
    assert sim.drivers[result['device']].calls[-1] == ('key', 'home')


def test_basic_auth_protects_dashboard_but_not_embed_shell(sim):
    assert httpx.get(sim.url+'/').status_code == 401
    assert httpx.get(sim.url+'/embed/device-console?device=sim-1&mode=control').status_code == 200
    assert httpx.get(sim.url+'/static/embed/device-console.js').status_code == 200
    assert httpx.get(sim.url+'/static/dashboard/core.js').status_code == 401
    assert httpx.get(sim.url+'/api/devices/sim-1/console/missing/frame').status_code == 401


def test_revoked_unredeemed_launch_cannot_reactivate_a_grant(sim):
    result = create(sim, request_id='revoke-before-exchange', user='revoke-before-exchange').json()
    httpx.delete(sim.url+'/api/access/sessions/'+result['session_id'], headers=sim.headers).raise_for_status()
    launch = parse_qs(urlsplit(result['embed_url']).query)['launch'][0]
    response = httpx.post(sim.url+'/api/access/embed-sessions', json={'launch_token':launch})
    assert response.status_code in (401, 410), response.text
    assert sim.registry.grants.get(result['grant_id']).status == 'closed'
    assert live_counts(sim) == {}


def test_cleanup_failure_blocks_reallocation(sim):
    result = create(sim, request_id='cleanup-fail', user='cleanup-fail', device='sim-1').json()
    sim.drivers['sim-1'].fail_cleanup = True
    stopped = httpx.delete(sim.url+'/api/access/sessions/'+result['session_id'], headers=sim.headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()['status'] == 'closing'
    assert sim.lock.live_status(device='sim-1')['locked']
    retry = create(sim, request_id='after-cleanup-fail', user='next', device='sim-1')
    assert retry.status_code == 409
    # 实验已知清理故障由替身注入；显式解除后清理专属测试状态。
    sim.drivers['sim-1'].fail_cleanup = False
    sim.managers['sim-1'].clear_unavailable('sim-1', actor='simulation-test')
    sim.registry.grants.update(result['grant_id'], status='closed', closed_reason='simulation_recovered')


def test_expiry_rejects_actions_and_releases_device(sim):
    import time
    result = create(sim, request_id='expiry', user='expiry').json()
    doc, headers = exchange(sim, result)
    sim.registry.grants.update(result['grant_id'], expires_at=time.time()-1)
    base = f'{sim.url}/api/devices/{result["device"]}/console/{doc["session_id"]}'
    for action in ('event', 'heartbeat'):
        response = httpx.post(base+'/'+action, headers=headers, json={})
        assert response.status_code == 401, response.text
    assert live_counts(sim) == {}
    assert not sim.lock.live_status(device=result['device'])['locked']


def test_ambiguous_legacy_console_id_cannot_revoke_a_controller(sim):
    ctl = create(sim, request_id='ambiguous-control', user='ambiguous-control', device='sim-1').json()
    watch = create(sim, request_id='ambiguous-view', user='ambiguous-view', mode='view', device='sim-1').json()
    response = httpx.delete(sim.url+'/api/access/sessions/'+ctl['console_session_id'], headers=sim.headers)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'ambiguous_session_id'
    assert live_counts(sim) == {'sim-1':1}
    assert ctl['session_id'] != watch['session_id']



def test_creation_response_loss_replays_without_another_reservation(sim):
    import json
    import socket
    payload = json.dumps({'request_id':'lost-create','user':'lost-create','mode':'control'}).encode()
    request = (f'POST /api/access/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
               f'Authorization: Bearer {sim.secret}\r\nContent-Type: application/json\r\n'
               f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode()+payload
    with socket.create_connection(('127.0.0.1',sim.port), timeout=10) as sock:
        sock.sendall(request)
        # 不读取响应，独立回读数据库确认服务器已创建，然后断开客户端。
        wait_for(lambda: (g:=sim.registry.grants.find(request_id='lost-create')) is not None and g.status=='active')
    replay = create(sim, request_id='lost-create',user='lost-create')
    assert replay.status_code == 200, replay.text
    assert sum(live_counts(sim).values()) == 1
    assert len([g for g in sim.registry.grants.list() if g.request_id=='lost-create']) == 1


def test_revoke_waits_for_inflight_action_and_rejects_new_actions(sim):
    result = create(sim,request_id='inflight',user='inflight',device='sim-1').json()
    doc, headers = exchange(sim,result)
    base = f'{sim.url}/api/devices/sim-1/console/{doc["session_id"]}'
    seq = int(httpx.get(base+'/frame',headers=headers).headers['x-frame-seq'])
    driver = sim.drivers['sim-1']
    entered, resume = threading.Event(), threading.Event()
    driver.action_started, driver.action_release = entered, resume
    before = len(driver.calls)
    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(httpx.post,base+'/event',headers=headers,
            json={'event_id':'inflight-one','kind':'tap','px':42,'py':100,'frame_seq':seq},timeout=15)
        assert entered.wait(timeout=5)
        revoke = pool.submit(httpx.delete,sim.url+'/api/access/sessions/'+result['session_id'],headers=sim.headers,timeout=15)
        try:
            wait_for(lambda: sim.registry.grants.get(result['grant_id']).status=='closing')
            assert sim.lock.live_status(device='sim-1')['locked']
            assert not revoke.done()
            assert httpx.post(base+'/event',headers=headers,json={}).status_code == 401
        finally:
            resume.set()
        assert action.result().status_code == revoke.result().status_code == 200
    driver.action_started = driver.action_release = None
    assert driver.calls[before:] == [('tap',42,100),('key','home')]
    assert not sim.lock.live_status(device='sim-1')['locked']
    assert live_counts(sim) == {}


def test_rig_channel_disconnect_releases_lease_and_reservation(sim):
    result = create(sim,request_id='disconnect',user='disconnect',device='sim-1').json()
    agent = sim.agents[0]
    agent.stop()
    try:
        wait_for(lambda: not sim.lock.live_status(device='sim-1')['locked'])
        assert live_counts(sim) == {}
    finally:
        agent.start()
        wait_for(agent.connected.is_set)
    assert sim.registry.grants.get(result['grant_id']).status == 'active'
    # 分配新会话时对账关闭已经没有 lease 的旧 active 授权。
    fresh=create(sim,request_id='after-disconnect',user='after-disconnect',device='sim-1')
    assert fresh.status_code == 200, fresh.text
    assert sim.registry.grants.get(result['grant_id']).status == 'closed'


def test_admin_kick_releases_device_and_invalidates_access(sim):
    created = create(sim, request_id="admin-kick", user="kick-user", device="sim-1").json()
    token, headers = exchange(sim, created)
    path = sim.url + "/api/access/sessions/" + created["session_id"] + "/kick"
    assert httpx.post(path, headers=sim.headers).status_code == 401
    response = httpx.post(path, auth=sim.admin_auth)
    assert response.status_code == 200, response.text
    assert response.json()["closed_reason"] == "kicked"
    assert not sim.lock.live_status(device="sim-1")["locked"]
    assert live_counts(sim) == {}
    frame = sim.url + "/devices/sim-1/console/" + token["session_id"] + "/frame"
    assert httpx.get(frame, headers=headers).status_code in (401, 403, 410)


def test_reconcile_repairs_both_reservation_mismatches(sim):
    from autophone.application.device_access import reconcile_console_reservations

    def reconcile(apply):
        return reconcile_console_reservations(
            control=sim.control, grants=sim.registry.grants,
            call=lambda device, op, payload: sim.managers[device].device_status(device),
            devices=sim.devices, apply=apply)
    # 有预留、无设备会话。
    sim.control.create_reservation({"rig_id": "sim-1",
        "owner": {"kind": "device_console", "device": "sim-1", "session_id": "missing"}})
    assert len(reconcile(False)["released"]) == 1
    assert live_counts(sim) == {"sim-1": 1}
    assert len(reconcile(True)["released"]) == 1
    assert live_counts(sim) == {}
    # 有设备会话、无预留。
    create(sim, request_id="reconcile-live", user="reconcile-user", device="sim-1").raise_for_status()
    reservation = sim.control.list_reservations(statuses=["active"])[0]
    sim.control.release_reservation(reservation["reservation_id"], "simulation-fault")
    assert len(reconcile(False)["registered"]) == 1
    assert live_counts(sim) == {}
    assert len(reconcile(True)["registered"]) == 1
    assert live_counts(sim) == {"sim-1": 1}
    assert reconcile(True)["registered"] == []
    # 新补登记的预留由后续对账回收。
    for grant in sim.registry.grants.list(statuses=("active",)):
        sim.service.revoke_session(None, grant.grant_id)
    reconcile(True)
    assert live_counts(sim) == {}


def test_selected_rig_channel_disconnect_refuses_dispatch(sim):
    first, second = sim.devices[:2]
    for device in (first, second):
        sim.store.begin_agent_connection('sim-host', device, 1, 'sim-' + device)
    agent = sim.agents[1]
    with httpx.Client(base_url=sim.url, auth=sim.admin_auth, trust_env=False) as client:
        def inventory():
            result = client.get('/api/platform/targets')
            result.raise_for_status()
            return {row['rig_id']: row['status'] for row in result.json()['targets']}

        assert inventory()[second] == 'online'
        screen = client.get(f'/devices/{first}/screen.jpg')
        screen.raise_for_status()
        other = client.get(f'/devices/{second}/screen.jpg')
        other.raise_for_status()
        assert screen.content != other.content
        initial = list(sim.drivers[first].calls)
        agent.stop()
        try:
            wait_for(lambda: inventory()[second] == 'offline')
            assert any(row['rig_id'] == second and row['status'] == 'online'
                       for row in sim.control.list_rigs())
            for path in (f'/devices/{second}/screen.jpg', f'/api/location/devices/{second}/current'):
                assert client.get(path).status_code == 503, path
            result = client.post(f'/api/devices/{second}/console', json={'mode': 'control'})
            assert result.status_code == 503, result.text
            assert inventory()[first] == 'online'
            assert client.get(f'/devices/{first}/screen.jpg').content == screen.content
            assert sim.drivers[first].calls == initial
        finally:
            agent.start()
            wait_for(agent.connected.is_set)
