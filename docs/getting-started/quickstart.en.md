# Quickstart

[简体中文](quickstart.md) | **English**

This guide provides two progressive onboarding paths: a **Zero-VM Offline Walkthrough** (no virtual machine required) and a **Single-VM Live Regression** on one `vphone` instance (no third-party IPA or external PostgreSQL required).

## Requirements

- **macOS (Apple Silicon)** for running `vphone-cli` VMs and native macOS Vision OCR; pure-Python host logic, PostgreSQL Control Plane tests, and documentation checks also run on Linux CI
- **Python `3.11`–`3.13`** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js `22+`** (optional, for Playwright dashboard E2E tests)

See the [Environment & Verification Matrix](environment-matrix.en.md) for the full list of repository-verified host, Python, iOS, Control Plane, and AX backend configurations.

## Step 0: Zero-VM Offline Walkthrough

Install the core platform and test dependencies from the repository root:

```bash
uv sync --locked --group platform --group contract
```

### 1. Try the Offline Interactive Device Console Demo

Start the local dashboard and open the built-in demo page `/embed/device-console?demo=1` in your browser:

```bash
export AUTOPHONE_STATE_DIR="$PWD/.tmp-state"
uv run --group platform autophone serve --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000/embed/device-console?demo=1` to preview remote touch input, clipboard text dispatch, and session status interactions without booting a VM.

### 2. Run Offline Recipe Validation & Synthetic OCR Assertions

Validate the case registry statically and run positive/negative synthetic OCR assertions without connecting a live device:

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone app show ios_settings_smoke
uv run --group platform autophone app case lint ios_settings_smoke
uv run --group platform python scripts/verify_core_install.py examples/ios-settings-smoke
```

## Step 1: Single-VM IPA-Free Live Regression

Live regression requires macOS with Xcode Command Line Tools. On first OCR use, Autophone compiles the bundled Swift Vision OCR helper into your user cache (or you can point `AUTOPHONE_OCR_BIN` to a prebuilt binary). The smoke suite targets the English Settings root screen. You can verify native host OCR against a synthetic image first:

```bash
uv run python -m autophone.device.ocr_target probe
```

Prepare `VPhone.bundle` and a schema v2 VM (for example `rig-a`, stored by default under `~/.vphone/machines/rig-a`) following the [`vphone-cli` upstream documentation](https://lakr233.github.io/vphone-cli/).

### 1. Generate a `DeviceTarget` Configuration

```bash
uv run --group platform autophone rig init rig-a \
  --bundle /absolute/path/to/VPhone.bundle
```

This command automatically derives `vm_dir`, `vphone.sock`, runtime/log/temp directories, reads the expected `UDID` from `udid-prediction.txt`, assigns conflict-free host ports, and writes `~/.vphone/machines/rig-a/autophone/target.json`.

### 2. Start the Single VM & Web Console (No PostgreSQL Required)

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone rig up \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json"
```

When given a single `--target` and `AUTOPHONE_DATABASE_URL` is unset, `autophone rig up` automatically boots the VM and dashboard in **single-VM local mode** and prints `Dashboard: http://127.0.0.1:8000` once ready.

### 3. Run the IPA-Free Smoke Suite & Inspect HTML Evidence Reports

Open `http://127.0.0.1:8000` and expand the regression / IPA / recording panel:

1. Select `iOS Settings Smoke` (built on system `com.apple.Preferences` — no third-party IPA or SpringBoard probe compilation required).
2. Run positive case `R1` on the selected Rig and observe a `passed` verdict (`General` found), then run negative control `R2` and observe a deterministic `failed` verdict (`AUTOPHONE_NEGATIVE_SENTINEL_404` absent). *(Tip: if an idle VM screen turned off and the first run reports `app_ui_unavailable`, send two `Home` presses from the Device Console to wake and unlock to the Home screen, then retry.)*
3. Open the self-contained **HTML Report** from the run history to inspect step-by-step screenshots and assertion results.

You can run the same suite from another terminal via CLI:

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone --app ios_settings_smoke --device rig-a run --ids R1 R2
```

Against a local single-Rig dashboard, you can also run `python3 scripts/verify_settings_run.py --port 8000 --rig rig-a` repeatedly to submit `R1` and `R2`, check verdicts, and read back both HTML reports.

## Next Steps

- Verified host/Python/iOS/AX configurations: [Environment & Verification Matrix](environment-matrix.en.md)
- Environment trade-offs & capability boundaries: [Testing Environment FAQ](faq.en.md)
- Writing custom App Profiles, Cases, and Typed-IR Recipes: [Authoring Cases & Recipes](../guides/authoring-cases.md)
- Extending Autophone via entry points: [Public Entry Point Extension Guide](../guides/extensions.en.md)
- Complete action catalog: [Recipe Verb Reference](../reference/verbs.md)
- Multiple VMs with PostgreSQL Control Plane: [Multi-VM Control Plane Deployment](../guides/multi-vm-control-plane.md)
- Standalone wheel/sdist install & optional guest helpers: [Core Distribution & Standalone Install](../public-core.md)
