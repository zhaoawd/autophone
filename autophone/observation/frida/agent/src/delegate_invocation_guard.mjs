// The same delegate event can synchronously pass through several hooked implementations.
// Keep the guard only for the native invocation; later identical chunks are real data.
const active = new Set();
export function guardDelegateCallbacks(kind, callbacks, report) {
  return {
    onEnter(args) {
      const key = [this.threadId, String(args[3]), kind, String(args[4])].join(':');
      if (active.has(key)) {
        const event = { ev: 'delegate_reentry_skipped', kind, task_ptr: String(args[3]) };
        if (report) report(event);
        else if (typeof send === 'function') send(event);
        return;
      }
      this.delegateCaptureKey = key;
      active.add(key);
      try { callbacks.onEnter.call(this, args); }
      catch (error) { active.delete(key); this.delegateCaptureKey = null; throw error; }
    },
    onLeave(retval) {
      if (!this.delegateCaptureKey) return;
      try { callbacks.onLeave?.call(this, retval); }
      finally { active.delete(this.delegateCaptureKey); this.delegateCaptureKey = null; }
    },
  };
}
