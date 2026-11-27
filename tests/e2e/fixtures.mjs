// 两类浏览器回归共用的夹具。
//
// 每条用例默认要求：页面不抛 JS 异常、没有未声明的失败响应、没有未声明的真实设备调用。
// 需要放行时逐条点名（`dashboard.allowHttpFailure`），不整段放行。
import { expect, test as base } from '@playwright/test';
import { startWorkerDiagnostics } from './worker-diagnostics.mjs';

// 会改变设备或读取设备状态的端点。默认类里它们必须由 demo 模式或 page.route 覆盖；
// 没覆盖就是用例在打真实设备，拦下并判失败。
const DEVICE_ENDPOINT = /^https?:\/\/[^/]+\/(?:api\/vm|api\/location|api\/access|devices)\//;

export const test = base.extend({
  workerDiagnostics: [async ({}, use, info) => {
    const write = startWorkerDiagnostics(info);
    try { await use(write); } finally { write('worker_fixture_teardown'); }
  }, { scope: 'worker', auto: true }],

  testDiagnostics: [async ({ workerDiagnostics }, use, info) => {
    workerDiagnostics('test_begin', { test_id: info.testId, retry: info.retry });
    try { await use(); } finally {
      workerDiagnostics('test_end', { test_id: info.testId, status: info.status });
    }
  }, { auto: true }],

  /** 由配置的 project.use 给定：默认类 true，真实后端类 false。 */
  blockUndeclaredDeviceCalls: [true, { option: true }],

  dashboard: [async ({ page, blockUndeclaredDeviceCalls }, use) => {
    // Keep functional checks independent of remote fonts, including catch-all page routes.
    const fontNetwork = await page.context().newCDPSession(page);
    await fontNetwork.send('Network.enable');
    await fontNetwork.send('Network.setBlockedURLs', {
      urls: ['https://fonts.googleapis.com/*', 'https://fonts.gstatic.com/*'],
    });
    const browserErrors = [];
    const allowedHttpFailures = [];
    const blockedDeviceCalls = [];

    page.on('pageerror', error => browserErrors.push(`pageerror: ${error.message}`));
    page.on('console', message => {
      // Chromium 把任意 4xx/5xx 另打一条无 URL 的 generic console error；有 URL 的
      // response 监听才是权威口径，避免预期的 fail-closed 503 被重复算成 JS 崩溃。
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
        browserErrors.push(`console: ${message.text()}`);
      }
    });
    page.on('response', response => {
      if (response.status() < 400) return;
      const failure = { status: response.status(), url: response.url() };
      if (!allowedHttpFailures.some(allow => allow(failure))) {
        browserErrors.push(`http ${failure.status}: ${failure.url}`);
      }
    });

    if (blockUndeclaredDeviceCalls) {
      // 最先注册 ⇒ 优先级最低：用例后注册的 page.route 一律先于它生效，
      // 只有谁都没声明的设备调用才会落到这里。
      await page.route(DEVICE_ENDPOINT, async route => {
        blockedDeviceCalls.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
        await route.abort('blockedbyclient');
      });
    }

    const dashboard = {
      /**
       * 声明一条预期内的失败响应。
       * @param {(failure: {status: number, url: string}) => boolean} predicate
       */
      allowHttpFailure(predicate) {
        allowedHttpFailures.push(predicate);
      },
      /** 点名放行某个路径上的某个状态码，不放行同前缀的其他端点。 */
      allowStatusOn(status, ...pathnames) {
        const wanted = new Set(pathnames);
        allowedHttpFailures.push(
          failure => failure.status === status && wanted.has(new URL(failure.url).pathname),
        );
      },
    };

    await use(dashboard);

    expect(
      blockedDeviceCalls,
      '用例打到了未声明的真实设备端点；用 demo 模式或 page.route 明确声明它要什么',
    ).toEqual([]);
    expect(browserErrors, 'dashboard emitted browser runtime errors').toEqual([]);
  }, { auto: true }],
});

export { expect };

/** 断言某个 pane 渲染出了它的浏览器契约。 */
export async function expectPane(page, pane, { expectHash = true } = {}) {
  const nav = page.locator(`[data-pane="${pane.key}"]`);
  const content = page.locator(`#content[data-pane-root="${pane.key}"]`);
  if (expectHash) await expect(page).toHaveURL(new RegExp(`#${pane.key}$`));
  await expect(nav).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#hTitle')).toHaveText(pane.title);
  await expect(content).toContainText(pane.marker);
  await expect(content.locator('.ap-loading')).toHaveCount(0);
  await expect(content.locator('.ap-state-error')).toHaveCount(0);
}

/** 按 key 取 pane 契约，key 写错时立刻失败而不是 undefined 传下去。 */
export function pane(panes, key) {
  const found = panes.find(item => item.key === key);
  if (!found) throw new Error(`pane-contracts.mjs 里没有 pane: ${key}`);
  return found;
}
