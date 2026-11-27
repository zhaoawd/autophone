import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createDelegateBodyAccumulator,
  planDelegateBodyChunk,
  recordDelegateBodyChunk,
} from './delegate_body_accumulator.mjs';

test('delegate accumulator caps exported chunks while retaining original body length', () => {
  const state = createDelegateBodyAccumulator(5);
  const first = planDelegateBodyChunk(state, 3);
  recordDelegateBodyChunk(state, { ...first, bytes: Uint8Array.from([97, 98, 99]).buffer });
  const second = planDelegateBodyChunk(state, 4);
  recordDelegateBodyChunk(state, { ...second, bytes: Uint8Array.from([100, 101]).buffer });

  assert.equal(state.cap, 5);
  assert.equal(state.bodyLength, 7);
  assert.equal(state.capturedLength, 5);
  assert.equal(state.truncated, true);
  assert.equal(state.error, null);
  assert.deepEqual(Array.from(new Uint8Array(state.chunks[0].bytes)), [97, 98, 99]);
  assert.deepEqual(Array.from(new Uint8Array(state.chunks[1].bytes)), [100, 101]);
});

test('delegate accumulator records encoding errors without claiming captured bytes', () => {
  const state = createDelegateBodyAccumulator(5);
  const plan = planDelegateBodyChunk(state, 3);
  recordDelegateBodyChunk(state, { ...plan, captureLength: 0, error: 'copy failed' });

  assert.equal(state.bodyLength, 3);
  assert.equal(state.capturedLength, 0);
  assert.equal(state.error, 'copy failed');
  assert.deepEqual(state.chunks, []);
});
