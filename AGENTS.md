# Autophone Public Core — Agent Guide

Autophone is an end-to-end iOS VM automation, device control plane, and deterministic regression platform built on `vphone-cli` (macOS, Python 3.11–3.13).

## Quick Environment Setup

- Install dependencies from source: `uv sync --locked --group platform --group contract` (add `--group frida` for Frida/OCR probes).
- Single upstream VM bring-up:
  - Generate target config: `uv run --group platform autophone rig init rig-a --bundle /path/to/VPhone.bundle`
  - Start VM + web console (single-VM local mode, no PostgreSQL required):
    `uv run --group platform autophone rig up --target ~/.vphone/machines/rig-a/autophone/target.json`
- Multi-VM Control Plane: set `AUTOPHONE_DATABASE_URL` and pass multiple `--target` arguments to `autophone rig up`.
- Web dashboard only: `uv run --group platform autophone serve --host 127.0.0.1 --port 8000` (configure `AUTOPHONE_AUTH` before binding non-loopback addresses).
- MCP server for AI clients (`vphone_mcp/`): run with `uv --directory vphone_mcp run vphone-mcp`.

## Task-Specific Skills & Docs

Read only the references needed for your task:

| Task | Entry Point |
| --- | --- |
| Quickstart & FAQ | [docs/getting-started/quickstart.md](docs/getting-started/quickstart.md), [docs/getting-started/faq.md](docs/getting-started/faq.md) |
| VM bring-up, UDS & device interface | [vm-bringup](.claude/skills/vm-bringup/SKILL.md), [Multi-VM setup](docs/guides/multi-vm-control-plane.md) |
| Package store & IPA build tracing | [package-store](.claude/skills/package-store/SKILL.md), [docs/guides/packages-and-recording.md](docs/guides/packages-and-recording.md) |
| Case & recipe authoring | [case-registry](.claude/skills/case-registry/SKILL.md), [docs/guides/authoring-cases.md](docs/guides/authoring-cases.md) |
| Web dashboard & device console | [web-dashboard](.claude/skills/web-dashboard/SKILL.md), [docs/guides/device-console.md](docs/guides/device-console.md) |
| AI diagnostics & input governance | [ai-capabilities](.claude/skills/ai-capabilities/SKILL.md), [Runtime contract](docs/concepts/runtime-contract.md) |
| Auto-generated references (verbs, CLI, schemas, HTTP API) | [docs/reference/verbs.md](docs/reference/verbs.md), [docs/reference/cli.md](docs/reference/cli.md), [docs/reference/schemas.md](docs/reference/schemas.md), [docs/reference/http-api.md](docs/reference/http-api.md) |

## Core Invariants

- Deterministic regression runs MUST NOT call `autophone.application.ai` for live decision-making. AI is restricted to authoring, exploration, and post-run diagnosis; generated assets enter as `draft`.
- Typed-IR recipes only use registered verbs (`autophone.execution.engine.recipe.ACTIONS`) and registered Operations. Static schema, admission, and verdict checks are never replaced by prose claims.
- Device execution respects Control Plane routing and per-Rig `ScreenLock` serialization. When a Control Plane DSN is configured and unreachable, fail closed instead of falling back to direct socket access.
- Never overwrite frozen Build Records or historical run evidence; automated installs always specify `--build-id`.
- `type` writes to the guest clipboard (`timeout >= 3s`); never `SIGSTOP` `vphoned`.

## Verification Commands

- Lint: `uv run --only-group dev ruff check .`
- Docs drift & public content check: `uv run --group platform python scripts/generate_public_docs.py --check && python3 scripts/check_public_content.py`
- Public platform tests (ephemeral Unix-socket PostgreSQL, no live VM): `scripts/test_public_platform.sh`
- Dashboard Playwright E2E (mock API): `npm run test:dashboard-e2e`
- Isolated wheel/sdist build & install check: `scripts/build_public_core.sh dist/core` + `scripts/verify_core_install.py`
