"""Recording operation names shared by transport and execution."""
RECORDING_SESSION_OPS = frozenset({
    "record_start", "record_view", "record_frame", "record_heartbeat", "record_event",
    "record_label", "record_finish", "record_cancel", "record_claim", "record_desktop_cancel",
})
