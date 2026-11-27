# IPA Package Store & Web Recording Replay

[简体中文](packages-and-recording.md) | **English**

Autophone uses the host-side Package Store as the single source of truth for IPA builds, combining **Version Slots + immutable Build Records + per-Target install receipts** so every test run traces back to the exact binary under test.

## 1. Register an App Profile

First register the target application inside a writable App asset directory, and keep the same `AUTOPHONE_APP_ASSET_DIR` when running the dashboard or CLI:

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/app-assets"
autophone app add example \
  --bundle-id com.example.app \
  --display-name "Example App"
```

## 2. Store IPAs & Install by Exact Build ID

- **Version Slot**: Every `CFBundleShortVersionString` maps to a stable Version Slot. Multiple builds sharing the same version string are stored as separate immutable Build Records within that slot, each with a unique `build_id`, SHA-256 digest, and optional source commit identity.
- **Bundle ID Verification**: On ingest, Autophone parses `Info.plist` inside the IPA and rejects the upload if its bundle ID does not match the App Profile.
- **Pre-Install SpringBoard Check**: Before installing an IPA, Autophone verifies SpringBoard process identity and memory region tags. If `procexp` is not installed in the guest, build the read-only probe with `scripts/build_guest_helpers.sh` and stage it automatically via `autophone rig up --springboard-region-probe ...` (see [Core Distribution & Standalone Install](../public-core.md#安装前检查)).

CLI workflow:

```bash
# Ingest an IPA into the Package Store
autophone app package add example ./Example-1.2.0.ipa --label v1.2.0

# Inspect Version Slots, Build Records, and current per-target install receipts
autophone app package list example

# Install an exact Build ID onto a specific Rig
autophone app package install example --slot 001 --build-id <build_id> --device rig-a
```

You can also perform the same workflow in the Web Dashboard: select a target Rig, expand **Regression, IPA & Recording**, upload an IPA, choose a Build Record to install, and inspect the installed build receipt.

## 3. Web Recording, Draft Saving & Replay

Web recording shares the device's `ScreenLock` with the remote Device Console so every recorded sequence is cleanly attributable:

1. Select a target Rig and App Profile on the dashboard home page and click **Start Recording**.
2. Tap on the live viewport, press hardware keys such as Home, or dispatch clipboard text — each confirmed action is recorded in order.
3. Click **Finish Recording**, enter a draft name, and click **Save Draft from Recorded Steps**.
4. Click **Replay on Selected Rig** to submit a replay run and inspect the verdict.

> [!NOTE]
> Coordinate-based recording (`tap_xy`) depends on identical screen resolution and UI layout. Before promoting recorded flows into long-term regression assets, rewrite critical interactions using semantic Verbs such as `tap_text`, `wait_text`, and `assert_present` (see [Authoring Cases & Recipes](authoring-cases.en.md) and the [Recipe Verb Reference](../reference/verbs.md)).
