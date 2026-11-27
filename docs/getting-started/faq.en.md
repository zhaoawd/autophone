# Autophone Testing Environment & Capabilities FAQ

[简体中文](faq.md) | **English**

> Document status: **Current User Guide** (Public Core).
> Scope: Jailbroken iOS virtual machines managed through `vphone-cli`. Exact support for a given App, iOS version, or workflow depends on live verification results for that build (see the [Environment & Verification Matrix](environment-matrix.en.md)).

Autophone uses `vphone` so teams can execute compatible **device-built iOS IPAs**, control test runs deterministically, and collect traceable evidence inside one isolated environment. Xcode Simulator is best suited for development debugging and simulator builds; physical iPhones are required to validate real hardware and stock iOS conditions. Each environment answers different questions.

## Environment Selection

### 1. How does `vphone` compare with Xcode Simulator and physical iPhones?

| Dimension | Xcode Simulator | Autophone + `vphone` | Physical iPhone |
| --- | --- | --- | --- |
| Runtime | Simulator runtime sharing the macOS kernel | Isolated guest VM running adapted iOS firmware | Physical Apple hardware and stock iOS |
| Target Build | Compiled for iOS Simulator | Compatible device-built iOS IPA | Properly signed device-built iOS App |
| Primary Use | Development debugging, layout checks, unit tests, and routine UI tests | Deterministic business-flow regression on exact IPA builds, multi-VM Control Plane, and runtime observation | Real hardware, thermal/power/performance profiling, and stock OS sign-off |
| Main Boundaries | Cannot replace testing an exact device-built IPA and does not reproduce full device behavior | Virtualized hardware, firmware patches, and jailbreak environment differences | Process observation and runtime instrumentation restricted by OS sandboxing; physical device fleet management |

References: [Apple Simulator Architecture (WWDC19)](https://developer.apple.com/videos/play/wwdc2019/418/) · [Running Your App on Simulated or Physical Devices](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices)

### 2. Why choose `vphone` instead of Xcode Simulator?

Autophone manages test targets around an **exact device-built IPA**, binding every Run to a specific Build ID, archive SHA-256 digest, and target installation observation. Even from identical source code, a Simulator build is a distinct build artifact and cannot directly substitute for device-IPA regression — especially when QA or release teams only receive the packaged device IPA.

In addition, jailbroken `vphone` exposes UDS control channels, system location control, process inspection, optional Frida observation, and host-managed runtime/artifact directories. When you have source code and primarily need fast dev-time iteration, Xcode Simulator remains lighter. See [Packages & Recording](../guides/packages-and-recording.md) and [Apple TN3117](https://developer.apple.com/documentation/technotes/tn3117-resolving-build-errors-for-apple-silicon).

### 3. If we are testing device-built IPAs, why not just use physical iPhones?

Stock physical iPhones restrict process attachment, memory-region inspection, unified system location sessions, and file access through OS signing and sandboxing. Jailbroken `vphone` lets a single Apple Silicon macOS host manage multiple isolated VMs, runtime directories, and `ScreenLock` serialization via UDS and a unified Control Plane.

However, whenever your goal is validating an unjailbroken environment, real camera/sensors/baseband behavior, thermal/battery performance, or pre-release device-model compatibility, you **must** test on real physical iPhones. `vphone` results do not replace physical hardware sign-off.

### 4. How should all three environments work together?

| Testing Objective | Recommended Environment | Evidence Scope |
| --- | --- | --- |
| Dev-time debugging, layout adaptation, unit & routine UI tests | Xcode Simulator | Matching simulator build and runtime |
| Deterministic business-flow regression & runtime observation on exact device IPAs | Autophone + `vphone` | Verified IPA build, VM image, and case suite |
| Real camera, sensors, wireless radios, and hardware security enclaves | Physical iPhone | Actual hardware, stock OS version, and lab conditions |
| Performance, battery/thermals, and pre-release sign-off | Physical iPhone | Measured physical device and workload |

## App Onboarding & Compatibility

### 5. Can any IPA run inside `vphone`?

No guarantee. Device-built IPAs can still be affected by iOS version requirements, signing/encryption schemes, hardware-only dependencies, backend checks, or jailbreak/VM detection. Always qualify a new App step by step across installation, launch, core flows, and observation capabilities.

### 6. Do I need App source code to use Autophone?

For compatible, installable IPAs, UI automation, screenshots, Vision OCR / Accessibility (AX) targeting, and system location control do not require source code. Code-level diagnosis, L2 build-manifest symbol exports, or custom instrumentation hooks do benefit from build artifacts or engineering collaboration.

### 7. Both use ARM64 — why can't a device-built IPA run directly on Apple Silicon Simulator?

Sharing the ARM64 CPU ISA does not make the target platform ABI or system frameworks identical. iOS device builds and iOS Simulator builds are distinct compilation targets (see [Apple TN3117](https://developer.apple.com/documentation/technotes/tn3117-resolving-build-errors-for-apple-silicon)).

### 8. Can system location, camera, push notifications, and payments all be tested?

Evaluate each capability per App. Public Core provides system CoreLocation pinning and route replay (see [System Location Guide](../guides/location.md)), but injected coordinates do not validate real GNSS reception. Flows relying on Secure Enclave hardware, APNs push certificates, or production payment rails must be validated separately on physical devices.

## Test Results & AI Boundaries

### 9. Does Autophone let AI improvise actions or decide pass/fail during regression runs?

**No.** Deterministic regression runs never call `autophone.application.ai` for live decision-making. AI is restricted to authoring-time exploration, draft generation, and post-run failure diagnosis; any generated asset enters as `draft` and must pass validation and promotion. Live verdicts come solely from statically validated Typed-IR Recipes and explicit assertions — AI diagnosis can never rewrite a test verdict. See [Regression Runtime Contract](../concepts/runtime-contract.md).

### 10. How do we prove a run tested a specific build rather than another IPA with the same version number?

Autophone binds every test target through an immutable Build Record, IPA SHA-256 digest, explicit `--build-id`, and per-target installation receipts (plus byte-verified build markers where packaged), freezing build identity at Run submission. See [Packages & Recording](../guides/packages-and-recording.md).

### 11. Does a non-passing run always mean the App has a bug?

Not necessarily. Autophone strictly separates `failed` (assertion mismatch or asset defect), `blocked` (device, asset, or prerequisite capability unavailable), and `error` (protocol, timeout, empty assertion, or orchestration failure), recording the exact stage and blocker code in results and HTML reports. See [Regression Runtime Contract](../concepts/runtime-contract.md).

### 12. Can tests inside a VM affect live production backends?

**Yes.** A VM isolates local device state, not the remote servers your App talks to. Connecting to production APIs when placing orders, editing accounts, or sending messages creates real side effects. Define test environments, isolated accounts, and data cleanup policies before onboarding any App.

### 13. How many `vphone` VMs can one Mac run concurrently?

There is no fixed universal cap — capacity depends on host RAM, memory pressure, VM configuration, and workload profile. Touch operations on a single Rig are serialized by `ScreenLock`; horizontal concurrency scales across multiple isolated Rigs coordinated by the Control Plane. See [Terminology](../concepts/terminology.md) and [Multi-VM Control Plane Deployment](../guides/multi-vm-control-plane.md).
