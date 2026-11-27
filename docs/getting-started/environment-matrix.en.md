# Environment & Verification Matrix

[简体中文](environment-matrix.md) | **English**

This page documents **only configurations verified in this repository** across host platforms, Python versions, `uv` dependency groups, `vphone-cli` protocols, guest iOS builds, deployment topologies, and Accessibility (AX) backends. Any OS or iOS version not listed below should be treated as **unverified**.

## 1. Host OS & Architecture

| Host Platform | Supported Scope | Verification Basis |
| --- | --- | --- |
| **macOS (Apple Silicon / `arm64`)** | Live `vphone-cli` iOS VMs (`Virtualization.framework`), native Swift Vision OCR, Xcode `iphoneos` SDK compilation of optional guest Mach-O helpers (`scripts/build_guest_helpers.sh`, `scripts/build_ax_dump.sh`, `scripts/build_guest_ax_probe.sh`), and all offline suites | Native Vision OCR verified for both source checkout and wheel installs on CI `macos-latest` (`.github/workflows/ci.yml`); live VM regressions and soak runs verified on Apple Silicon macOS |
| **Linux (`ubuntu-latest` CI)** | Pure-Python logic, static lint (`ruff`), generated reference docs drift check, public content hygiene checks, PostgreSQL Control Plane platform tests, wheel/sdist build & isolated install checks, Playwright mock-API dashboard E2E, and Node.js Frida agent unit tests | All non-VM CI jobs in `.github/workflows/ci.yml` run on `ubuntu-latest`; **cannot** run `vphone-cli` VMs or macOS Vision OCR |

## 2. Python, `uv` Dependency Groups & Toolchains

- **Python versions**: Supported and CI-verified on **`3.11`**, **`3.12`**, and **`3.13`** (`requires-python = ">=3.11,<3.14"`). The CI `build-and-install` matrix tests clean virtual-environment installs of all 5 wheels and 5 sdists across `3.11`, `3.12`, and `3.13`; lint, platform, OCR, and E2E jobs run on `3.13`. Python `<3.11` and `3.14+` are unsupported.
- **Lockfile & groups**: Managed via [`uv`](https://docs.astral.sh/uv/) and `uv.lock` (core requires `Pillow>=13.0.0`, pinned to `13.0.0` in `uv.lock`):

| `uv` Group / Install Extra | Packages | Purpose |
| --- | --- | --- |
| Core (`autophone` + 4 components) | `autophone-kernel`, `autophone-rig`, `autophone-domain`, `autophone-console` | CLI, Typed-IR static validation, JSON Schema contracts, UDS transport, Web Console, and single-VM local execution (**zero `psycopg` dependency**) |
| `--extra postgres` / `[postgres]` | `psycopg[binary]>=3.2` | **Optional PostgreSQL driver**; required only for multi-VM Control Plane (`AUTOPHONE_DATABASE_URL`) or `rig up --ephemeral-db`; PostgreSQL code paths fail closed with actionable install hints when omitted |
| `--group platform` / `[platform]` | `fastapi`, `uvicorn`, `websockets`, `httpx2`, `croniter`, `psycopg[binary]` | Full source-tree platform development & test group (including `scripts/test_public_platform.sh`); standalone wheel/sdist core installs keep `psycopg` strictly in the optional `[postgres]` extra |
| `--group contract` / `[contract]` | `pytest`, `pytest-xdist` | Offline test suites and `scripts/test_public_platform.sh` |
| `--group frida` / `[frida]` | Pinned `frida==17.16.1`, `frida-tools==14.10.4`, `ddddocr>=1.6.1` | Required only when opting into `frida_inproc` AX, Frida `api_capture`, `crash_trap`, or gray-box probes; **not needed** for OCR-only or default zero-Frida `guest_axruntime` UI runs |
| `--group dev` (source dev only) | `ruff>=0.16.6` | Static lint gate (`uv run --only-group dev ruff check .`); omitted from wheel/sdist runtime dependencies |

- **Optional browser/agent test toolchain**: **Node.js `22+`** (CI-verified on Node `22` + Playwright Chromium) for `npm run test:dashboard-e2e` and `npm --prefix autophone/observation/frida/agent test`.

## 3. `vphone-cli` Protocols & Verified Guest iOS Coverage

| Dimension | Configuration / Version | Status | Notes & Evidence |
| --- | --- | --- | --- |
| **`vphone-cli` protocol (recommended)** | `upstream_v1` (upstream [`Lakr233/vphone-cli`](https://github.com/Lakr233/vphone-cli) schema v2 VM layout) | Recommended Public Core path | Configured via `autophone rig init <rig-id> --bundle /path/to/VPhone.bundle` (requires absolute `core_bundle`), uses UDS/vsock (`vphone.sock`) for control, system location, and UDS `apps.install`; `type` writes guest `UIPasteboard` (`timeout >= 3s`); pair with `scripts/build_guest_helpers.sh` when guest lacks `procexp` |
| **`vphone-cli` protocol (compatibility)** | `legacy` (legacy checkout layout, e.g., `vm-2607` validation VM) | Compatibility path | `protocol: "legacy"`; legacy SSH install/probe paths require an explicit non-empty `AUTOPHONE_SSH_PASSWORD`. Stage 2 (9/9) and Stage 3 / Phase 2B (20/20) live validations were executed on `vm-2607` |
| **Verified Guest iOS version (regression & AX)** | **iOS `26.1` (build `23B85`)** | Verified live end-to-end | Validated on `vm-2607` across `Food Ordering` (React Native), system `Settings` (`com.apple.Preferences`, SwiftUI), and `SpringBoard` Control Center (Round 1, Round 1.5 `ax_dump-v9` 5/5, Stage 2 joint verdict 9/9, and Stage 3 `guest_axruntime` 20/20 zero-Frida soak; see [Validation Report](../concepts/app-symbols-v1-validation-report.md)) |
| **Verified Guest iOS version (`upstream_v1` + `vphone_mcp` + `guest_axruntime`)** | **iOS `27.0.1` (build `24A446`)** | Verified live (`vphone_mcp` 10/10 & `guest_axruntime` end-to-end) | Verified on a running `upstream_v1` `vphone-cli` VM across: (1) `vphone_mcp` 10/10 checks (`wait_ready`, `get_capabilities`, JPEG `screenshot` geometry, `list_apps`, location round-trip, `local_lock` lifecycle/contention/quarantine, and Control Plane unreachable fail-closed + live reservation round-trip); (2) default zero-Frida `guest_axruntime` end-to-end verification (`guest_ax_probe` auto-deploy/cache reuse, `Settings` `12/14` attr `5019` `axIdentifier` with 10× guest P50 `102.11 ms` / `AxSession.refresh()` P50 `369.80 ms`, 4/4 fail-closed gating across live SpringBoard `"Trust This Computer?"` modal / Home / Control Center / Lock Screen, `ios-settings-smoke` `R1`/`R2`/`R3_AX` ×2 and `Food Ordering` zero-Frida `tap_ax` navigation, `SpringBoard` `autophone-springboard-regions` `0 / 1,027` `Tag: 255`, and `voiceover=false`; see [Validation Report §7](../concepts/app-symbols-v1-validation-report.md)) |
| **Other iOS versions** | Any iOS build not listed above | Unverified | Guest helper build scripts pass `-mios-version-min=16.0` (`arm64 + arm64e`), but only the two guest iOS builds above have live verification evidence in this repository; qualify other VM images before relying on them in regression |

## 4. Deployment Topologies

| Topology | Entry Point | Required Services | Isolation & Fail-Closed Guarantees |
| --- | --- | --- | --- |
| **Zero-VM Offline Mode** | `autophone serve` (`/embed/device-console?demo=1`), `autophone app case lint`, `scripts/verify_core_install.py` | Python `3.11`–`3.13` + core packages (or source `--group platform`) | No VM, no PostgreSQL / `psycopg`, no Frida required |
| **Single-VM Local Mode** | `autophone rig up --target <target.json>` (single `--target`, `AUTOPHONE_DATABASE_URL` unset) | Apple Silicon macOS + single `vphone` VM + Xcode CLT | No PostgreSQL server and no `psycopg` installation required in wheel/sdist installs; single-Rig `ScreenLock` serialization |
| **Multi-VM Control Plane Mode** | `autophone rig up --target rig-a.json --target rig-b.json` (with `AUTOPHONE_DATABASE_URL` or `--ephemeral-db`) | **PostgreSQL `16`** + optional **`autophone[postgres]`** (CI & `scripts/test_public_platform.sh` verified) + isolated per-Rig `DeviceTarget` configs | Dedicated `autophone-rig-agent` & `ScreenLock` per Rig; missing `psycopg` or unreachable Control Plane fails closed (CLI exits `2`, HTTP returns `503`), never falling back to direct UDS |
| **[`vphone_mcp/`](https://github.com/zhaoawd/autophone/blob/main/vphone_mcp/README.md) MCP Server** | `uv --directory vphone_mcp run vphone-mcp` (`AUTOPHONE_VPHONE_PROTOCOL=upstream_v1\|legacy`) | Standalone `vphone_mcp/` package | Supports `control_plane`, `local_lock`, and `managed_proxy` `ScreenLock` coordination (fails closed when Control Plane is configured and unreachable; opt-in `VPHONE_MCP_REQUIRE_LOCK=1` guard for `standalone`) plus `upstream_v1` wire compatibility; graduated from experimental after passing 10/10 live `upstream_v1` checks (iOS `27.0.1 / 24A446`) |

## 5. Accessibility (AX) Backends

The active `ax_backend` is frozen at run startup with strict cross-backend isolation (`claims.json`, `ax_comparison`) and zero mid-run fallback:

| AX Backend (`capabilities.ax.backend`) | Current Role | Guest Helper & Env Override | Frida Requirement | Verified Behavior & Boundaries |
| --- | --- | --- | --- | --- |
| **`guest_axruntime`** | **Default AX backend** (`DEFAULT_AX_BACKEND`; zero-Frida default for pure-UI & anti-injection targets) | `support/guest_ax_probe` (built via `scripts/build_guest_ax_probe.sh` / `AUTOPHONE_GUEST_AX_PROBE`, single minimal entitlement `com.apple.accessibility.api = true`) | **Zero Frida required** (also verified 9/9 coexisting with Frida `api_capture`) | Verified on iOS `26.1` (`23B85`, `legacy`) across 20/20 multi-app zero-Frida soak (`46/46` probes OK, P50 `267.14 ms`, `0` `Tag: 255` regions, `0` VoiceOver activations) and on iOS `27.0.1` (`24A446`, `upstream_v1`) end-to-end zero-Frida (`Settings` guest P50 `102.11 ms` / `refresh()` P50 `369.80 ms`, 4/4 fail-closed gating for live system modal / Home / Control Center / Lock Screen, `ios-settings-smoke` + `Food Ordering` `tap_ax` PASS, `SpringBoard` `0 / 1,027` `Tag: 255`, `voiceover=false`); emits `ui_structure` only (no in-process gray-box fields) |
| **`frida_inproc`** | **Explicit opt-in** (`capabilities.ax.backend: frida_inproc`) & in-process gray-box channel | `support/ax_dump.dylib` (`dumper_version: 9`, built via `scripts/build_ax_dump.sh` / `AUTOPHONE_AX_DYLIB`) | Requires `--group frida` (`frida==17.16.1`) + guest `frida-server` tunnel (`27042`) | Supports in-process `variant_observation` (`core_location_authorization`), `modal_sibling` diagnostics, and `api_capture`; leaves `Tag: 255` pages in target app (cleared on post-run app restart; never attach to `SpringBoard`) |

> For standalone wheel/sdist installs, see [Core Distribution & Standalone Install](../public-core.md#可选-guest-ax-探针wheel--sdist-安装) for building and configuring all three optional guest helper binaries (`scripts/build_guest_helpers.sh`, `scripts/build_guest_ax_probe.sh`, and `scripts/build_ax_dump.sh`).

## 6. Offline Verification Commands

```bash
# 1. Static syntax & lint gate
uv run --only-group dev ruff check .

# 2. Generated reference docs drift & public content/link checks
uv run --group platform python scripts/generate_public_docs.py --check && python3 scripts/check_public_content.py

# 3. Public platform test suite (ephemeral Unix-socket PostgreSQL or Docker postgres:16)
scripts/test_public_platform.sh

# 4. Dashboard Playwright E2E (mock API)
npm run test:dashboard-e2e

# 5. Native macOS Vision OCR self-check (macOS only)
uv run python -m autophone.device.ocr_target probe
```

See also: [Quickstart](quickstart.en.md), [Testing Environment FAQ](faq.en.md), and [Core Distribution & Standalone Install](../public-core.md).
