# Authoring Cases & Recipes (Typed-IR)

[简体中文](authoring-cases.md) | **English**

Autophone separates *what to test* (human-readable `Case` definitions) from *how to execute it* (deterministic `Recipe` and `Operation` assets). Deterministic regression runs strictly execute statically validated Typed-IR steps and never rely on live AI decision-making.

## Directory Layout

All App assets live under `$AUTOPHONE_APP_ASSET_DIR/apps/<app>/` (see complete examples under `examples/ios-settings-smoke/` and `examples/food-ordering/`):

```text
apps/<app>/
├── profile.yaml            # App Profile (bundle_id, reset policy, capabilities, etc.)
├── cases/
│   └── cases.yaml          # Case Registry (schema_version: 2, default filename)
├── recipes/
│   ├── r1.yaml             # live Recipe (paired with active cases)
│   └── drafts/
│       └── r2.yaml         # draft Recipe (paired with draft cases)
└── operations/
    └── operations.yaml     # Reusable state-transition Operations (optional)
```

Full auto-generated field dictionaries for every asset file are documented in the [JSON Schema Reference](../reference/schemas.md).

## 1. App Profile (`profile.yaml`)

```yaml
schema_version: v1
app: my_app
product_key: my_app
bundle_id: com.example.myapp
display_name: My App
env: verification
lifecycle: active
reset: relaunch
capabilities:
  api_capture:
    enabled: false
    ready_markers:
      - Home
      - Featured
```

> [!TIP]
> The runtime launch-readiness gate does not trust the OS foreground process name alone. You must declare at least one visible post-launch OCR text marker under `capabilities.api_capture.ready_markers`; otherwise live regression fails closed during setup as `app_ui` unavailable.

## 2. Case Registry (`cases/*.yaml`)

```yaml
meta:
  title: My App Smoke
  app: my_app
  schema_version: 2
  total: 1
  next_seq: 2
cases:
  - id: R1
    revision: 1
    status: draft
    automation: ui_ready
    case_type: Functional
    test_point: Key home entry is visible
    owner: qa
    module: Home
    item: Home screen displays the primary entry label after launch
    login_precondition: any
    steps:
      - Launch the app and wait for the Home screen to settle
    expects:
      - Target entry text is visible on screen
```

## 3. Recipe (`recipes/r1.yaml`)

A `Recipe` is a closed list of steps where each step is either an allowlisted `action` (invoking a registered Verb) or a registered `operation` — with no branching or looping control flow. See the [Recipe Verb Reference](../reference/verbs.md) for all supported Verbs and parameter constraints.

```yaml
id: R1
steps:
  - action: wait_text
    query: General
    timeout_s: 10
  - action: tap_text
    query: General
  - action: assert_present
    queries: ["About", "Software Update"]
    wait_s: 5
```

> [!IMPORTANT]
> Every Recipe must include at least one `assert_*` action. An empty run or a zero-assertion run that finishes without error is marked `error` at runtime to prevent false-green passes (see [Regression Runtime Contract](../concepts/runtime-contract.md)).

## 4. Static Validation & Trial Runs

Run offline static validation before connecting a VM:

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/app-assets"
autophone app case lint my_app
```

Once static validation passes, explicitly trial-run draft cases on a target Rig and inspect the generated HTML evidence report:

```bash
autophone --app my_app --device rig-a run --ids R1 --draft-cases
```

After a draft case passes verification consistently and satisfies promotion gates, promote it to `active` so it joins the default regression suite.
