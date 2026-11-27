import test from 'node:test';
import assert from 'node:assert/strict';
import { httpResponseStatus } from './http_response_status.mjs';

for (const code of [200, 204, 404, 503]) {
  test(`capture serializes bridged HTTP ${code} as a JSON number`, () => {
    const int64 = { toString: () => String(code), toJSON: () => String(code) };
    assert.equal(typeof JSON.parse(JSON.stringify({ status: int64 })).status, 'string');
    const response = { statusCode: () => int64 };
    assert.equal(JSON.parse(JSON.stringify({ status: httpResponseStatus(response) })).status, code);
  });
}

test('capture retains native numbers and rejects invalid HTTP status values', () => {
  assert.equal(httpResponseStatus({ statusCode: () => 201 }), 201);
  for (const value of [null, undefined, false, 0, 99, 600, 200.5, NaN, Infinity, 'invalid']) {
    assert.equal(httpResponseStatus({ statusCode: () => value }), null);
  }
});
