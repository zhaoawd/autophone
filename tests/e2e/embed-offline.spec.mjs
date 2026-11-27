import { test, expect } from '@playwright/test';

test('offline embed handles frames, input, heartbeat and teardown without API requests', async ({page}) => {
  const requests = [];
  await page.route('**/api/**', async route => {
    requests.push(route.request().url());
    await route.abort();
  });
  await page.clock.install();
  await page.goto('/embed/device-console?device=offline&mode=control&demo=1&launch=test-fixture');
  await expect(page).toHaveURL(/device=offline&mode=control&demo=1$/);
  const frame = page.locator('[data-frame]');
  await expect(frame).toHaveAttribute('src', /^data:image\/svg\+xml/);
  await frame.click({position: {x: 60, y: 90}});
  await expect(page.locator('[data-status]')).toContainText('已发送 1 个动作');
  await page.clock.fastForward(31000);
  await page.evaluate(() => window.dispatchEvent(new Event('beforeunload')));
  await page.locator('[data-stop]').click();
  await expect(page.locator('[data-status]')).toContainText('会话已结束');
  await page.goto('/embed/device-console?device=offline&mode=control');
  await expect(page.locator('[data-status]')).toContainText('缺少启动凭证');
  expect(requests).toEqual([]);
});
