'use strict';

(() => {
  const get = id => document.getElementById('workspace-' + id);
  const panel = document.getElementById('workspace');
  const app = get('app');
  const frame = get('frame');
  const appBase = name => '/api/apps/' + encodeURIComponent(name);
  const recordingPath = (base, id) => base + '/recordings/' + encodeURIComponent(id);
  const jsonBody = (data, method = 'POST', headers = {}) => ({
    method, headers: {'Content-Type': 'application/json', ...headers}, body: JSON.stringify(data),
  });
  const json = async (path, options) => (await request(path, options)).json();
  const requestId = () => crypto.randomUUID?.() ?? Array.from(
    crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0'),
  ).join('');
  let version = 0;
  let session = null;
  let busy = false;
  let replayTimer;
  let replayId;
  let runTimer;
  let activeRunId;
  let messageDevice = '';
  const TERMINAL_RUN_STATUSES = new Set(['passed', 'failed', 'error', 'done', 'blocked', 'aborted']);

  function show(message, device = rig.value) {
    messageDevice = device;
    get('message').textContent = message ? device + ' · ' + message : '';
  }

  function available() {
    return panel.open && !rig.disabled && !byId('controls').disabled && Boolean(rig.value);
  }

  function updateControls() {
    app.disabled = !available() || busy || Boolean(session);
    get('record').disabled = app.disabled || !app.value;
    for (const button of panel.querySelectorAll('#workspace-upload button, ul button')) {
      button.disabled = app.disabled || !app.value;
    }
    if (session) {
      get('home').disabled = session.busy || !session.seq;
      get('clipboard').querySelector('button').disabled = session.busy || !session.seq;
      get('finish').disabled = get('cancel').disabled = session.busy;
    }
  }

  function clearLists() {
    get('installed').textContent = '';
    get('run-status').textContent = '';
    for (const id of ['cases', 'runs', 'builds', 'recordings', 'result']) get(id).replaceChildren();
    clearTimeout(replayTimer);
    replayId = null;
    clearTimeout(runTimer);
    activeRunId = null;
  }

  function addLink(row, label, href) {
    const link = document.createElement('a');
    link.textContent = label;
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener';
    row.append(link);
  }

  function reportArtifactHref(runId, run) {
    const raw = run?.artifacts?.report_html || run?.artifacts?.operation_report
      || run?.artifacts?.verification_report || run?.report_html;
    if (!raw || !runId) return null;
    const filename = String(raw).split('/').filter(Boolean).pop();
    if (!filename) return null;
    return '/api/runs/' + encodeURIComponent(runId) + '/artifact/' + encodeURIComponent(filename);
  }

  function renderRuns(runs) {
    get('runs').replaceChildren();
    if (!runs?.length) {
      addRow('runs', '暂无运行记录');
      return;
    }
    for (const run of runs) {
      const id = run.run_id || '';
      const summary = [id, run.status, run.device, run.type].filter(Boolean).join(' · ');
      const row = addRow('runs', summary);
      const reportHref = reportArtifactHref(id, run);
      if (reportHref) addLink(row, '查看 HTML 报告', reportHref);
      if (id) addLink(row, 'JSON 详情', '/api/runs/' + encodeURIComponent(id));
    }
  }

  function addButton(row, label, action) {
    const button = document.createElement('button');
    button.textContent = label;
    button.onclick = action;
    row.append(button);
  }

  function addRow(list, message) {
    const row = document.createElement('li');
    row.textContent = message;
    get(list).append(row);
    return row;
  }

  async function loadProfiles() {
    const generation = version;
    if (!available()) return;
    try {
      const {apps} = await json('/api/apps');
      if (generation !== version) return;
      const selected = app.value;
      app.replaceChildren(new Option('选择 App', ''));
      for (const profile of apps || []) {
        app.add(new Option(profile.display_name || profile.app, profile.app));
      }
      app.value = Array.from(app.options).some(option => option.value === selected) ? selected : '';
      if (!app.value && app.options.length === 2) app.selectedIndex = 1;
      get('target').textContent = apps?.length ? rig.value : rig.value + ' · 请在 App 资产目录运行 autophone app add 创建 App Profile';
      await loadDetails(generation);
    } catch (error) {
      if (generation === version) show(error.message);
    } finally {
      updateControls();
    }
  }

  async function loadDetails(generation) {
    if (!available() || !app.value) return;
    const device = rig.value;
    const appName = app.value;
    const base = appBase(appName);
    const query = encodeURIComponent(device);
    const [packages, recordings, casesResp, runsResp] = await Promise.allSettled([
      json(base + '/packages' + (platformMode === 'local' ? '' : '?rig_id=' + query)),
      json(base + '/recordings?device=' + query),
      json(base + '/cases'),
      json('/api/runs/history?app=' + encodeURIComponent(appName) + '&limit=10'),
    ]);
    if (generation !== version) return;
    get('cases').replaceChildren();
    get('runs').replaceChildren();
    get('builds').replaceChildren();
    get('recordings').replaceChildren();
    if (casesResp.status === 'fulfilled') {
      const rows = casesResp.value.cases || [];
      for (const row of rows) {
        const label = [row.id, row.item || row.test_point, row.status].filter(Boolean).join(' · ');
        const item = addRow('cases', label);
        const hasRecipe = Boolean(row.execution_recipe) || row.execution?.has_recipe !== false;
        if (row.id && hasRecipe) {
          addButton(item, '在所选 Rig 运行', () => startCaseRun(appName, device, row));
        }
      }
      if (!rows.length) addRow('cases', '暂无已存用例');
    } else {
      addRow('cases', casesResp.reason.message);
    }
    if (runsResp.status === 'fulfilled') {
      renderRuns(runsResp.value.runs || []);
    } else {
      addRow('runs', runsResp.reason.message);
    }
    if (packages.status === 'fulfilled') {
      const {installed_build, packages: builds} = packages.value;
      get('installed').textContent = '所选 Rig 已安装构建 · ' + (installed_build?.build_id || '无回执');
      for (const build of builds || []) {
        const row = addRow('builds', [build.label, build.short_version, build.build_id].filter(Boolean).join(' · '));
        if (build.build_id && build.artifact_availability !== 'pruned') {
          addButton(row, '安装', () => mutate(async () => {
            const path = base + '/versions/' + encodeURIComponent(build.slot)
              + '/builds/' + encodeURIComponent(build.build_id) + '/install';
            const answer = await json(path, jsonBody({device, idempotency_key: requestId()}));
            return answer.assignment_id ? '已提交构建 ' + build.build_id + ' · ' + answer.assignment_id
              : '已执行安装 · ' + build.build_id;
          }));
        }
      }
      if (!builds?.length) addRow('builds', '暂无已入库构建');
    } else {
      get('installed').textContent = '';
      addRow('builds', packages.reason.message);
    }
    if (recordings.status === 'fulfilled') {
      for (const row of recordings.value.recordings || []) {
        const item = addRow('recordings', [row.display_name || row.name || row.recording_id, row.device, row.status].filter(Boolean).join(' · '));
        if (['draft', 'active'].includes(row.draft?.status)) {
          addButton(item, '在所选 Rig 回放', () => startReplay(base, device, row));
        } else if (row.landable) {
          const name = document.createElement('input');
          name.setAttribute('aria-label', '操作草稿名称');
          item.append(' ', name);
          addButton(item, '以录制步骤保存草稿', () => mutate(async () => {
            if (!name.value.trim()) throw new Error('请输入操作草稿名称');
            await json(recordingPath(base, row.recording_id) + '/recorded-steps', jsonBody({name: name.value}));
            return '录制步骤已保存为草稿';
          }));
        }
      }
      if (!recordings.value.recordings?.length) addRow('recordings', '暂无录制');
    } else {
      addRow('recordings', recordings.reason.message);
    }
    updateControls();
  }

  async function mutate(action) {
    if (!available() || !app.value || busy || session) return;
    const generation = version;
    const device = rig.value;
    busy = true;
    updateControls();
    try {
      const message = await action();
      if (generation !== version) return;
      show(message, device);
      await loadDetails(generation);
    } catch (error) {
      if (generation === version) show(error.message, device);
    } finally {
      busy = false;
      updateControls();
    }
  }

  get('upload').onsubmit = event => {
    event.preventDefault();
    const base = appBase(app.value);
    const ipa = event.target.elements.ipa.files[0];
    if (!ipa) return;
    mutate(async () => {
      const staged = await json(base + '/packages/upload?filename=' + encodeURIComponent(ipa.name),
        {method: 'POST', body: ipa});
      await json(base + '/packages', jsonBody({ipa_path: staged.ipa_path}));
      event.target.reset();
      return 'IPA 已入库';
    });
  };

  function scheduleFrame(s, ms) {
    s.timer = setTimeout(() => pollFrame(s), ms);
  }

  function tokenHeader(s) {
    return {'X-Controller-Token': s.token};
  }

  async function pollFrame(s) {
    if (s !== session) return;
    try {
      const response = await request(s.path + '/frame');
      const data = await response.blob();
      if (s !== session) return;
      const next = URL.createObjectURL(data);
      frame.src = next;
      URL.revokeObjectURL(s.url);
      s.url = next;
      s.seq = Number(response.headers.get('x-frame-seq')) || 0;
    } catch (error) {
      if (s !== session) return;
      s.seq = 0;
      frame.removeAttribute('src');
      show(error.message, s.device);
    }
    updateControls();
    scheduleFrame(s, 500);
  }

  function release(s) {
    clearTimeout(s.timer);
    clearInterval(s.heartbeat);
    URL.revokeObjectURL(s.url);
    frame.removeAttribute('src');
    get('live').hidden = true;
    session = null;
    setBusy(false);
    updateControls();
  }

  get('record').onclick = async () => {
    if (!available() || !app.value || busy || session) return;
    const device = rig.value;
    const base = appBase(app.value);
    setBusy(true);
    try {
      const result = await json(base + '/recordings/start', jsonBody({device, surface: 'web'}));
      if (!result.recording_id || !result.controller_token) throw new Error('服务端未返回录制会话');
      const s = {device, path: recordingPath(base, result.recording_id) + '/session',
        token: result.controller_token, seq: 0, busy: false};
      session = s;
      get('live').hidden = false;
      s.heartbeat = setInterval(async () => {
        try { await request(s.path + '/heartbeat', {method: 'POST', headers: tokenHeader(s)}); }
        catch (error) { if (s === session) show(error.message, s.device); }
      }, 5000);
      show('录制中', device);
      updateControls();
      pollFrame(s);
    } catch (error) {
      setBusy(false);
      show(error.message, device);
    }
  };

  async function sendRecording(intent) {
    const s = session;
    if (!s?.seq || s.busy || !frame.complete) return;
    s.busy = true;
    updateControls();
    try {
      await json(s.path + '/event', jsonBody({
        ...intent, event_id: requestId(), frame_seq: s.seq,
      }, 'POST', tokenHeader(s)));
      if (s === session) show('已记录动作', s.device);
    } catch (error) {
      if (s === session) show(error.message, s.device);
    } finally {
      s.busy = false;
      updateControls();
    }
  }

  frame.onclick = event => {
    if (!frame.naturalWidth || !frame.naturalHeight) return;
    const box = frame.getBoundingClientRect();
    const px = Math.floor((event.clientX - box.x) / box.width * frame.naturalWidth);
    const py = Math.floor((event.clientY - box.y) / box.height * frame.naturalHeight);
    sendRecording({kind: 'tap', px, py});
  };
  get('home').onclick = () => sendRecording({kind: 'key', name: 'home'});
  get('clipboard').onsubmit = event => {
    event.preventDefault();
    sendRecording({kind: 'type', text: event.target.elements.text.value});
  };

  async function finish(action) {
    const s = session;
    if (!s || s.busy) return;
    s.busy = true;
    updateControls();
    try {
      await request(s.path + '/' + action, {method: 'POST', headers: tokenHeader(s)});
      release(s);
      show(action === 'finish' ? '录制已保存' : '录制已取消', s.device);
      await loadDetails(version);
    } catch (error) {
      show(error.message, s.device);
      if (error.status === 404 || error.status === 503) release(s);
    } finally {
      s.busy = false;
      updateControls();
    }
  }

  get('finish').onclick = () => finish('finish');
  get('cancel').onclick = () => finish('cancel');

  function startCaseRun(appName, device, caseRow) {
    const generation = version;
    const runId = 'web-' + requestId().slice(0, 12);
    const selector = {ids: [caseRow.id]};
    if (caseRow.status && caseRow.status !== 'active') selector.statuses = [caseRow.status];
    const spec = {
      schema_version: 'v1',
      run_id: runId,
      type: 'regression',
      app: appName,
      device,
      requested_by: 'web',
      case_selector: selector,
    };
    return mutate(async () => {
      const submitted = await json('/runs', jsonBody(spec));
      clearTimeout(runTimer);
      if (generation !== version) return '';
      const resolvedId = submitted.run_id || runId;
      activeRunId = resolvedId;
      get('run-status').textContent = device + ' · ' + resolvedId + ' · ' + (submitted.status || 'queued');
      pollRun(resolvedId, device, generation);
      return '已提交用例运行 · ' + resolvedId;
    });
  }

  async function pollRun(id, device, generation) {
    try {
      const detail = await json('/api/runs/' + encodeURIComponent(id));
      if (generation !== version || activeRunId !== id) return;
      const status = detail.result?.status || detail.active?.status || 'running';
      const statusLine = get('run-status');
      statusLine.replaceChildren(device + ' · ' + id + ' · ' + status);
      const reportHref = reportArtifactHref(id, detail);
      if (reportHref) {
        statusLine.append(' ');
        addLink(statusLine, '查看 HTML 报告', reportHref);
      }
      if (TERMINAL_RUN_STATUSES.has(status)) {
        const runsResp = await json('/api/runs/history?app=' + encodeURIComponent(app.value) + '&limit=10');
        if (generation === version) renderRuns(runsResp.runs || []);
      } else {
        runTimer = setTimeout(() => pollRun(id, device, generation), 900);
      }
    } catch (error) {
      if (generation === version && activeRunId === id) show(error.message, device);
    }
  }

  function startReplay(base, device, row) {
    const generation = version;
    return mutate(async () => {
      const batch = await json(base + '/recordings/replay-batch',
        jsonBody({device, recording_ids: [row.recording_id]}));
      clearTimeout(replayTimer);
      if (generation !== version) return '';
      replayId = batch.batch_id;
      pollReplay(base, replayId, generation);
      return '已提交回放 · ' + batch.batch_id;
    });
  }

  async function pollReplay(base, id, generation) {
    try {
      const batch = await json(base + '/replay-batches/' + encodeURIComponent(id));
      if (generation !== version || replayId !== id) return;
      get('result').replaceChildren();
      addRow('result', batch.phase + (batch.verdict ? ' · ' + batch.verdict : ''));
      for (const item of batch.items || []) {
        addRow('result', [item.name, item.status, item.run_id, item.note].filter(Boolean).join(' · '));
      }
      if (['queued', 'running'].includes(batch.phase)) {
        replayTimer = setTimeout(() => pollReplay(base, id, generation), 900);
      }
    } catch (error) {
      if (generation === version && replayId === id) show(error.message);
    }
  }

  panel.addEventListener('toggle', () => {
    if (session) { panel.open = true; return; }
    version++;
    clearLists();
    if (panel.open) loadProfiles();
    updateControls();
  });
  app.addEventListener('change', async () => {
    version++;
    clearLists();
    await loadDetails(version);
  });
  document.addEventListener('platform-target-changed', () => {
    version++;
    clearLists();
    if (messageDevice !== rig.value) show('');
    get('target').textContent = rig.value || '请选择目标 Rig';
    if (panel.open) loadProfiles();
    updateControls();
  });
  document.addEventListener('platform-busy-changed', updateControls);
})();
