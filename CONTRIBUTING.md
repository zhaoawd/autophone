# Contributing to Autophone

**English** | [简体中文](CONTRIBUTING.zh-CN.md)

Thank you for your interest in contributing to [Autophone](https://github.com/zhaoawd/autophone). Bug reports, documentation improvements, tests, and code changes are all welcome. This repository is the Single Source of Truth for Autophone Public Core.

## 1. Development Environment

- Python `3.11`–`3.13` and [`uv`](https://docs.astral.sh/uv/)
- Node.js `22+` (required for dashboard Playwright E2E and Frida agent unit tests)
- macOS with Xcode Command Line Tools for live `vphone-cli` VM execution and native macOS Vision OCR (pure Python logic, PostgreSQL Control Plane tests, and package build/install verification also run on Linux)

Install development, platform, and contract dependencies from the lockfile:

```bash
uv sync --locked --group dev --group platform --group contract
# Or via the Makefile shortcut:
make sync
```

## 2. Multi-Package Architecture & Ownership

Autophone maintains a single source tree (`autophone/`) partitioned into five installable distributions at build time:

| Package | Responsibility Boundary |
| --- | --- |
| `autophone-kernel` | JSON Schemas, shared data contracts, artifact manifests, and core primitives |
| `autophone-rig` | `vphone-cli` UDS/lifecycle driver, Rig Agent, `ScreenLock`, and PostgreSQL Control Plane |
| `autophone-domain` | Typed-IR Recipe executor, IPA Package Store, system location, OCR/AX observation, and admission checks |
| `autophone-console` | `autophone` CLI entry point and Web Device Console / dashboard |
| `autophone` | Top-level aggregate meta-package |

When adding new source files:
- Verify their path maps to the intended component in [pkgs/ownership.py](pkgs/ownership.py).
- Respect the **core runtime contract**: deterministic regression runs must **never** call `autophone.application.ai` for live decision-making (AI is restricted to offline authoring, exploration, and post-run diagnosis; generated assets enter as `draft`).
- Typed-IR recipes may only use registered Verbs (`autophone.execution.engine.recipe.ACTIONS`) and registered Operations.

## 3. Local Pre-PR Verification

Before opening a Pull Request, run the fast local gate (lint, generated reference docs drift check, public content & link check, and Frida agent unit tests):

```bash
make check
```

Run `make help` to view all available targets. You can also invoke each verification step directly:

```bash
# 1. Static lint, generated reference docs drift check, public content & Markdown link check
uv run --only-group dev ruff check .
uv run --group platform python scripts/generate_public_docs.py --check
python3 scripts/check_public_content.py

# 2. Frida agent Node.js unit tests (Node.js 22+)
npm --prefix autophone/observation/frida/agent test

# 3. Platform integration suite (supports local PostgreSQL initdb/pg_ctl,
#    automatic Docker postgres:16 fallback, or an external AUTOPHONE_TEST_POSTGRES_DSN)
scripts/test_public_platform.sh

# 4. Web Console Playwright E2E (when changing frontend assets or HTTP routes)
npm ci && npx playwright install chromium && npm run test:dashboard-e2e

# 5. Clean build & archive/hygiene verification for all five wheels and sdists
scripts/build_public_core.sh dist/core
```

If you modify CLI options, Recipe Verbs, JSON Schemas, or HTTP routes, regenerate the code-derived reference docs under `docs/reference/` before committing and note any breaking or deprecated changes in [CHANGELOG.md](CHANGELOG.md#versioning--deprecation-policy):

```bash
uv run --group platform python scripts/generate_public_docs.py --write
# Or: make docs-write
```

## 4. Reporting Issues & Contribution Licensing

- **Bug reports**: Please include commit SHA or version, OS, Python version, deployment mode (single-VM local / Control Plane), minimal reproduction steps, and redacted error summaries.
- **Security vulnerabilities**: Do **not** open a public issue; follow the private reporting process in [SECURITY.md](SECURITY.md).
- **Community standards**: Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Inbound = Outbound licensing**: Under Section 5 (Submission of Contributions) of the [Apache License, Version 2.0](LICENSE), unless you explicitly state otherwise in writing, any contribution intentionally submitted for inclusion in this repository is licensed under the Apache-2.0 license with no additional terms or conditions.
