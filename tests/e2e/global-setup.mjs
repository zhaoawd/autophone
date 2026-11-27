// 跑测试之前先确认「回答请求的那个 dashboard 到底是什么」。
//
// 默认每轮起一台独立测试服务，这里的校验是回归守卫：env 没传进去、状态目录落回仓库根、
// 服务不是按测试模式起的，都在第一条用例之前失败，而不是等测试把状态写进主 checkout。
//
// 显式复用（AUTOPHONE_E2E_REUSE_SERVER=1）时它是准入闸：版本号不足以判定对方跑的是不是
// 本次要测的源码——同一版本号在两个 checkout 上完全相同，未提交改动也只是一个后缀，
// 所以这里比对 code_root、HEAD 与工作区脏标记。
import { execFileSync } from 'node:child_process';
import path from 'node:path';

import {
  repoRoot,
  reuseAcceptsDirtyTree,
  reuseExistingServer,
} from './server-common.mjs';

const IDENTITY_PATH = '/api/runtime/identity';

function fail(lines) {
  throw new Error(['[dashboard-e2e] 测试服务校验未通过：', ...lines.map(line => `  ${line}`)].join('\n'));
}

async function readIdentity(baseURL) {
  let lastError = null;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      const response = await fetch(`${baseURL}${IDENTITY_PATH}`);
      if (response.status === 401 || response.status === 403) {
        fail([
          `${baseURL}${IDENTITY_PATH} 需要鉴权（HTTP ${response.status}）。`,
          '测试服务不应配置 AUTOPHONE_AUTH；复用的服务配了口令时无法校验身份，请改用默认的独立服务。',
        ]);
      }
      if (response.status === 404) {
        fail([
          `${baseURL}${IDENTITY_PATH} 返回 404。`,
          '该进程的源码早于本接口，无法核对源码身份与状态目录，拒绝复用。',
        ]);
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      if (String(error?.message || '').startsWith('[dashboard-e2e]')) throw error;
      lastError = error;
      await new Promise(resolve => setTimeout(resolve, 300));
    }
  }
  fail([`读不到 ${baseURL}${IDENTITY_PATH}：${lastError?.message || '未知原因'}。`]);
}

function localHead() {
  try {
    return execFileSync('git', ['-C', repoRoot, 'rev-parse', 'HEAD'], {
      encoding: 'utf-8',
      timeout: 10_000,
    }).trim();
  } catch {
    return null;
  }
}

export default async function globalSetup(config) {
  const baseURL = config.projects[0]?.use?.baseURL;
  const expectedStateDir = config.webServer?.env?.AUTOPHONE_STATE_DIR;
  if (!baseURL) fail(['配置里没有 baseURL，无法定位测试服务。']);

  const identity = await readIdentity(baseURL);
  const codeRoot = path.resolve(String(identity?.roots?.code || ''));
  const stateRoot = path.resolve(String(identity?.roots?.state || ''));
  const expected = path.resolve(repoRoot);

  if (identity?.test_mode !== true) {
    fail([
      `${baseURL} 上的服务不是按测试模式启动的（test_mode=false）。`,
      '测试只对 AUTOPHONE_TEST_MODE=1 起的服务执行，避免把回归打到日常使用的看板上。',
    ]);
  }
  if (codeRoot !== expected) {
    fail([
      `${baseURL} 上的服务跑的是另一份源码。`,
      `它的 code_root=${codeRoot}`,
      `本次要测的 code_root=${expected}`,
    ]);
  }
  if (stateRoot === expected) {
    fail([
      `${baseURL} 上的服务把状态根指到了仓库本身（state=${stateRoot}）。`,
      '浏览器回归必须用隔离状态目录，否则 runs/、锁根与注册表会被测试写脏。',
    ]);
  }
  // 状态目录是本轮新建的 ⇒ 这台服务就是 Playwright 刚起的那一台，不是复用来的。
  // 开了复用开关但现场没有可复用的服务时也会走到这里。
  const startedByUs = Boolean(expectedStateDir) && stateRoot === path.resolve(expectedStateDir);

  // App 资产根：配置要求隔离时（真实后端类），服务自报的 roots.app_asset 必须是本轮副本；
  // 复用来的服务至少要指在 test-results 下，不能是仓库本身。
  const expectedAppAssetDir = config.webServer?.env?.AUTOPHONE_APP_ASSET_DIR;
  if (expectedAppAssetDir) {
    const reported = identity?.roots?.app_asset;
    if (!reported) {
      fail([
        `${baseURL} 上的服务没有自报 roots.app_asset。`,
        '该进程的源码早于 App 资产根开关，导入提交等写请求会落进仓库里的 apps/，拒绝执行。',
      ]);
    }
    const appAssetRoot = path.resolve(String(reported));
    const sandbox = path.join(expected, 'test-results') + path.sep;
    if (appAssetRoot === expected || !appAssetRoot.startsWith(sandbox)) {
      fail([
        `${baseURL} 上的服务的 App 资产根不在 test-results 下（app_asset=${appAssetRoot}）。`,
        '真实后端类会提交导入与 Recipe 写入，App 资产根必须指向本轮副本。',
      ]);
    }
    if (startedByUs && appAssetRoot !== path.resolve(expectedAppAssetDir)) {
      fail([
        '本轮起的测试服务没有用到本轮的 App 资产副本。',
        `它的 app_asset=${appAssetRoot}`,
        `本轮应为=${path.resolve(expectedAppAssetDir)}`,
      ]);
    }
  }

  if (!startedByUs && !reuseExistingServer) {
    fail([
      '本轮起的测试服务没有用到本轮的隔离状态目录。',
      `它的 state=${stateRoot}`,
      `本轮应为=${path.resolve(expectedStateDir)}`,
      'webServer.env 未生效时会出现这种情况，先检查配置再重跑。',
    ]);
  }
  if (reuseExistingServer && !startedByUs) {
    const head = localHead();
    if (!head || !identity?.source?.head) {
      fail([
        '复用模式下读不出 HEAD（本地或服务端），无法判定两边是不是同一份源码。',
        '去掉 AUTOPHONE_E2E_REUSE_SERVER，用默认的独立测试服务。',
      ]);
    }
    if (identity.source.head !== head) {
      fail([
        `${baseURL} 上的服务停在另一个提交上。`,
        `它的 HEAD=${identity.source.head}`,
        `本地 HEAD=${head}`,
      ]);
    }
    if (identity.source.dirty !== false && !reuseAcceptsDirtyTree) {
      const state = identity.source.dirty === null ? '读不出工作区状态' : '工作区有未提交改动';
      fail([
        `${state}：复用的进程可能还跑在改动之前的 Python 代码上（静态资源每次请求实读，Python 路由不会）。`,
        '要么用默认的独立测试服务，要么设 AUTOPHONE_E2E_REUSE_DIRTY=1 明确接受这个风险。',
      ]);
    }
  }
}
