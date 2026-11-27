export function createDelegateBodyAccumulator(cap) {
  return {
    cap: Math.max(0, Number(cap) || 0),
    bodyLength: 0,
    capturedLength: 0,
    chunks: [],
    truncated: false,
    error: null,
  };
}

export function planDelegateBodyChunk(state, chunkLength) {
  const actualLength = Math.max(0, Number(chunkLength) || 0);
  const remaining = Math.max(0, state.cap - state.capturedLength);
  return {
    actualLength,
    captureLength: Math.min(actualLength, remaining),
  };
}

export function recordDelegateBodyChunk(
  state,
  { actualLength, captureLength, bytes = null, error = null },
) {
  state.bodyLength += actualLength;
  if (error && state.error === null) state.error = String(error);
  if (captureLength > 0 && bytes) {
    state.chunks.push({ bytes, length: captureLength });
    state.capturedLength += captureLength;
  }
  if (captureLength < actualLength) state.truncated = true;
  return state;
}
