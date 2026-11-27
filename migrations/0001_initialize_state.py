"""Initialize the split host-state root and import an optional legacy layout."""

STATE_SCHEMA = 1
MIGRATION_ID = "0001_initialize_state"
REVERSIBLE = True


def forward(context):
    context.state_root.mkdir(parents=True, exist_ok=True)
    context.import_legacy_state()


def backward(context):
    # Migration 0001 only copies missing legacy files.  Snapshot restoration is
    # the authoritative transactional rollback path; backward is intentionally
    # a no-op so it never deletes user state created after the migration.
    return None
