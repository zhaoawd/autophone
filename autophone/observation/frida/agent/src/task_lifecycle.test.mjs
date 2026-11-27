import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function harness() {
  const source = readFileSync(new URL('./net_tap.js', import.meta.url), 'utf8');
  const begin = source.indexOf('var CAPTURE_ID =');
  const end = source.indexOf('// App-scoped capture policy.', begin);
  assert.ok(begin >= 0 && end > begin, 'production lifecycle implementation missing');
  const sent = [];
  const state = { state: 0, tid: 1, received: 0 };
  const task = {
    $className: 'SyntheticTask', $class: null,
    state: () => state.state, taskIdentifier: () => state.tid,
    countOfBytesReceived: () => state.received, countOfBytesSent: () => 0,
    response: () => null, error: () => null,
  };
  const context = { ptr: value => value, send: e => sent.push(e), CAPTURING: true, Process: { id: 42 },
    ObjC: { Object: function () { return task; } }, C: {}, intOf: Number,
    setTimeout, Date, httpResponseStatus: () => null };
  vm.createContext(context); vm.runInContext(source.slice(begin, end), context);
  return { context, sent, state };
}

test('cancel requested remains pending until an observed completed task state', async () => {
  const { context: c, state, sent } = harness();
  const t = c.observeTask('0x1', false);
  t.request_observed = true;
  c.emitTaskLifecycle(t, 'cancel_requested');
  assert.equal(t.terminal, false);
  const seal = await c.sealCapture(0);
  assert.equal(seal.pending.length, 1);
  assert.equal(seal.pending[0].task_instance_id, t.task_instance_id);
  assert.equal(seal.pending[0].cancel_requested, true);
  assert.equal(sent.at(-1).ev, 'capture_sealed');
  assert.equal(seal.event_seq, sent.at(-1).event_seq);
  assert.ok(sent.every((e, i) => e.event_seq === i + 1 && e.capture_id === seal.capture_id));
});

test('state completed proves terminal but does not invent an HTTP response', async () => {
  const { context: c, state, sent } = harness();
  const t = c.observeTask('0x1', false);
  t.request_observed = true;
  state.state = 3;
  c.emitTaskLifecycle(t, 'state_observed');
  assert.equal(t.terminal, true);
  assert.equal(t.status, null);
  assert.equal(sent.filter(e => e.ev === 'resp').length, 0);
  assert.equal((await c.sealCapture(0)).pending.length, 1);
});

test('pointer reuse and duplicate resume preserve distinct task instances', () => {
  const { context: c, state } = harness();
  const first = c.observeTask('0x1', false);
  assert.equal(c.observeTask('0x1', false).task_instance_id, first.task_instance_id);
  state.tid = 2;
  const second = c.observeTask('0x1', false);
  assert.notEqual(second.task_instance_id, first.task_instance_id);
  assert.equal(first.terminal, false);
});

test('bounded drain keeps late terminal events before seal acknowledgement', async () => {
  const { context: c, sent, state } = harness();
  const t = c.observeTask('0x1', false);
  t.request_observed = true;
  setTimeout(() => { state.state = 3; c.emitTaskLifecycle(t, 'completion'); }, 5);
  const seal = await c.sealCapture(70);
  assert.equal(seal.pending.length, 0);
  assert.ok(sent.find(e => e.ev === 'task_lifecycle' && e.terminal));
  const count = sent.length;
  c.emitAgentEvent({ ev: 'req', task_ptr: '0x1' });
  assert.equal(sent.length, count);
});

test('native response nil and getter error remain distinguishable', () => {
  const { context: c } = harness();
  assert.equal(c.taskSnapshot('0x1').native_response_present, false);
  c.ObjC.Object = function () { return { response() { throw new Error('read failed'); } }; };
  assert.equal(c.taskSnapshot('0x2').native_response_present, null);
});

test('creation alone cannot claim lifecycle coverage when hooks are absent', () => {
  const { context: c } = harness();
  assert.equal(c.observeTask('0x1', true).coverage_gap, true);
});

test('creation-return observation installs passive hooks once and records actual state transition', () => {
  const { context: c, state, sent } = harness();
  const hooks = {};
  const cls = { '- cancel': { implementation: 'cancel-imp' },
    '- setState:': { implementation: 'state-imp' }, $superClass: null, toString: () => 'SyntheticTask' };
  c.Interceptor = { attach(address, callbacks) { hooks[address] = callbacks; } };
  c.ObjC.Object = function () { return { $class: cls, state: () => state.state,
    taskIdentifier: () => state.tid, countOfBytesReceived: () => 0, countOfBytesSent: () => 0,
    response: () => null, error: () => null }; };
  state.state = 1;
  const task = c.observeCreatedTask('0x1');
  assert.equal(task.coverage_gap, false);
  assert.equal(c.observeCreatedTask('0x1').task_instance_id, task.task_instance_id);
  assert.equal(Object.keys(c.TASK_INSTANCES).length, 1);
  const invocation = {};
  hooks['cancel-imp'].onEnter.call(invocation, ['0x1']);
  assert.equal(task.cancel_requested, true);
  assert.equal(task.terminal, false);
  hooks['state-imp'].onEnter.call(invocation, ['0x1']);
  state.state = 3;
  hooks['state-imp'].onLeave.call(invocation);
  assert.equal(sent.at(-1).terminal, true);
  assert.equal(sent.at(-1).terminal_source, 'state_observed');
});

test('early lifecycle install uses named factories, stays passive and is idempotent', () => {
  const { context: c, sent } = harness();
  const attached = [];
  c.RESUME_HOOK_MODE = 'none';
  c.installPassiveResumeHook = () => { c.RESUME_HOOK_MODE = 'passive'; };
  c.C.NSURLSession = { '- dataTaskWithRequest:': { implementation: 'factory' } };
  c.Interceptor = { attach: (imp, callbacks) => attached.push({ imp, callbacks }) };
  const ready = c.installCaptureLifecycleHooks();
  assert.equal(ready.ok, true);
  assert.equal(ready.resume_mode, 'passive');
  assert.equal(ready.class_enumeration, false);
  c.installCaptureLifecycleHooks();
  assert.equal(attached.length, 1);
  assert.equal(sent.at(-1).ev, 'lifecycle_coverage_ready');
});

test('missing named factory implementations cannot report coverage ready', () => {
  const { context: c, sent } = harness();
  c.RESUME_HOOK_MODE = 'none';
  c.installPassiveResumeHook = () => { c.RESUME_HOOK_MODE = 'passive'; };
  c.C.NSURLSession = {};
  const ready = c.installCaptureLifecycleHooks();
  assert.equal(ready.ok, false);
  assert.equal(sent.at(-1).ev, 'lifecycle_coverage_failed');
});

test('stored task pointer strings are converted at native ObjC boundaries', () => {
  const { context: c, state, sent } = harness();
  const hooks = [];
  const cls = { '- cancel': { implementation: 'cancel-imp' },
    '- setState:': { implementation: 'state-imp' }, $superClass: null, toString: () => 'NativeTask' };
  c.ptr = address => ({ toString: () => address });
  c.Interceptor = { attach: (imp, callbacks) => hooks.push(callbacks) };
  c.ObjC.Object = function (address) {
    if (typeof address === 'string') throw new TypeError('NativePointer required');
    return { $class: cls, state: () => state.state, taskIdentifier: () => state.tid,
      countOfBytesReceived: () => state.received, countOfBytesSent: () => 0,
      response: () => null, error: () => null };
  };
  state.state = 1;
  const task = c.observeCreatedTask(c.ptr('0x123'));
  state.state = 3;
  c.emitTaskLifecycle(task, 'state_observed');
  assert.equal(sent.at(-1).state, 3);
  assert.deepEqual(Array.from(sent.at(-1).snapshot_errors), []);
  assert.equal(sent.at(-1).tid, 1);
  assert.equal(sent.at(-1).bytes_received, 0);
  assert.equal(sent.at(-1).native_response_present, false);
  const fromStoredPointer = { hook_coverage: [], coverage_gap: false };
  c.installTaskLifecycleHooks(task.task_ptr, fromStoredPointer);
  assert.equal(fromStoredPointer.coverage_gap, false);
  assert.equal(fromStoredPointer.hook_coverage.length, 2);
});

test('native completion waits for the response callback before sealing', async () => {
  const { context: c, state, sent } = harness();
  const t = c.observeTask('0x1', false);t.request_observed = true;
  state.state = 3;c.emitTaskLifecycle(t, 'state_observed');
  setTimeout(() => {
    c.emitAgentEvent({ev:'resp',task_ptr:'0x1',status:200});
    c.emitTaskLifecycle(t,'delegate_done');
  },10);
  const ack=await c.sealCapture(100);
  assert.equal(ack.pending.length,0);
  assert.equal(sent.filter(e=>e.ev==='resp').length,1);
  assert.ok(sent.findIndex(e=>e.ev==='resp')<sent.findIndex(e=>e.ev==='capture_sealed'));
});

test('native completion without callback remains explicit pending at drain deadline', async () => {
  const {context:c,state}=harness();const t=c.observeTask('0x1',false);t.request_observed=true;
  state.state=3;c.emitTaskLifecycle(t,'state_observed');
  assert.equal((await c.sealCapture(0)).pending.length,1);
});

test('verified native zero-response cancellation can seal without a callback', async () => {
  const {context:c}=harness();
  c.ObjC.Object=function(){return {state:()=>3,taskIdentifier:()=>1,
    countOfBytesReceived:()=>0,countOfBytesSent:()=>0,response:()=>null,
    error:()=>({isNull:()=>false,code:()=>-999,domain:()=> 'NSURLErrorDomain'})};};
  const task=c.observeTask('0x1',false);task.request_observed=true;
  c.emitTaskLifecycle(task,'state_observed');
  assert.equal(task.callback_complete,false);
  assert.equal((await c.sealCapture(0)).pending.length,0);
});

test('callback before native state3 cannot prematurely seal or poll dead pointers', async () => {
  const {context:c,state}=harness(); const t=c.observeTask('0x1',false); t.request_observed=true;
  state.state=0;c.emitTaskLifecycle(t,'completion');
  c.ObjC.Object=function(){throw new Error('no polling native pointers');};
  assert.equal((await c.sealCapture(0)).pending.length,1);
});

test('controlled suspended task waits for explicit synthetic delivery completion', async () => {
  const {context:c,state}=harness();state.state=1;
  const t=c.observeTask('0x1',true);t.request_observed=true;t.controlled_local=true;
  t.delivery_pending=true;t.synthetic_callback_complete=false;
  const ack=await c.sealCapture(0);assert.equal(ack.pending.length,1);
});

test('successful controlled callback preserves native suspended state at seal', async () => {
  const {context:c,state}=harness();state.state=1;
  const t=c.observeTask('0x1',true);t.request_observed=true;t.controlled_local=true;
  t.delivery_pending=false;t.synthetic_callback_complete=true;
  c.emitTaskLifecycle(t,'synthetic_completion');
  assert.equal(t.state,1);assert.equal(t.terminal,false);
  assert.equal((await c.sealCapture(0)).pending.length,0);
});

test('new creation after a synthetic callback has distinct identity without inventing native terminal',()=>{
  const {context:c,state}=harness();state.state=1;
  const first=c.observeTask('0x1',true);first.request_observed=true;
  first.controlled_local=true;first.synthetic_callback_complete=true;
  state.tid=2;const next=c.observeTask('0x1',true);
  assert.notEqual(first.task_instance_id,next.task_instance_id);
  assert.equal(first.terminal,false);assert.equal(next.identity_ambiguous,false);
});

for (const [field,value] of [['state',0],['bytes_sent',1],['bytes_received',1],['native_response_present',true]])
  test(`new creation cannot excuse prior controlled native ${field}=${value}`,()=>{
    const {context:c,state}=harness();state.state=1;
    const first=c.observeTask('0x1',true);first.request_observed=true;
    first.controlled_local=true;first.synthetic_callback_complete=true;first[field]=value;
    state.tid=2;assert.equal(c.observeTask('0x1',true).identity_ambiguous,true);
  });
