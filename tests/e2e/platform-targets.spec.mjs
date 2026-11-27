import {expect, test} from './fixtures.mjs';

test('target switch discards stale readings and keeps actions and failures on the selected Rig', async ({page, dashboard}) => {
  let offline = false;
  const writes = [];
  let releaseA;
  let receivedA;
  const pendingA = new Promise(resolve => { releaseA = resolve; });
  const startedA = new Promise(resolve => { receivedA = resolve; });
  await page.route('**/api/platform/targets', route => route.fulfill({json: {mode: 'control', targets: [
    {rig_id: 'rig-a', status: 'online'}, {rig_id: 'rig-b', status: offline ? 'offline' : 'online'},
  ]}}));
  await page.route('**/api/location/routes', route => route.fulfill({json: {routes: [{route_id: 'demo-route'}]}}));
  await page.route('**/api/location/sessions', route => {
    if (route.request().method() === 'POST') {
      writes.push(route.request().postDataJSON());
      if (offline) return route.fulfill({status: 503, json: {detail: {message: 'Rig disconnected'}}});
    }
    return route.fulfill({json: {sessions: [{state: 'running', spec: {
      device: 'rig-a', session_id: 'session-a', owner: 'test', source: {kind: 'fixed'},
    }}]}});
  });
  await page.route('**/api/location/devices/rig-a/current', async route => {
    receivedA();
    await pendingA;
    await route.fulfill({json: {device: 'rig-a', reading: {latitude: 10}}});
  });
  await page.route('**/api/location/devices/rig-b/current', route => route.fulfill({
    json: {device: 'rig-b', reading: {latitude: 20}},
  }));
  const launches = [];
  await page.route('**/api/access/launch-tokens', route => {
    launches.push(route.request().postDataJSON());
    return route.fulfill({json: {launch_token: 'test-ticket'}});
  });
  await page.route('**/embed/device-console?**', route => route.fulfill({
    contentType: 'text/html', body: '<html><title>Test console</title></html>',
  }));
  await page.route('**/devices/rig-b/screen.jpg', route => route.fulfill({
    contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=', 'base64'),
  }));
  await page.goto('/?rig=rig-a&app=food_ordering');
  await page.locator('#rig').selectOption('rig-a');
  await expect(page.locator('#sessions')).toContainText('session-a');
  await page.locator('#current').click();
  await startedA;
  await page.locator('#rig').selectOption('rig-b');
  await expect(page.locator('#sessions')).toBeEmpty();
  await page.locator('#current').click();
  await expect(page.locator('#reading')).toContainText('rig-b');
  const completedA = page.waitForResponse('**/api/location/devices/rig-a/current');
  releaseA();
  await completedA;
  await expect(page.locator('#reading')).toContainText('"latitude": 20');
  await page.getByRole('button', {name: '开始回放'}).click();
  await expect(page.locator('#reading')).toContainText('rig-b · 请求已完成');
  expect(writes[0].spec.device).toBe('rig-b');
  expect(writes[0].spec.targets[0].target_id).toBe('system:rig-b');

  await page.locator('#screenshot').click();
  await expect(page.locator('#screen-status')).toHaveText('rig-b');
  await page.locator('#console').click();
  await expect(page.locator('[data-devcon-frame]')).toHaveAttribute('src', /device=rig-b/);
  await expect(page.locator('#rig')).toBeDisabled();
  expect(launches).toEqual([{device: 'rig-b', mode: 'control'}]);
  await page.getByRole('button', {name: '关闭并归还设备'}).click();
  await expect(page.locator('#rig')).toBeEnabled();

  offline = true;
  dashboard.allowStatusOn(503, '/api/location/sessions');
  await page.getByRole('button', {name: '开始回放'}).click();
  await expect(page.locator('#reading')).toContainText('Rig disconnected');
  await page.locator('#refresh').click();
  await expect(page.locator('#status')).toHaveText('rig-b · offline');
  await expect(page.locator('#console')).toBeDisabled();
  await expect(page.locator('#current')).toBeDisabled();
  await expect(page.locator('#screen')).toBeHidden();
  expect(writes.map(call => call.spec.device)).toEqual(['rig-b', 'rig-b']);
  await page.locator('#rig').selectOption('rig-a');
  await expect(page.locator('#console')).toBeEnabled();
});

test('configured control plane failure keeps device actions disabled', async ({page, dashboard}) => {
  dashboard.allowStatusOn(503, '/api/platform/targets');
  await page.route('**/api/platform/targets', route => route.fulfill({
    status: 503, json: {detail: {code: 'control_plane_unavailable'}},
  }));
  await page.route('**/api/location/routes', route => route.fulfill({json: {routes: []}}));
  await page.goto('/?rig=rig-b');
  await expect(page.locator('#status')).toContainText('control_plane_unavailable');
  await expect(page.locator('#rig')).toBeDisabled();
  await expect(page.locator('#console')).toBeDisabled();
  await expect(page.locator('#current')).toBeDisabled();
});
