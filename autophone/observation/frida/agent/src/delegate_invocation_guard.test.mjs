import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { guardDelegateCallbacks } from './delegate_invocation_guard.mjs';
import { createDelegateBodyAccumulator, planDelegateBodyChunk, recordDelegateBodyChunk } from './delegate_body_accumulator.mjs';

function captureHarness() {
  const code = readFileSync(new URL('./net_tap.js', import.meta.url), 'utf8');
  const begin = code.indexOf('function hookDelegateImpl(');
  const end = code.indexOf('// 只读抓取用的 delegate', begin);
  const hooks = [], sent = [], pending = {};
  const context = { ptr: value => value,
    guardDelegateCallbacks, CAPTURING: true, BODY_CAP: 1000, Process: { id: 1 }, C: {}, setTimeout,
    Interceptor: { attach: (_address, callbacks) => hooks.push(callbacks) },
    capturePolicyForTask: () => 'full', forgetCapturePolicy() {}, PENDING: pending,
    pendingFor: task => pending[task] ||= { status: 200, headers: {}, is_menu: false, body: createDelegateBodyAccumulator(1000) },
    ObjC: { Object: function (value) { return value === 'task' ? {retain() {}, release() {}} : value; } }, intOf: Number,
    planDelegateBodyChunk, recordDelegateBodyChunk,
    copyDataBytes: data => ({bytes: data.bytes, error: null}),
    isMenuListUrl: () => false, taskRequestUrl: () => '',
    dataFromCopiedChunks: chunks => ({ handle: Buffer.concat(chunks.map(c => Buffer.from(c.bytes))) }),
    dataBytes: data => ({ b64: data.toString('base64'), err: null }), send: value => sent.push(value),
  };
  vm.createContext(context);
  vm.runInContext(code.slice(code.indexOf('var CAPTURE_ID ='), code.indexOf('// App-scoped capture policy.')), context);
  vm.runInContext(code.slice(begin, end), context);
  context.hookDelegateImpl({ implementation: 'outer' }, 'data');
  context.hookDelegateImpl({ implementation: 'forwarded' }, 'data');
  context.hookDelegateImpl({ implementation: 'done' }, 'done');
  return { hooks, sent };
}

function invoke(hook, args, nested = () => {}) {
  const invocation = { threadId: 1 };
  hook.onEnter.call(invocation, args);
  try { nested(); } finally { hook.onLeave?.call(invocation); }
}

test('real delegate capture hook records a synchronously forwarded NSData once', () => {
  const { hooks, sent } = captureHarness();
  const bytes = new TextEncoder().encode('{"data":1}').buffer;
  const data = { bytes, length: () => bytes.byteLength, isNull: () => false, toString: () => 'data-pointer' };
  const args = [null, null, null, 'task', data];
  invoke(hooks[0], args, () => invoke(hooks[1], args));
  invoke(hooks[2], [null, null, null, 'task', null]);
  const response = sent.find(row => row.ev === 'resp');
  assert.equal(Buffer.from(response.body_b64, 'base64').toString(), '{"data":1}');
  assert.equal(response.body_len, bytes.byteLength);
  const terminal = sent.findLast(row => row.ev === 'task_lifecycle');
  assert.equal(terminal.response_observed, false);
  assert.equal(terminal.data_callbacks, 1);
  assert.equal(terminal.observed_data_bytes, bytes.byteLength);
});

test('separate callbacks with identical NSData bytes remain distinct chunks', () => {
  const { hooks, sent } = captureHarness();
  const bytes = new TextEncoder().encode('A').buffer;
  const data = { bytes, length: () => 1, isNull: () => false, toString: () => 'reused-pointer' };
  const args = [null, null, null, 'task', data];
  invoke(hooks[0], args); invoke(hooks[0], args);
  invoke(hooks[2], [null, null, null, 'task', null]);
  assert.equal(Buffer.from(sent.find(row => row.ev === 'resp').body_b64, 'base64').toString(), 'AA');
});

test('reentry diagnostic uses the capture event sink for sequence and identity', () => {
  const events = [];
  const hook = guardDelegateCallbacks('body', { onEnter() {} }, event => events.push(event));
  const args = [null, null, null, 'task', 'data'];
  const outer = { threadId: 77 }, inner = { threadId: 77 };
  hook.onEnter.call(outer, args);
  hook.onEnter.call(inner, args);
  hook.onLeave.call(inner);
  hook.onLeave.call(outer);
  assert.equal(events.length, 1);
  assert.equal(events[0].ev, 'delegate_reentry_skipped');
});
