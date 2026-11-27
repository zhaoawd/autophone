// 两类浏览器回归共用的服务装配。
//
// 两类的区别只在「测试面对的是什么」：
//   - 浏览器 + 模拟 API（`*.spec.mjs`，默认入口）：验证交互、渲染、请求参数、
//     异常恢复与异步时序，不依赖后端数据；
//   - 浏览器 + 真实后端 + 隔离数据（`*.backend.spec.mjs`）：验证保存、刷新后恢复、
//     权限、契约与跨页面流程。
// 两类都起自己的 dashboard 进程，都指向本轮新建的临时状态目录，互不共用端口与数据。
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const repoRoot = fileURLToPath(new URL('../..', import.meta.url));
export const evidenceRunDir = process.env.AUTOPHONE_E2E_RUN_DIR || null;

export function evidencePaths(name) {
  return {
    report: evidenceRunDir ? path.join(evidenceRunDir, 'report.json') : `../../test-results/${name}-report.json`,
    output: evidenceRunDir ? path.join(evidenceRunDir, 'artifacts') : `../../test-results/${name}`,
  };
}

const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);

function flag(name) {
  return TRUE_VALUES.has(String(process.env[name] || '').trim().toLowerCase());
}

/** 是否允许复用一台已经起着的 dashboard。默认不允许（本地与 CI 一致）。 */
export const reuseExistingServer = flag('AUTOPHONE_E2E_REUSE_SERVER');

/** 复用时是否接受「工作区有未提交改动」。默认不接受，见 global-setup.mjs。 */
export const reuseAcceptsDirtyTree = flag('AUTOPHONE_E2E_REUSE_DIRTY');

/** 跑完是否保留隔离状态目录（排障用）。 */
export const keepStateDir = flag('AUTOPHONE_E2E_KEEP_STATE');

/**
 * 本轮的隔离状态目录。
 *
 * `AUTOPHONE_STATE_DIR` 决定 `autophone.paths.STATE_ROOT`，runs/、registries、
 * 锁根 `runs/.locks`、config 覆写全部落在它下面。不设置时它回落到仓库根，
 * 测试就会往主 checkout 里写运行期状态——那正是这里要避免的。
 *
 * 目录不放在 `outputDir` 下：Playwright 每轮先删 outputDir，再起 webServer。
 */
export function stateDirFor(name) {
  if (evidenceRunDir) return path.join(evidenceRunDir, 'state');
  return path.join(repoRoot, 'test-results', `${name}-state`, `run-${process.pid}`);
}

/**
 * 本轮的隔离 App 资产根（真实后端类用）。
 *
 * `AUTOPHONE_APP_ASSET_DIR` 决定 `autophone.paths.APP_ASSET_ROOT`：App Profile、用例库、
 * Recipe、操作库、知识与 Fixture 声明都按 `<它>/apps/<app>/` 解析。webServer 命令先用
 * `prepare-app-assets.mjs` 把仓库 `apps/` 复制到这里，跑完由 global-teardown 删除。
 */
export function appAssetDirFor(name) {
  if (evidenceRunDir) return path.join(evidenceRunDir, 'assets');
  return path.join(repoRoot, 'test-results', `${name}-assets`, `run-${process.pid}`);
}

/**
 * 测试服务的 vphone-cli 目录（BL-231）：一个不存在的短路径。默认 socket 为
 * `<它>/vm/vphone.sock`，须短于 AF_UNIX 约 104 字节的上限，连接失败方式才与 CI 相同
 * （ENOENT），放在 test-results 下会超长。
 */
export const ABSENT_VPHONE_CLI_DIR = '/tmp/autophone-e2e-absent-vphone-cli';

/** 测试服务里 AI CLI 的占位名：不存在的可执行文件，AI job 在调用前即失败。 */
export const NO_AI_CODEX_BIN = 'autophone-e2e-no-ai';

/**
 * AI CLI 占位目录（BL-229）：含 `claude`、`codex` 两个立即以 126 退出的脚本，与 pytest 共用。
 * 两类 dashboard 都把它前置到 PATH，并把 `AUTOPHONE_CODEX_BIN` 指向不存在的占位名
 * （否则 codex 会优先解析到 ChatGPT.app 内置路径，绕过 PATH），测试服务因此不执行真实 AI CLI。
 */
export const AI_CLI_GUARD_DIR = path.join(repoRoot, 'tests', 'ai_cli_guard');

export function productInputRoots(stateDir) {
  return {
    FOOD_ORDERING_PROJECT_ROOT: path.join(stateDir, 'product-inputs', 'food_ordering_v2'),
    UI_EVAL_TARGET_A_PROJECT_ROOT: path.join(stateDir, 'product-inputs', 'ui_eval_target_a'),
  };
}

/**
 * 组装一台 dashboard 测试服务。
 *
 * `--no-control-plane`：控制面由 `start_vphone.sh` 引导，测试进程不接它，也不
 * 继承外部 shell 里的 `AUTOPHONE_DATABASE_URL`。
 * `AUTOPHONE_SCHEDULER_ENABLED=0`：关掉定时调度循环，避免测试期自行发起 run。
 * `AUTOPHONE_TEST_MODE=1`：进程在 `/api/runtime/identity` 上自报测试模式，
 * 复用校验按它判定。
 * `appAssetDir`：给出时先复制仓库 `apps/` 到该目录，并以 `AUTOPHONE_APP_ASSET_DIR` 指向它；
 * 同时把 AI provider 固定为 codex，后端在导入提交后自行提交的 AI job（suggest-ops）在
 * 调用前即以「codex executable not found」失败。不给时显式清空该变量，不继承外层 shell。
 * 两类都前置 `AI_CLI_GUARD_DIR` 并设 `AUTOPHONE_CODEX_BIN`：`/api/ai/doctor` 等按名字查找
 * claude 的路径只会执行占位脚本。
 * 设备目标（BL-231）：`VPHONE_CLI_DIR` 指向不存在的 `ABSENT_VPHONE_CLI_DIR`，清空
 * `VM_DIR`、`VPHONE_SOCK`、`AP_DEVICE`，Frida 端点指向不可连接的 `127.0.0.1:1`。测试服务的
 * 默认 socket 因此不存在（设备键仍为 vm），不会连到 `../vphone-cli/<vm>/vphone.sock`
 * 或本机 27042/27142 隧道，也不继承外层 shell 里的这些值。
 */
export function dashboardWebServer({ port, stateDir, appAssetDir = null, host = '127.0.0.1' }) {
  const baseURL = `http://${host}:${port}`;
  const preflight = `node tests/e2e/server-preflight.mjs --host ${host} --port ${port} --state-dir '${stateDir}'`;
  const hasDelivery = existsSync(path.join(repoRoot, 'pkgs/autophone-delivery/pyproject.toml'));
  const prepareProducts = hasDelivery ? `node tests/e2e/prepare-product-inputs.mjs --state-dir '${stateDir}'` : null;
  const prepareAssets = appAssetDir ? `node tests/e2e/prepare-app-assets.mjs --target '${appAssetDir}'` : null;
  const serve = [
    `uv run${hasDelivery ? ' --extra delivery' : ''} --group platform python3 -m autophone.cli serve`,
    `--host ${host}`,
    `--port ${port}`,
    '--no-control-plane',
  ].join(' ');
  return {
    baseURL,
    webServer: {
      // webServer 的默认 cwd 是配置文件所在目录（tests/e2e）。BL-020 拆包
      // （e7822584）后，整体安装包不再持有源码，可编辑安装也不再往 site-packages
      // 写 autophone 的 path 入口，`import autophone` 只在 cwd 为仓库根时成立；
      // 沿用默认 cwd 会让 serve 直接 ModuleNotFoundError: No module named 'autophone'。
      cwd: repoRoot,
      command: [preflight, prepareAssets, prepareProducts, serve].filter(Boolean).join(' && '),
      url: `${baseURL}/health`,
      reuseExistingServer,
      timeout: 60_000,
      env: {
        AUTOPHONE_STATE_DIR: stateDir,
        ...productInputRoots(stateDir),
        AUTOPHONE_TEST_MODE: '1',
        AUTOPHONE_SCHEDULER_ENABLED: '0',
        // 显式清空：外层 shell 里若已 export 控制面 DSN 或看板口令，测试进程不继承。
        AUTOPHONE_DATABASE_URL: '',
        AUTOPHONE_AUTH: '',
        AUTOPHONE_APP_ASSET_DIR: appAssetDir || '',
        PATH: `${AI_CLI_GUARD_DIR}${path.delimiter}${process.env.PATH || ''}`,
        AUTOPHONE_CODEX_BIN: NO_AI_CODEX_BIN,
        VPHONE_CLI_DIR: ABSENT_VPHONE_CLI_DIR,
        VM_DIR: '',
        VPHONE_SOCK: '',
        AP_DEVICE: '',
        AUTOPHONE_FRIDA_HOST: '127.0.0.1:1',
        AP_FRIDA_HOST: '',
        AUTOPHONE_FRIDA_HOST_PORT: '',
        AUTOPHONE_SSH_PORT: '',
        ...(appAssetDir ? { AUTOPHONE_AI_PROVIDER: 'codex' } : {}),
      },
    },
  };
}

/** macOS 开发机复用已安装的 Chrome，Linux CI 用按版本装好的 Chromium。 */
export const browserChannel =
  process.env.AUTOPHONE_E2E_CHANNEL || (process.platform === 'darwin' ? 'chrome' : undefined);
