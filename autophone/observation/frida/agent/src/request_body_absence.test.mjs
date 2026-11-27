import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function observe(bodyReader, { missingRequest = false } = {}) {
  const source = readFileSync(new URL('./net_tap.js', import.meta.url), 'utf8');
  const begin = source.indexOf('function observeResumeRequest(');
  const end = source.indexOf('function installPassiveResumeHook(', begin);
  const sent = [];
  const request = { isNull: () => false, handle: 'request', HTTPBody: bodyReader };
  const task = {
    currentRequest: () => missingRequest ? null : request,
    originalRequest: () => null,
    taskIdentifier: () => 1,
  };
  const context = { ptr: value => value,
    ObjC: { Object: function () { return task; } },
    CAPTURING: true, Process: { id: 1 }, C: {}, setTimeout,
    readReqMeta: () => ({ url: 'https://example.invalid/body' }),
    rememberCapturePolicy: () => 'full',
    dataBytes: body => ({ len: body == null ? -1 : 0, b64: null, truncated: false }),
    send: event => sent.push(event),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('var CAPTURE_ID ='), source.indexOf('// App-scoped capture policy.')), context);
  vm.runInContext(source.slice(begin, end), context);
  context.observeResumeRequest('synthetic-task');
  return sent.find(event => event.ev === 'req');
}

for (const nil of [null, { isNull: () => true, handle: null }]) {
  test('request body absence requires a successful HTTPBody nil read', () => {
    const event = observe(() => nil);
    assert.equal(event.body_absent, true);
    assert.equal(event.body_err, null);
    assert.equal(event.body_len, -1);
  });
}

test('HTTPBody read failure cannot become an absent request body', () => {
  const event = observe(() => { throw new Error('synthetic read failure'); });
  assert.equal(event.body_absent, false);
  assert.ok(event.body_err);
});

test('a missing request cannot prove body absence', () => {
  const event = observe(() => { throw new Error('must not read'); }, { missingRequest: true });
  assert.equal(event.body_absent, false);
});

test('an observed empty NSData is not an absent body', () => {
  const event = observe(() => ({ isNull: () => false, handle: 'empty-data' }));
  assert.equal(event.body_absent, false);
  assert.equal(event.body_len, 0);
  assert.equal(event.body_err, null);
});
