// Keep evidence until the Playwright process exits, including worker shutdown errors.
import fs from 'node:fs';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const repoRoot = fileURLToPath(new URL('..', import.meta.url));
const kinds = { mock: 'dashboard-e2e', backend: 'dashboard-backend-e2e' };

export function processSnapshot(pid) {
  const result = spawnSync('ps', ['-axo', 'pid=,ppid=,stat=,comm='], {
    encoding: 'utf8', timeout: 1000, maxBuffer: 4 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) return { state: 'unknown', reason: result.error?.code || 'ps_failed' };
  const rows = result.stdout.split('\n').flatMap(line => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\S+)\s+(.+)$/);
    return match ? [{ pid: +match[1], ppid: +match[2], state: match[3], executable: match[4] }] : [];
  });
  const ids = new Set([pid]);
  for (let size = -1; size !== ids.size;) {
    size = ids.size;
    for (const row of rows) if (ids.has(row.ppid)) ids.add(row.pid);
  }
  return { state: 'observed', processes: rows.filter(row => ids.has(row.pid)) };
}

export async function runWithEvidence({ root, kind, args = [], command, commandArgs, env = process.env }) {
  if (!kinds[kind]) throw new Error('Expected mock or backend');
  const parent = path.join(root, 'test-results', `${kinds[kind]}-runs`);
  fs.mkdirSync(parent, { recursive: true });
  const runDir = fs.mkdtempSync(path.join(parent, `${new Date().toISOString().replace(/[:.]/g, '-')}-`));
  const childEnv = { ...env, AUTOPHONE_E2E_RUN_DIR: runDir };
  const config = `tests/e2e/playwright${kind === 'backend' ? '.backend' : ''}.config.mjs`;
  const argv = commandArgs ?? [path.join(root, 'node_modules/@playwright/test/cli.js'), 'test', '--config', config, ...args];
  const git = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' });
  const dirty = spawnSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' });
  const diff = spawnSync('git', ['diff', 'HEAD', '--binary'], { cwd: root, maxBuffer: 16 * 1024 * 1024 });
  const record = {
    schema: 'autophone.browser-run-evidence/1', kind, run_dir: runDir,
    started_at: new Date().toISOString(), source_head: git.status === 0 ? git.stdout.trim() : null,
    source_dirty: dirty.status === 0 ? Boolean(dirty.stdout.trim()) : null,
    tracked_diff_sha256: diff.status === 0 ? createHash('sha256').update(diff.stdout).digest('hex') : null,
    node: process.version, args, status: 'running',
    roots: { state: path.join(runDir, 'state'), assets: path.join(runDir, 'assets') },
  };
  const save = () => fs.writeFileSync(path.join(runDir, 'execution.json'), JSON.stringify(record, null, 2) + '\n');
  save();
  process.stdout.write(`Browser evidence: ${runDir}\n`);
  const log = fs.openSync(path.join(runDir, 'stdout-stderr.log'), 'a');
  const events = fs.openSync(path.join(runDir, 'process-events.jsonl'), 'a');
  const event = (name, extra = {}) => fs.writeSync(events, JSON.stringify({
    event: name, at: new Date().toISOString(), monotonic_ms: performance.now(), ...extra,
  }) + '\n');
  let child;
  let signal = null;
  let timer;
  const handlers = new Map();
  let code = 1;
  try {
    child = spawn(command || process.execPath, argv, { cwd: root, env: childEnv, stdio: ['inherit', 'pipe', 'pipe'] });
    record.pid = child.pid;
    save();
    event('spawn', { pid: child.pid });
    const snapshot = () => event('process_snapshot', processSnapshot(child.pid));
    for (const [stream, output] of [[child.stdout, process.stdout], [child.stderr, process.stderr]]) {
      stream.on('data', chunk => { fs.writeSync(log, chunk); output.write(chunk); });
    }
    timer = setInterval(snapshot, 10000);
    timer.unref();
    for (const name of ['SIGINT', 'SIGTERM']) {
      const handler = () => { signal = name; event('signal', { signal: name }); snapshot(); child.kill(name); };
      handlers.set(name, handler);
      process.on(name, handler);
    }
    const result = await new Promise(resolve => {
      child.once('error', error => { event('spawn_error', { code: error.code }); resolve({ code: 1 }); });
      child.once('exit', (exitCode, exitSignal) => { event('exit', { code: exitCode, signal: exitSignal }); snapshot(); });
      child.once('close', (exitCode, exitSignal) => resolve({ code: exitCode, signal: exitSignal }));
    });
    signal ||= result.signal;
    code = signal === 'SIGINT' ? 130 : signal === 'SIGTERM' ? 143 : (result.code ?? 1);
    record.status = code === 0 ? 'passed' : 'failed';
    record.exit_code = code;
    record.signal = signal;
    const keep = ['1', 'true', 'yes', 'on'].includes(String(env.AUTOPHONE_E2E_KEEP_STATE || '').trim().toLowerCase());
    record.state_preserved = code !== 0 || keep;
    // The wrapper owns only these two exact directories, never a caller-provided path.
    if (!record.state_preserved) {
      for (const dir of Object.values(record.roots)) fs.rmSync(dir, { recursive: true, force: true });
    }
  } finally {
    clearInterval(timer);
    for (const [name, handler] of handlers) process.off(name, handler);
    record.finished_at = new Date().toISOString();
    save();
    fs.closeSync(log);
    fs.closeSync(events);
  }
  return { code, runDir };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [kind, ...args] = process.argv.slice(2);
  process.exitCode = (await runWithEvidence({ root: repoRoot, kind, args })).code;
}
