// 第一类浏览器回归：浏览器 + 模拟 API。默认入口，`npm run test:dashboard-e2e`。
//
// 收集口径是约定而不是清单：`tests/e2e` 下所有 `*.spec.mjs` 都进这一类，
// 只排除第二类的 `*.backend.spec.mjs`（见 playwright.backend.config.mjs）。
// 新增一个 spec 文件不需要改这里。
//
// 需要自带模拟服务的 spec（例如 device-console.spec.mjs，打的是
// tests/support/device_console_simulation.py 起的模拟服务而不是看板）不属于这两类。
import { defineConfig } from '@playwright/test';

import { browserChannel, dashboardWebServer, evidencePaths, evidenceRunDir, stateDirFor } from './server-common.mjs';

const port = process.env.AUTOPHONE_E2E_PORT || '8765';
const { baseURL, webServer } = dashboardWebServer({
  port,
  stateDir: stateDirFor('dashboard-e2e'),
});

const evidence = evidencePaths('dashboard-e2e');

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.mjs',
  testIgnore: ['**/*.backend.spec.mjs', '**/device-console.spec.mjs'],
  globalSetup: './global-setup.mjs',
  globalTeardown: './global-teardown.mjs',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  reporter: [['line'], ['json', { outputFile: evidence.report }]],
  outputDir: evidence.output,
  projects: [
    {
      // 项目名进 reporter 输出，两类结果在报告里可分辨。
      name: 'dashboard-mock',
      use: {
        baseURL,
        browserName: 'chromium',
        channel: browserChannel,
        headless: true,
        screenshot: 'only-on-failure',
        // A worker can fail after every test passed; defer trace retention to the run.
        trace: evidenceRunDir ? 'on' : 'retain-on-failure',
        // 这一类不碰真实设备端点：未被 page.route 或 demo 覆盖的
        // /api/vm/*、/api/location/*、/api/access/*、/devices/* 一律拦下并判失败。
        blockUndeclaredDeviceCalls: true,
      },
    },
  ],
  webServer,
});
