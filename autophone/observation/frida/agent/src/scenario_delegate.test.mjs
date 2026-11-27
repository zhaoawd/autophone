'use strict';
// scenario_delegate.mjs 纯决策层单测（node --test，无 frida/VM）。
// 覆盖：step 编排、selector 去重、可行性 fail-closed、timeout 需求更少的非对称、缺省兜底。

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  planDelegateSteps,
  requiredSelectors,
  planDelegateShortCircuit,
  SEL_RESPONSE,
  SEL_DATA,
  SEL_COMPLETE,
} from './scenario_delegate.mjs';

const responder = (present) => (sel) => present.has(sel);
const ALL = new Set([SEL_RESPONSE, SEL_DATA, SEL_COMPLETE]);

test('fixed_json 产出 response→data→complete 三步（保序）', () => {
  const steps = planDelegateSteps({ type: 'fixed_json', status: 200, body: '{"code":0}' });
  assert.deepEqual(steps.map((s) => s.kind), ['response', 'data', 'complete']);
  assert.equal(steps[0].status, 200);
  assert.equal(steps[1].body, '{"code":0}');
  assert.equal(steps[2].error, null);
});

test('timeout 只产出 complete，带 NSURLErrorTimedOut(-1001)', () => {
  const steps = planDelegateSteps({ type: 'timeout' });
  assert.equal(steps.length, 1);
  assert.equal(steps[0].kind, 'complete');
  assert.deepEqual(steps[0].error, { domain: 'NSURLErrorDomain', code: -1001 });
});

test('fixed_json 缺省 status=200、body 强制字符串化、缺省 headers={}', () => {
  const steps = planDelegateSteps({ type: 'fixed_json', body: 123 });
  assert.equal(steps[0].status, 200);
  assert.deepEqual(steps[0].headers, {});
  assert.equal(steps[1].body, '123');
  assert.equal(typeof steps[1].body, 'string');
});

test('未知/缺省 action 类型按 fixed_json 兜底', () => {
  assert.equal(planDelegateSteps(undefined)[0].kind, 'response');
  assert.equal(planDelegateSteps({})[0].kind, 'response');
});

test('requiredSelectors 去重且保序', () => {
  const steps = [
    { kind: 'response' }, { kind: 'data' }, { kind: 'complete' }, { kind: 'data' },
  ];
  assert.deepEqual(requiredSelectors(steps), [SEL_RESPONSE, SEL_DATA, SEL_COMPLETE]);
});

test('delegate 实现全部 selector → fixed_json 可行、missing 为空', () => {
  const plan = planDelegateShortCircuit({ type: 'fixed_json', body: '{}' }, responder(ALL));
  assert.equal(plan.applicable, true);
  assert.deepEqual(plan.missing, []);
  assert.deepEqual(plan.selectors, [SEL_RESPONSE, SEL_DATA, SEL_COMPLETE]);
});

test('fixed_json 缺 didCompleteWithError → 不可行(fail-closed，避免吊死)', () => {
  const present = new Set([SEL_RESPONSE, SEL_DATA]);
  const plan = planDelegateShortCircuit({ type: 'fixed_json', body: '{}' }, responder(present));
  assert.equal(plan.applicable, false);
  assert.deepEqual(plan.missing, [SEL_COMPLETE]);
});

test('fixed_json 缺 didReceiveData → 不可行(会给 app 残缺响应)', () => {
  const present = new Set([SEL_RESPONSE, SEL_COMPLETE]);
  const plan = planDelegateShortCircuit({ type: 'fixed_json', body: '{}' }, responder(present));
  assert.equal(plan.applicable, false);
  assert.deepEqual(plan.missing, [SEL_DATA]);
});

test('timeout 仅需 didCompleteWithError → 缺 response/data 仍可行(非对称)', () => {
  const present = new Set([SEL_COMPLETE]);
  const plan = planDelegateShortCircuit({ type: 'timeout' }, responder(present));
  assert.equal(plan.applicable, true);
  assert.deepEqual(plan.selectors, [SEL_COMPLETE]);
});

test('timeout 连 didCompleteWithError 都没有 → 不可行', () => {
  const plan = planDelegateShortCircuit({ type: 'timeout' }, responder(new Set()));
  assert.equal(plan.applicable, false);
  assert.deepEqual(plan.missing, [SEL_COMPLETE]);
});
