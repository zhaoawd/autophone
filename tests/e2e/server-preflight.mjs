// dashboard 测试服务的启动前置检查。
//
// Playwright 的 `webServer` 在 `reuseExistingServer: false` 下只有一种内建判定：
// 先请求 `url`（这里是 /health），能拿到 2xx/3xx 才算「已被占用」并报英文错误。
// 端口被**无关进程**占用时 /health 不会应答，Playwright 于是照常启动 uvicorn，
// 真实失败是 uvicorn 绑不上端口后立刻退出，报出来的却是「Process from
// config.webServer exited early」。这个脚本把那种情况提前拦下并说清原因。
//
// 它是 webServer 命令的第一段，失败即整轮失败；成功时顺手建好本轮的隔离状态目录。
import net from 'node:net';
import fs from 'node:fs';

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const port = Number(argValue('--port'));
const stateDir = argValue('--state-dir');
const host = argValue('--host') || '127.0.0.1';

// 只有「连接被拒绝」允许继续启动。连上是占用；连接超时或其他错误均拒绝启动，
// 但不能据此确认端口被占用。
// ——例如已有进程监听但不 accept（backlog 排满）时 SYN 被静默丢弃，超时按空闲处理会让
// uvicorn 接着去绑一个绑不上的端口，报出来的却是「进程提前退出」。
function probePort() {
  return new Promise(resolve => {
    const socket = net.connect({ host, port });
    const done = value => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(3000);
    socket.once('connect', () => done({ busy: true, confirmed: true, reason: '已有进程在监听' }));
    socket.once('timeout', () => done({ busy: true, reason: '连接超时，端口占用状态未知' }));
    socket.once('error', error => (error.code === 'ECONNREFUSED'
      ? done({ busy: false })
      : done({ busy: true, reason: `探测失败：${error.code || error.message}` })));
  });
}

const probe = await probePort();
if (probe.busy) {
  process.stderr.write(`[dashboard-e2e] 端口探测结论：${probe.reason}\n`);
  if (!probe.confirmed) {
    process.stderr.write(`[dashboard-e2e] 无法确认 ${host}:${port} 可用，测试服务未启动。请检查本机网络权限与连接状态。\n`);
    process.exit(1);
  }
  process.stderr.write(
    `[dashboard-e2e] 端口 ${host}:${port} 已被占用，测试服务无法启动。\n` +
    '  本轮按「默认起一台独立测试服务」执行，不复用任何已有进程。处理方式二选一：\n' +
    `  1. 关掉占用该端口的进程（lsof -nP -iTCP:${port} -sTCP:LISTEN）；\n` +
    `  2. 换一个端口：AUTOPHONE_E2E_PORT=8791 npm run test:dashboard-e2e。\n` +
    '  确实要复用一台已经起着的 dashboard 时，显式设 AUTOPHONE_E2E_REUSE_SERVER=1，\n' +
    '  它会先校验对方的源码身份、测试模式与状态目录。\n');
  process.exit(1);
}

if (stateDir) fs.mkdirSync(stateDir, { recursive: true });
