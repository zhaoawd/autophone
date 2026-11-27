import fs from 'node:fs';
import path from 'node:path';

// Resource types only: no arguments, environment, request bodies, or handle contents.
export function startWorkerDiagnostics(info) {
  const root = process.env.AUTOPHONE_E2E_RUN_DIR;
  if (!root) return () => {};
  const dir = path.join(root, 'workers');
  fs.mkdirSync(dir, { recursive: true });
  const target = path.join(dir, `${info.workerIndex}-${process.pid}.jsonl`);
  const write = (event, fields = {}) => fs.appendFileSync(target, JSON.stringify({
    event, pid: process.pid, ppid: process.ppid, worker_index: info.workerIndex,
    at: new Date().toISOString(), monotonic_ms: performance.now(),
    active_resource_types: process.getActiveResourcesInfo(), ...fields,
  }) + '\n');
  write('worker_setup');
  const timer = setInterval(() => write('heartbeat'), 10000);
  timer.unref();
  process.once('beforeExit', code => write('before_exit', { code }));
  process.once('exit', code => write('exit', { code }));
  // Keep the unref'd sampler alive after fixture teardown to observe shutdown waits.
  return write;
}
