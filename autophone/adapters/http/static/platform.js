'use strict';

const byId = id => document.getElementById(id);
const rig = byId('rig');
const params = new URLSearchParams(location.search);
let revision = 0;
let screenshotUrl = '';
let requested = params.get('rig') || '';
let targets = [];
let platformMode = "";

async function request(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body;
    const error = new Error(detail.message || detail.code || (typeof detail === 'string' && detail) || 'HTTP ' + response.status);
    error.status = response.status;
    throw error;
  }
  return response;
}

function clearSelection() {
  revision++;
  byId('controls').disabled = byId('location-controls').disabled = true;
  byId('reading').textContent = byId('screen-status').textContent = '';
  byId('sessions').replaceChildren();
  byId('screen').hidden = true;
  byId('screen').removeAttribute('src');
  URL.revokeObjectURL(screenshotUrl);
  screenshotUrl = '';
}

function selectTarget() {
  clearSelection();
  const selected = targets.find(row => row.rig_id === rig.value);
  const available = selected && ['local', 'online'].includes(selected.status);
  byId('controls').disabled = byId('location-controls').disabled = !available;
  byId('status').textContent = selected ? selected.rig_id + ' · ' + selected.status :
    (requested ? '所选目标已不可用：' + requested : (targets.length ? '请选择目标 Rig' : '尚无登记的 Rig'));
  if (available) loadSessions(revision);
  document.dispatchEvent(new Event("platform-target-changed"));
}

async function loadTargets() {
  clearSelection();
  const generation = revision;
  rig.disabled = true;
  byId('status').textContent = '正在读取设备列表';
  try {
    const data = await (await request('/api/platform/targets')).json();
    if (generation !== revision) return;
    platformMode = data.mode;
    targets = data.targets;
    rig.replaceChildren(new Option('选择 Rig', ''));
    for (const row of targets) rig.add(new Option(row.rig_id + ' · ' + row.status, row.rig_id));
    if (!requested && targets.length === 1) requested = targets[0].rig_id;
    rig.value = requested;
    rig.disabled = !targets.length;
    selectTarget();
  } catch (error) {
    if (generation !== revision) return;
    platformMode = "";
    targets = [];
    rig.replaceChildren();
    byId('status').textContent = '无法读取设备列表：' + error.message;
  }
}

rig.onchange = () => {
  requested = rig.value;
  const url = new URL(location.href);
  if (requested) url.searchParams.set('rig', requested);
  else url.searchParams.delete('rig');
  history.replaceState(null, '', url);
  selectTarget();
};
byId('refresh').onclick = loadTargets;

function setBusy(busy) {
  rig.disabled = byId('refresh').disabled = busy;
  byId('controls').disabled = byId('location-controls').disabled = busy;
  document.dispatchEvent(new Event("platform-busy-changed"));
}

byId('console').onclick = async () => {
  setBusy(true);
  let opened = false;
  try { opened = await openDeviceConsole(rig.value, () => setBusy(false)); }
  finally { if (!opened) setBusy(false); }
};

byId('screenshot').onclick = async () => {
  const generation = revision;
  const output = byId('screen');
  try {
    const response = await request('/devices/' + encodeURIComponent(rig.value) + '/screen.jpg');
    if (response.status === 202) throw new Error('等待执行侧画面');
    const blob = await response.blob();
    if (generation !== revision) return;
    URL.revokeObjectURL(screenshotUrl);
    screenshotUrl = URL.createObjectURL(blob);
    output.src = screenshotUrl;
    output.hidden = false;
    byId('screen-status').textContent = rig.value;
  } catch (error) {
    if (generation !== revision) return;
    output.hidden = true;
    byId('screen-status').textContent = error.message;
  }
};

byId('current').onclick = async () => {
  const generation = revision;
  const device = rig.value;
  try {
    const data = await (await request('/api/location/devices/' + encodeURIComponent(device) + '/current')).json();
    if (generation !== revision) return;
    if (data.device !== device) throw new Error('回读设备与所选目标不一致');
    byId('reading').textContent = device + '\n' + JSON.stringify(data.reading, null, 2);
  } catch (error) {
    if (generation === revision) byId('reading').textContent = error.message;
  }
};

async function loadSessions(generation) {
  try {
    const data = await (await request('/api/location/sessions')).json();
    if (generation !== revision) return;
    const box = byId('sessions');
    box.replaceChildren();
    for (const session of data.sessions) {
      const {session_id, device, owner} = session.spec;
      if (device !== rig.value || session.state === 'stopped') continue;
      const row = document.createElement('p');
      row.append(session_id + ' · ' + session.state + ' ');
      const path = '/api/location/sessions/' + encodeURIComponent(session_id);
      function control(label, method, values) {
        const button = document.createElement('button');
        button.textContent = label;
        button.onclick = () => changeSession(path, method, {owner, ...values});
        row.append(button);
      }
      if (session.spec.source.kind === 'route') {
        const paused = session.state === 'paused';
        control(paused ? '继续' : '暂停', 'PATCH', {command: paused ? 'resume' : 'pause'});
      }
      control('停止', 'DELETE', {});
      box.append(row);
    }
  } catch (error) {
    if (generation === revision) byId('sessions').textContent = '无法读取会话：' + error.message;
  }
}

async function changeSession(path, method, body) {
  const generation = revision;
  const device = rig.value;
  setBusy(true);
  try {
    await request(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    byId('reading').textContent = device + ' · 请求已完成';
    await loadSessions(generation);
  } catch (error) {
    byId('reading').textContent = device + ' · ' + error.message;
  } finally {
    setBusy(false);
  }
}

function createSession(event) {
  event.preventDefault();
  const kind = event.target.id;
  const device = rig.value;
  const id = 'web-' + Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  const source = {kind, source_id: id, coordinate_system: kind === 'fixed' ? 'wgs84' : 'gcj02'};
  const body = {spec: {device, session_id: id, owner: 'platform-console', source,
    targets: [{kind: 'system_core_location', target_id: 'system:' + device, coordinate_system: 'wgs84'}]}};
  if (kind === 'route') {
    source.route_id = byId('routes').value;
  } else {
    const fields = event.target.elements;
    body.initial_sample = {producer_sequence: 0, emitted_wall_at: new Date().toISOString(),
      emitted_monotonic_s: performance.now() / 1000,
      coordinate: {latitude: fields.lat.valueAsNumber, longitude: fields.lon.valueAsNumber, coordinate_system: 'wgs84'}};
  }
  changeSession('/api/location/sessions', 'POST', body);
}

async function loadRoutes() {
  const routes = byId('routes');
  try {
    const data = await (await request('/api/location/routes')).json();
    for (const row of data.routes) routes.add(new Option(row.route_id, row.route_id));
  } catch (error) {
    routes.add(new Option('无法读取路线：' + error.message, ''));
  }
}

if (location.hash && !params.has('rig') && document.querySelector('a[href="/workspace"]')) {
  location.replace('/workspace' + location.search + location.hash);
} else {
  byId('fixed').onsubmit = byId('route').onsubmit = createSession;
  loadTargets();
  loadRoutes();
}
