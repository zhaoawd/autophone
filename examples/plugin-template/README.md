# Autophone Plugin Template

Minimal, runnable third-party plugin package demonstrating Autophone's three core extension entry points:

- `autophone.run_admission` (`autophone_plugin_template.admission:validate_run_admission`)
- `autophone.executor` (`autophone_plugin_template.executor:create_executor`)
- `autophone.device_session` (`autophone_plugin_template.device_session:create_device_session_ops`)

For complete protocol contracts, loading precedence, and fail-closed guarantees, see the Extension Guide:
- [Public Entry Point Extension Guide (English)](../../docs/guides/extensions.en.md)
- [公开 Entry Point 扩展指南（简体中文）](../../docs/guides/extensions.md)

## Package Layout

```text
examples/plugin-template/
├── pyproject.toml
├── README.md
└── src/autophone_plugin_template/
    ├── __init__.py
    ├── admission.py       # autophone.run_admission
    ├── executor.py        # autophone.executor
    └── device_session.py  # autophone.device_session
```

## Important: Coexistence with `autophone-domain`

`autophone.run_admission`, `autophone.executor`, and `autophone.device_session` all enforce **exact cardinality = 1** in `autophone.rig.plugins` and fail closed if 0 or 2+ providers are installed in the same Python environment.

1. **Minimal `autophone-rig`-only deployment (without `autophone-domain`)**:
   Install this package directly (`pip install -e examples/plugin-template`). Its `[project.entry-points.*]` declarations will be discovered automatically by `autophone.rig.plugins`.
2. **Alongside `autophone-domain` in the same environment**:
   Remove or comment out the overlapping `[project.entry-points.*]` tables in `pyproject.toml` before installing, and wire the factories explicitly on `autophone-rig-agent`:

```bash
autophone-rig-agent \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
  --executor-factory autophone_plugin_template.executor:create_executor \
  --device-session-factory autophone_plugin_template.device_session:create_device_session_ops
```

Or via environment variables:

```bash
export AUTOPHONE_AGENT_EXECUTOR_FACTORY="autophone_plugin_template.executor:create_executor"
export AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY="autophone_plugin_template.device_session:create_device_session_ops"
```

## Verification

The repository verifies this template directly against Autophone's real entry-point loaders, `ControlPlane`, `RigAgent`, and `DeviceSessionAgent` in [tests/test_extension_entrypoints.py](../../tests/test_extension_entrypoints.py):

```bash
uv run --group platform --group contract pytest tests/test_extension_entrypoints.py
```
