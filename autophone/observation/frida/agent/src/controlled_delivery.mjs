// Local delivery protocol. Dependencies own native lifetimes and callback invocation.
export function deliverLocal(io) {
  const state = { pending: true };
  let acknowledged = false, disposition, responseReturned = false, released = false;
  function release() {
    if (!released) { released = true; io.release(); }
  }
  function fail() {
    state.pending = false;
    io.emit('failed', { code: 'controlled_callback_failed' });
    release();
  }
  function finish(data, response) {
    if (!state.pending) return;
    try {
      const bytes = io.readData(data);
      if (!bytes || typeof bytes.b64 !== 'string' || !Number.isInteger(bytes.len)
          || bytes.len < 0 || bytes.truncated || bytes.err) throw Error('body');
      io.emit('effective_body', { body_b64: bytes.b64, body_len: bytes.len,
        body_truncated: false, status: 200 });
      if (io.via === 'completion') io.completion(data, response);
      else { io.data(data); io.complete(); }
      io.success();
      state.pending = false;
      io.emit('completed');
      release();
    } catch (_) { fail(); }
  }
  io.emit('started');
  try {
    const response = io.makeResponse(), data = io.makeData();
    if (!response || !data) throw Error('synthesis');
    if (io.via === 'completion') finish(data, response);
    else {
      io.response(response, value => {
        if (acknowledged) { fail(); return; }
        acknowledged = true;
        disposition = Number(value);
        io.emit('response_disposition', { disposition });
        if (disposition !== 1) { fail(); return; }
        if (responseReturned) finish(data, response);
      });
      responseReturned = true;
      if (acknowledged && disposition === 1) finish(data, response);
    }
  } catch (_) { fail(); }
  return state;
}
