import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { guardDelegateCallbacks } from './delegate_invocation_guard.mjs';
import { createDelegateBodyAccumulator, planDelegateBodyChunk, recordDelegateBodyChunk } from './delegate_body_accumulator.mjs';

function harness({ missingDelegate = false } = {}) {
  const code = readFileSync(new URL('./net_tap.js', import.meta.url), 'utf8');
  const hooks = new Map(), sent = [], pending = {};
  const selectors = ['- URLSession:dataTask:didReceiveResponse:completionHandler:',
    '- URLSession:dataTask:didReceiveData:', '- URLSession:task:didCompleteWithError:'];
  const cls = { $ownMethods: selectors };
  selectors.forEach((sel, index) => { cls[sel] = { implementation: `callback-${index}` }; });
  const session = { isNull: () => false,
    delegate: () => missingDelegate ? null : { isNull: () => false, $class: cls, $className: 'ObservedDelegate' } };
  const taskPointer = { isNull: () => false, toString: () => 'task' };
  const task = { retain() {}, release() {}, $class: null, state: () => 1, taskIdentifier: () => 1,
    countOfBytesReceived: () => 0, countOfBytesSent: () => 0, response: () => null, error: () => null,
    session: () => { throw new Error('factory session identity must be used'); } };
  const classes = { NSURLSession: { '- dataTaskWithRequest:': { implementation: 'factory' } }, ObservedDelegate: cls };
  const context = {
    C: new Proxy(classes, { ownKeys() { throw new Error('class enumeration forbidden'); } }),
    guardDelegateCallbacks, CAPTURING: true, BODY_CAP: 1000, setTimeout,
    Process: { id: 1, findModuleByAddress: () => ({ path: '/App/Synthetic' }) }, ptr: value => value,
    Interceptor: { attach(imp, callbacks) {
      if (hooks.has(imp)) throw new Error(`duplicate native interceptor: ${imp}`);
      hooks.set(imp, callbacks);
    } },
    ObjC: { Object: function (value) {
      if (String(value) === 'task') return task;
      if (value === 'session') return session;
      return value;
    } },
    capturePolicyForTask: () => 'full', forgetCapturePolicy() {}, PENDING: pending,
    pendingFor: id => pending[id] ||= { status: 200, headers: {}, is_menu: false, body: createDelegateBodyAccumulator(1000) },
    planDelegateBodyChunk, recordDelegateBodyChunk, intOf: Number,
    copyDataBytes: data => ({ bytes: data.bytes, error: null }),
    isMenuListUrl: () => false, taskRequestUrl: () => '',
    dataFromCopiedChunks: chunks => ({ handle: Buffer.concat(chunks.map(chunk => Buffer.from(chunk.bytes))) }),
    dataBytes: bytes => ({ b64: bytes.toString('base64'), err: null }), send: event => sent.push(event),
  };
  vm.createContext(context);
  vm.runInContext(code.slice(code.indexOf('var CAPTURE_ID ='), code.indexOf('// App-scoped capture policy.')), context);
  const begin = code.indexOf('function hookDelegateImpl(');
  const end = code.indexOf('// 注意：**load 期不自动装 delegate', begin);
  vm.runInContext(code.slice(begin, end), context);
  context.installTaskCreationHooks();
  const factoryInvocation = {};
  hooks.get('factory').onEnter?.call(factoryInvocation, ['session']);
  hooks.get('factory').onLeave.call(factoryInvocation, taskPointer);
  return { context, hooks, sent };
}

function invoke(callback, args) {
  const invocation = { threadId: 1 };
  callback.onEnter?.call(invocation, args);
  callback.onLeave?.call(invocation);
}

function deliver(hooks, text) {
  const bytes = new TextEncoder().encode(text).buffer;
  invoke(hooks.get('callback-1'), [null, null, 'session', 'task',
    { bytes, length: () => bytes.byteLength, isNull: () => false }]);
}

test('factory return captures the actual session delegate before the first response body', () => {
  const { hooks, sent } = harness();
  assert.equal(hooks.has('callback-0'), true);
  assert.equal(hooks.has('callback-1'), true);
  assert.equal(hooks.has('callback-2'), true);
  const observed = sent.find(event => event.ev === 'task_lifecycle');
  assert.equal(observed.delegate_coverage_gap, false);
  assert.equal(observed.delegate_hook_coverage.length, 3);
  deliver(hooks, 'first-body');
  invoke(hooks.get('callback-2'), [null, null, 'session', 'task', null]);
  assert.equal(Buffer.from(sent.find(event => event.ev === 'resp').body_b64, 'base64').toString(), 'first-body');
});

test('later named delegate installation cannot duplicate an early callback or collapse identical chunks', () => {
  const { context, hooks, sent } = harness();
  assert.equal(hooks.has('callback-1'), true);
  context.hookDelegates(['ObservedDelegate']);
  context.hookDelegateImpl({ implementation: 'callback-1' }, 'data');
  deliver(hooks, 'A');
  deliver(hooks, 'A');
  invoke(hooks.get('callback-2'), [null, null, 'session', 'task', null]);
  const responses = sent.filter(event => event.ev === 'resp');
  assert.equal(responses.length, 1);
  assert.equal(Buffer.from(responses[0].body_b64, 'base64').toString(), 'AA');
  assert.equal(sent.findLast(event => event.ev === 'task_lifecycle').data_callbacks, 2);
});

test('missing actual delegate records a coverage gap without inventing response hooks', () => {
  const { hooks, sent } = harness({ missingDelegate: true });
  assert.equal(hooks.size, 1);
  const observed = sent.find(event => event.ev === 'task_lifecycle');
  assert.equal(observed.delegate_coverage_gap, true);
  assert.equal(observed.delegate_hook_coverage.length, 0);
});
