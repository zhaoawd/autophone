// npm 入口由外层按退出码清理；直接 Playwright 调用沿用 KEEP_STATE 开关。
import fs from 'node:fs';
import path from 'node:path';

import { evidenceRunDir, keepStateDir, repoRoot } from './server-common.mjs';

export default async function globalTeardown(config) {
  // Wait for the npm wrapper's process exit code, including worker shutdown failures.
  if (evidenceRunDir || keepStateDir) return;
  const env = config.webServer?.env || {};
  // 只删 test-results 下本轮建的目录，任何其他路径一律不动。
  const sandbox = path.join(path.resolve(repoRoot), 'test-results') + path.sep;
  for (const dir of [env.AUTOPHONE_STATE_DIR, env.AUTOPHONE_APP_ASSET_DIR]) {
    if (!dir) continue;
    const resolved = path.resolve(dir);
    if (!resolved.startsWith(sandbox)) continue;
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}
