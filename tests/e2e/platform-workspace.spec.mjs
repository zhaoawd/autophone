import {test, expect} from './fixtures.mjs';

test('core workspace pins case runs, installs, and recording replay to the selected Rig', async ({page}) => {
  await page.addInitScript(() => Object.defineProperty(crypto, 'randomUUID', {value: undefined}));
  const mutations = [];
  let saved = false;
  let draft = false;
  let offlineB = false;
  let submittedRun = null;
  let settleA;
  const slowA = new Promise(resolve => { settleA = resolve; });
  const app = '/api/apps/example';
  const recording = app + '/recordings/record-1';
  const frame = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6MmrEYAAAAASUVORK5CYII=', 'base64');
  await page.route('**/runs', async route => {
    const request = route.request();
    if (request.method() !== 'POST') return route.fallback();
    const body = request.postDataJSON();
    mutations.push({path: '/runs', body, token: request.headers()['x-controller-token']});
    submittedRun = {
      run_id: body.run_id,
      status: 'passed',
      app: body.app,
      device: body.device,
      type: body.type,
      report_html: 'runs/' + body.run_id + '/report.html',
    };
    return route.fulfill({json: {run_id: body.run_id, status: 'queued'}, status: 200});
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const send = (json, status = 200) => route.fulfill({json, status});
    if (path === '/api/platform/targets') return send({mode: 'control', targets: [
      {rig_id: 'A', status: 'online'}, {rig_id: 'B', status: offlineB ? 'offline' : 'online'},
    ]});
    if (path === '/api/apps') return send({apps: [{app: 'example', display_name: 'Example'}]});
    if (path === '/api/location/sessions') return send({sessions: []});
    if (path === '/api/location/routes') return send({routes: []});
    if (path === app + '/cases' && request.method() === 'GET') {
      return send({cases: [{id: 'R1', item: 'Verify Home CTA', status: 'active', execution_recipe: [{action: 'assert_present'}]}]});
    }
    if (path === '/api/runs/history' && request.method() === 'GET') {
      return send({runs: submittedRun ? [submittedRun] : []});
    }
    if (submittedRun && path === '/api/runs/' + submittedRun.run_id && request.method() === 'GET') {
      return send({
        run_id: submittedRun.run_id,
        result: {status: 'passed', app: submittedRun.app, device: submittedRun.device},
        artifacts: {report_html: 'report.html'},
      });
    }
    if (path === app + '/packages' && request.method() === 'GET') {
      const rig = url.searchParams.get('rig_id');
      if (rig === 'A') await slowA;
      return send({installed_build: {build_id: 'installed-' + rig}, packages: [
        {slot: '002', build_id: 'bld-new', label: 'Candidate', artifact_availability: 'available'},
      ]});
    }
    if (path === app + '/recordings' && request.method() === 'GET') {
      return send({recordings: saved ? [{recording_id: 'record-1', device: 'B', name: 'Example trace',
        status: 'captured', landable: !draft, draft: draft ? {status: 'draft'} : null}] : []});
    }
    if (path === recording + '/session/frame') return route.fulfill({body: frame, contentType: 'image/png',
      headers: {'x-frame-seq': '11'}});
    if (path === app + '/replay-batches/batch') return send({phase: 'completed', verdict: 'pass',
      items: [{name: 'Example trace', status: 'pass', run_id: 'run-1'}]});
    mutations.push({path, body: request.postDataJSON(), token: request.headers()['x-controller-token']});
    if (path === app + '/versions/002/builds/bld-new/install') return send({assignment_id: 'assignment-B'});
    if (path === app + '/recordings/start') return send({recording_id: 'record-1', controller_token: 'test-controller'});
    if (path === recording + '/session/finish') saved = true;
    if (path === recording + '/recorded-steps') draft = true;
    if (path === app + '/recordings/replay-batch') return send({batch_id: 'batch'});
    if (path.startsWith(recording + '/') && request.method() === 'POST') return send({ok: true});
    throw new Error('Unexpected workspace request ' + request.method() + ' ' + request.url());
  });

  await page.goto('/?rig=A');
  const pendingA = page.waitForRequest(r => r.url().includes('/packages?rig_id=A'));
  await page.getByText('用例回归、IPA 与录制', {exact: true}).click();
  await pendingA;
  await page.locator('#rig').selectOption('B');
  await expect(page.locator('#workspace-installed')).toHaveText(/installed-B/);
  settleA();
  await expect(page.locator('#workspace-cases')).toHaveText(/R1 · Verify Home CTA/);
  await page.getByRole('button', {name: '在所选 Rig 运行', exact: true}).click();
  await expect(page.locator('#workspace-run-status')).toHaveText(/B · web-.* · passed/);
  await expect(page.locator('#workspace-runs a', {hasText: '查看 HTML 报告'})).toHaveAttribute(
    'href', new RegExp('/api/runs/web-[a-f0-9]+/artifact/report\\.html$'),
  );
  await expect(page.getByRole('button', {name: '安装', exact: true})).toBeEnabled();
  await page.getByRole('button', {name: '安装', exact: true}).click();
  await expect(page.locator('#workspace-message')).toHaveText(/assignment-B/);
  await page.locator('#workspace-record').click();
  await expect(page.locator('#rig')).toBeDisabled();
  await expect(page.locator('#workspace-home')).toBeEnabled();
  await page.locator('#workspace-home').click();
  await expect(page.locator('#workspace-message')).toHaveText(/已记录动作/);
  await page.locator('#workspace-finish').click();
  await page.getByRole('textbox', {name: '操作草稿名称'}).fill('example_trace');
  await page.getByRole('button', {name: '以录制步骤保存草稿'}).click();
  await page.getByRole('button', {name: '在所选 Rig 回放'}).click();
  await expect(page.locator('#workspace-result')).toHaveText(/run-1/);
  offlineB = true;
  await page.locator('#refresh').click();
  await expect(page.locator('#status')).toHaveText(/offline/);
  await expect(page.locator('#workspace-record')).toBeDisabled();
  expect(mutations.find(row => row.path === '/runs').body).toMatchObject({
    schema_version: 'v1', type: 'regression', app: 'example', device: 'B',
    case_selector: {ids: ['R1']},
  });
  expect(mutations.find(row => row.path.endsWith('/install')).body.device).toBe('B');
  expect(mutations.find(row => row.path.endsWith('/install')).body.idempotency_key).toMatch(/^[a-f0-9]{32}$/);
  expect(mutations.find(row => row.path.endsWith('/start')).body).toEqual({device: 'B', surface: 'web'});
  expect(mutations.find(row => row.path.endsWith('/event'))).toMatchObject({
    token: 'test-controller', body: {kind: 'key', name: 'home', frame_seq: 11},
  });
  expect(mutations.find(row => row.path.endsWith('/event')).body.event_id).toMatch(/^[a-f0-9]{32}$/);
  expect(mutations.find(row => row.path.endsWith('/replay-batch')).body.device).toBe('B');
});
