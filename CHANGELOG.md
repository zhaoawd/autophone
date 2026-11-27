# Changelog

All notable user-visible changes to Autophone Public Core are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
using `v<MAJOR>.<MINOR>.<PATCH>` tags and `YYYY-MM-DD` dates.

> `CHANGELOG.md` tracks the concise version-by-version history. For detailed deployment notes and verification entry points for the current release, see [RELEASE-NOTES.md](RELEASE-NOTES.md).

## Versioning & Deprecation Policy

Autophone treats four surfaces as public compatibility contracts: the `autophone` CLI ([docs/reference/cli.md](docs/reference/cli.md)), HTTP API endpoints ([docs/reference/http-api.md](docs/reference/http-api.md)), registered Recipe Verbs ([docs/reference/verbs.md](docs/reference/verbs.md)), and versioned JSON Schemas ([docs/reference/schemas.md](docs/reference/schemas.md)).

- **Pre-1.0 (`0.x`)**: Minor releases (`0.MINOR.0`) may introduce breaking changes as public interfaces stabilize. Breaking changes are called out explicitly in this changelog with migration notes, and deprecated flags, endpoints, or verbs emit deprecation warnings at least one minor release ahead whenever feasible. Patch releases (`0.MINOR.PATCH`) contain only backward-compatible fixes and security updates.
- **`1.0.0` and later**: Strictly follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). Breaking changes occur only in major releases, and deprecated public interfaces remain supported with warnings for at least one full minor release cycle before removal.

## [Unreleased]

## [0.2.0] - 2026-11-27

### Added

- **Immutable App Symbol contracts & toolchain (`app-symbols.v1`)**: Added `app-symbols.v1.json`, pure-content artifact digest routing (`symbols/artifacts/<digest>.yaml`), external `symbols/claims.json` (`symbol.unique_visibility` evidence without mutating `binding_sha256`), unified load-time `tap_ax(symbol=...)` expansion, L2 build-manifest import and L1 React Native `main.jsbundle` extraction (`autophone app symbols import-l2` / `extract-l1-rn`), and dual-denominator coverage reporting.
- **Multi-effect governance & isolated draft trials**: Upgraded effect evaluation to multi-verb sets with explicit `completeness` (`exhaustive` / `lower_bound` / `unknown`), rejecting `"irreversible"` wildcards on multi-effect symbol actions, hard-blocking incomplete `active` assets, and gating incomplete `draft` trials on explicit CLI confirmation plus `trial_data_isolation: "isolated"`.
- **Round 1.5 AX occlusion hardening (`ax_dump-v9`)**: Added `support/ax-dump/ax_dump.m` (`dumper_version: 9`) and `scripts/build_ax_dump.sh` with `accessibilityViewIsModal` modal-sibling occlusion detection, foreground activation checks, `screens.yaml` `blocking_overlays` pre-tap blocking, and case-wide automatic overlay dismissal suppression for symbol-bound cases.
- **Stage 2 entity binding, post-action readback, and joint verdict**: Added `entity-binding.v1.json`, case-level entity binding (`autophone/assets/entity_binding.py`), post-action controlled fixture/API readback (`autophone/execution/entity_readback.py`), API window request/response entity assertions, and fail-closed UI × API × readback joint verdict evaluation (`autophone/observation/joint_verdict.py`), verified offline and live on iOS 26.1 VM (9/9 matrix).
- **Stage 3 out-of-process `guest_axruntime` AX backend & zero-Frida execution**: Added the standalone zero-injection `AXRuntime` probe (`support/guest-ax-probe/`, `scripts/build_guest_ax_probe.sh`, single `com.apple.accessibility.api` entitlement), `guest_axruntime` integration across `AxSession`, `tap_ax`, `ax_checkpoint` v2, and `variant_evidence` (`ui_structure` vs `inproc_graybox` static separation) with foreground/Control Center/lock-screen gating and zero-VoiceOver enforcement, and true zero-Frida regression execution (skipping Frida readiness/attach when API capture is disabled, defaulting `crash_trap` off without Frida, recording `no_frida_attached` in `app_frida_hygiene` without unnecessary app termination, and supplying lazy `AxSession` for `requires={"ax"}` / `tap_ax` operations in `operation_run.py`); live-verified on both `legacy` iOS `26.1` and `upstream_v1` iOS `27.0.1` (`24A446`) VMs (4/4 cross-process fail-closed gating, zero-Frida `ios-settings-smoke` and `Food Ordering` runs, `voiceover=false`, `Tag: 255 == 0`).
- **Zero-Frida inter-run home reset preconditions**: Added `AppProfile.reset` and a `reset: relaunch` fallback in `RigMaintenance._reset_home_and_probe()` when no `reset_to_home` operation is registered, and tightened `examples/ios-settings-smoke` `ready_markers` to root-screen-only tokens (`tests/test_rig_maintenance_reset.py`).
- **`vphone_mcp` graduated from experimental (`ScreenLock`/Control Plane coordination & `upstream_v1`)**: Added `managed_proxy`, `control_plane`, `local_lock`, and `standalone` (`VPHONE_MCP_REQUIRE_LOCK=1`) coordination modes, strict fail-closed handling when a configured Control Plane is unreachable, `upstream_v1` / `legacy` UDS wire framing and read-only vs mutating tool classification, automated unit & root interop suites (`vphone_mcp/tests/`, `tests/test_vphone_mcp_coordination.py`), and 10/10 live verification checks against a running `upstream_v1` `vphone-cli` VM (iOS `27.0.1 / 24A446`).
- **Public Entry Point extension guide & minimal plugin template**: Added bilingual guides ([docs/guides/extensions.md](docs/guides/extensions.md), [docs/guides/extensions.en.md](docs/guides/extensions.en.md)), runnable third-party starter package (`examples/plugin-template/`), and protocol verification tests (`tests/test_extension_entrypoints.py`) covering `autophone.run_admission`, `autophone.executor`, and `autophone.device_session`.
- **Bilingual English onboarding, feature guides, Environment Matrix & Guest AX packaging docs**: Added English onboarding guides (`docs/getting-started/quickstart.en.md`, `faq.en.md`), 5 English feature guides (`docs/guides/{authoring-cases,device-console,location,multi-vm-control-plane,packages-and-recording}.en.md`) with bilingual switchers, bilingual Environment & Verification Matrix (`docs/getting-started/environment-matrix.{md,en.md}`), and wheel/sdist Guest AX probe build/configuration instructions (`scripts/build_guest_ax_probe.sh` / `AUTOPHONE_GUEST_AX_PROBE`, `scripts/build_ax_dump.sh` / `AUTOPHONE_AX_DYLIB`) in [docs/public-core.md](docs/public-core.md).

### Changed

- **`guest_axruntime` promoted to default AX backend (`DEFAULT_AX_BACKEND`)**: Switched default `capabilities.ax.backend` in `app-profile.v1.json`, `autophone/assets/apps.py`, and `autophone/device/ax_session.py` from `frida_inproc` to `guest_axruntime` after completing the 20/20 multi-app zero-Frida soak and inter-run reset preconditions; profiles omitting `backend` now execute zero-Frida by default (`crash_trap` default-off), while `frida_inproc` remains explicitly supported (`capabilities.ax.backend: frida_inproc` or `borrowed_lifetime`) for in-process gray-box probes (`api_capture`, `core_location_authorization`, `modal_sibling`) with strict cross-backend claim/comparison isolation and startup fail-closed validation on unknown backends.
- **`autophone-kernel` decoupled from `autophone-domain`**: Removed all upper-layer imports from `autophone/assets/{app_symbols,symbol_tools,entity_binding}.py` via pure-kernel `_package_manifest.py`, `api_assertions.py`, `effect_markers.py`, and domain hook registration (`autophone/execution/_entity_binding_hooks.py`), enforcing `0` layering violations across all 47 kernel modules in `tests/test_kernel_layering.py` and `scripts/verify_core_archives.py` and verifying standalone `autophone-kernel` wheel imports.
- **Optional PostgreSQL driver via `autophone[postgres]` & complete example bundling**: Moved `psycopg[binary]` out of base `autophone-rig` dependencies into the optional `[postgres]` extra with lazy fail-closed imports (`autophone/control/_psycopg.py`) so single-VM local mode and core wheel/sdist installs require zero PostgreSQL drivers; updated `scripts/build_public_core.sh` to copy the complete `examples/food-ordering` asset tree into `dist/core` and extended `verify_core_archives.py` / `verify_core_install.py` to enforce zero base `psycopg` dependencies.
- **Strict `tap_ax` targeting, v2 `build_marker` identity confirmation & promotion freshness**: Extended `tap_ax` with `screen_id` single-frame pre-checks and `require_unique` zero-dispatch failure (`ambiguous_match` / `locator_missing` / `screen_mismatch`), updated `operation-ax-checkpoint.v2.json` to accept `build-identity.v2.json` and `ax_backend`, added byte-verified v2 Package Store `build_marker` confirmation in `build_identity.py` / `run_execution.py` so confirmed live runs safely record `symbol.unique_visibility` claims while failing closed on missing or mismatched markers, and tightened `promotion.py` globally to mark legacy digest-less probes, rebuilt same-version IPAs, or symbol `binding_sha256` drift as `discrimination_stale`.

### Fixed

- **iOS `27.0.1` locked-screen geometry handling in `guest_axruntime`**: Updated `_resolve_screen_geometry()` in `autophone/device/guest_ax_probe.py` to fall back to UDS `capabilities.screen` point geometry when `foreground_active=False` (`nodes=[]`), preserving `foreground_occlusion_reason="screen_locked"` on iOS `27.0.1` locked/sleeping SpringBoard frames while keeping active-foreground geometry mismatch checks fail-closed (`tests/test_stage3_guest_axruntime.py`).
- **Deterministic `vphone_mcp` half-close test probe**: Added configurable `eof_probe_timeout` in `vphone_mcp/tests/test_upstream_v1.py` to eliminate the server/client scheduling race in `test_upstream_v1_no_shut_wr_while_legacy_uses_shut_wr`.

## [0.1.0] - 2026-11-25

Initial public release of Autophone Public Core under the Apache-2.0 license (with `NOTICE` attributing upstream [`vphone-cli`](https://github.com/Lakr233/vphone-cli)).

### Added

- **Modular 5-package distribution**: Published `autophone`, `autophone-kernel`, `autophone-rig`, `autophone-domain`, and `autophone-console` from a single source tree with explicit ownership boundaries and clean wheel/sdist builds.
- **Upstream `vphone-cli` VM lifecycle & Control Plane**: Added `upstream_v1` UDS/vsock device control, `autophone rig init` target generation, `autophone rig up` (zero-PostgreSQL single-VM local mode and multi-VM PostgreSQL Control Plane with `--ephemeral-db` and per-Rig `ScreenLock` serialization), and `python -m autophone.rig.emit_target`.
- **Web Device Console & IPA Package Store**: Added the core Web Console, offline interactive console demo (`/embed/device-console?demo=1`), authenticated online embed, Build ID–tracked IPA registration and UDS installation (`apps.install` with post-install verification), interactive recording, draft operation saving, and HTML evidence reports.
- **Deterministic Typed-IR execution & Vision OCR**: Added statically validated Typed-IR Recipe execution with Vision OCR (compiled on demand into the macOS user cache for both source and wheel installs, or configured via `AUTOPHONE_OCR_BIN`) and Accessibility (AX) semantic targeting.
- **Unified location control**: Added system CoreLocation (`system_core_location`) and App CoreLocation spawn-gate (`app_core_location_spawn`) targets, built-in route replay fixtures, per-Rig dashboard/CLI controls (`autophone location current|set|route|stop`), guest coordinate readback, and `location_services_off` pre-source guarding when guest Location Services are disabled.
- **Standalone example assets & experimental MCP server**: Included the IPA-free `examples/ios-settings-smoke` suite (R1 positive / R2 negative), the `examples/food-ordering` third-party App template, and the standalone experimental `vphone_mcp/` MCP server.
- **Pre-install probe & verification tooling**: Added the read-only SpringBoard memory-region probe source and macOS build helper (`support/springboard-regions/`, `scripts/build_guest_helpers.sh`), offline platform test runner (`scripts/test_public_platform.sh`), Playwright dashboard E2E suite, isolated core install checks (`scripts/verify_core_install.py`), and code-generated reference documentation (`docs/reference/`).

### Changed

- Set supported Python range to `3.11`–`3.13` and upgraded Pillow requirement to `>=13.0.0` (pinned to `13.0.0` in `uv.lock`).
- Standardized the artifact manifest governance field to `share_policy` and located single-Rig target emission under `autophone.rig.emit_target`.
- Required explicit case, App, or Bundle ID arguments across execution, recording, and observation entry points, with optional brand configuration via `AUTOPHONE_STORE_BRANDS`.

### Security

- Enforced fail-closed pre-install SpringBoard memory-region inspection to reject IPA transfers if Tag 255 artifacts are detected or memory regions cannot be read.
- Hardened dashboard and embed credential hygiene: access logs omit query strings, embedded consoles disable caching and Referrer leakage and strip one-time launch tokens from the browser URL immediately after exchange, and legacy SSH transport requires an explicit non-empty `AUTOPHONE_SSH_PASSWORD`.
- Updated Pillow to `13.0.0` to incorporate upstream security fixes and added bilingual vulnerability reporting guidance and SLA targets in [SECURITY.md](SECURITY.md).

[Unreleased]: https://github.com/zhaoawd/autophone/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/zhaoawd/autophone/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zhaoawd/autophone/releases/tag/v0.1.0
