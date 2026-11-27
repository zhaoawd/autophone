# HTTP API 接口目录（自动生成）

> 本文档由 `scripts/generate_public_docs.py` 从公开核心态 FastAPI `app.openapi()`（不含 `delivery` 扩展）自动生成，请勿手工修改。
> 启动看板后亦可直接访问 `/openapi.json` 或 `/docs` 获取实时交互式接口文档。

## 平台与健康检查

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `GET` | `/` | — | Platform Home |
| `GET` | `/api/platform/targets` | — | Platform Targets |
| `GET` | `/health` | — | Health |

## 设备与控制台

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `GET` | `/api/access/devices` | — | Access Devices |
| `POST` | `/api/access/embed-sessions` | `body` (json) | Embed Device Console Session |
| `GET` | `/api/access/grants` | — | Access List Grants |
| `POST` | `/api/access/launch-tokens` | `body` (json) | Embed Launch Token |
| `POST` | `/api/access/reservations/reconcile` | `body` (json) | Access Reconcile Reservations |
| `POST` | `/api/access/sessions` | `body` (json) | Access Create Session |
| `DELETE` | `/api/access/sessions/{session_id}` | `session_id` (path) | Access Revoke Session |
| `GET` | `/api/access/sessions/{session_id}` | `session_id` (path) | Access Get Session |
| `POST` | `/api/access/sessions/{session_id}/kick` | `session_id` (path) | Access Kick Session |
| `GET` | `/api/devices/{device}/console` | `device` (path) | Device Console Status |
| `POST` | `/api/devices/{device}/console` | `device` (path), `body` (json) | Device Console Start |
| `GET` | `/api/devices/{device}/console/{session_id}` | `device` (path), `session_id` (path) | Device Console View |
| `POST` | `/api/devices/{device}/console/{session_id}/event` | `device` (path), `session_id` (path), `body` (json) | Device Console Event |
| `GET` | `/api/devices/{device}/console/{session_id}/frame` | `device` (path), `session_id` (path) | Device Console Frame |
| `POST` | `/api/devices/{device}/console/{session_id}/heartbeat` | `device` (path), `session_id` (path) | Device Console Heartbeat |
| `POST` | `/api/devices/{device}/console/{session_id}/stop` | `device` (path), `session_id` (path) | Device Console Stop |
| `GET` | `/devices/{device}/screen.jpg` | `device` (path) | Screen Frame |
| `GET` | `/devices/{device}/screen.mjpeg` | `device` (path), `interval_ms` (query) | Screen Stream |
| `GET` | `/devices/{device}/status` | `device` (path) | Device Status |
| `GET` | `/embed/device-console` | `device` (query), `mode` (query), `launch` (query) | Device Console Embed |

## 系统定位与路线

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `GET` | `/api/location/checkpoints` | — | List Location Checkpoints |
| `POST` | `/api/location/checkpoints/{session_id}/restore` | `session_id` (path), `body` (json) | Restore Location Checkpoint |
| `GET` | `/api/location/devices/{device}/current` | `device` (path) | Current Location |
| `GET` | `/api/location/routes` | — | List Location Routes |
| `GET` | `/api/location/sessions` | — | List Location Sessions |
| `POST` | `/api/location/sessions` | `body` (json) | Create Location Session |
| `DELETE` | `/api/location/sessions/{session_id}` | `session_id` (path), `body` (json) | Stop Location Session |
| `GET` | `/api/location/sessions/{session_id}` | `session_id` (path) | Get Location Session |
| `PATCH` | `/api/location/sessions/{session_id}` | `session_id` (path), `body` (json) | Control Location Session |
| `POST` | `/api/location/sessions/{session_id}/samples` | `session_id` (path), `body` (json) | Publish Location Sample |

## App、用例、IPA 与录制

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `GET` | `/api/apps` | — | Apps |
| `POST` | `/api/apps` | `body` (json) | Create App |
| `POST` | `/api/apps/from-ipa` | `body` (json) | Create App From Ipa |
| `POST` | `/api/apps/inspect-ipa` | `filename` (query) | Inspect Ipa |
| `GET` | `/api/apps/summary` | — | Apps Summary |
| `PATCH` | `/api/apps/{app_name}` | `app_name` (path), `body` (json) | Update App |
| `GET` | `/api/apps/{app_name}/case-drafts` | `app_name` (path), `required_passes` (query) | Case Drafts |
| `POST` | `/api/apps/{app_name}/case-drafts/{case_id}/promote` | `app_name` (path), `case_id` (path), `body` (json) | Promote Case Draft Endpoint |
| `GET` | `/api/apps/{app_name}/cases` | `app_name` (path), `automation` (query), `include_deprecated` (query) | Cases |
| `POST` | `/api/apps/{app_name}/cases` | `app_name` (path), `body` (json) | Add Case |
| `POST` | `/api/apps/{app_name}/cases/batch-activate` | `app_name` (path), `body` (json) | Batch Activate Cases |
| `POST` | `/api/apps/{app_name}/cases/batch-deprecate` | `app_name` (path), `body` (json) | Batch Deprecate Cases |
| `POST` | `/api/apps/{app_name}/cases/from-verification` | `app_name` (path), `body` (json) | Promote Verification |
| `GET` | `/api/apps/{app_name}/cases/import/batches` | `app_name` (path), `limit` (query) | Case Import Batches |
| `GET` | `/api/apps/{app_name}/cases/import/batches/{batch_id}/progress` | `app_name` (path), `batch_id` (path) | Case Import Batch Progress |
| `POST` | `/api/apps/{app_name}/cases/import/commit` | `app_name` (path), `body` (json) | Commit Case Import |
| `POST` | `/api/apps/{app_name}/cases/import/stage` | `app_name` (path) | Stage Case Import |
| `PATCH` | `/api/apps/{app_name}/cases/{case_id}` | `app_name` (path), `case_id` (path), `body` (json) | Edit Case |
| `GET` | `/api/apps/{app_name}/cases/{case_id}/api-capture` | `app_name` (path), `case_id` (path), `allow_raw` (query) | Case Api Capture |
| `POST` | `/api/apps/{app_name}/cases/{case_id}/deprecate` | `app_name` (path), `case_id` (path), `body` (json) | Deprecate Case |
| `POST` | `/api/apps/{app_name}/cases/{case_id}/suggest-ops` | `app_name` (path), `case_id` (path), `body` (json) | Suggest Case Ops |
| `GET` | `/api/apps/{app_name}/cases/{case_id}/telemetry` | `app_name` (path), `case_id` (path) | Case Telemetry |
| `GET` | `/api/apps/{app_name}/exceptions` | `app_name` (path), `kind` (query), `include_muted` (query), `experimental` (query) | List Exceptions |
| `POST` | `/api/apps/{app_name}/exceptions/disposition` | `app_name` (path), `body` (json) | Set Exception Disposition |
| `POST` | `/api/apps/{app_name}/exceptions/disposition/clear` | `app_name` (path), `body` (json) | Clear Exception Disposition |
| `GET` | `/api/apps/{app_name}/gates/{gate_id}` | `app_name` (path), `gate_id` (path) | Package Gate |
| `GET` | `/api/apps/{app_name}/health` | `app_name` (path) | App Case Health |
| `POST` | `/api/apps/{app_name}/impact/bindings/{binding_id}/review` | `app_name` (path), `binding_id` (path), `body` (json) | Impact Binding Review |
| `GET` | `/api/apps/{app_name}/impact/index` | `app_name` (path), `build` (query), `rebuild` (query) | Impact Index |
| `POST` | `/api/apps/{app_name}/impact/plan` | `app_name` (path), `body` (json) | Impact Plan |
| `GET` | `/api/apps/{app_name}/impact/review/{screen_id}` | `app_name` (path), `screen_id` (path), `build` (query) | Impact Screen Review |
| `POST` | `/api/apps/{app_name}/impact/runs` | `app_name` (path), `body` (json) | Impact Run Create |
| `GET` | `/api/apps/{app_name}/knowledge/business-validity-candidates` | `app_name` (path), `case_id` (query) | Business Validity Candidates |
| `POST` | `/api/apps/{app_name}/knowledge/candidate-adoptions` | `app_name` (path), `body` (json) | Adopt Knowledge Candidate |
| `GET` | `/api/apps/{app_name}/knowledge/claims` | `app_name` (path) | Knowledge Claims |
| `POST` | `/api/apps/{app_name}/knowledge/compile` | `app_name` (path), `body` (json) | Knowledge Compile |
| `POST` | `/api/apps/{app_name}/knowledge/coverage` | `app_name` (path), `body` (json) | Knowledge Coverage |
| `GET` | `/api/apps/{app_name}/knowledge/duplicate-path-hints` | `app_name` (path), `recipe_id` (query) | Duplicate Path Hints |
| `GET` | `/api/apps/{app_name}/knowledge/exception-candidates` | `app_name` (path), `key` (query) | Exception Candidates |
| `GET` | `/api/apps/{app_name}/knowledge/fixture-requirements` | `app_name` (path) | Knowledge Fixture Requirements |
| `GET` | `/api/apps/{app_name}/knowledge/golden-paths/{path_id}/plan` | `app_name` (path), `path_id` (path) | Knowledge Golden Path Plan |
| `POST` | `/api/apps/{app_name}/knowledge/golden-paths/{path_id}/verify` | `app_name` (path), `path_id` (path), `body` (json) | Knowledge Golden Path Verify |
| `GET` | `/api/apps/{app_name}/knowledge/inventory` | `app_name` (path) | Knowledge Inventory |
| `GET` | `/api/apps/{app_name}/knowledge/preparation-card/{case_id}` | `app_name` (path), `case_id` (path), `trial_draft_recipes` (query) | Knowledge Preparation Card |
| `POST` | `/api/apps/{app_name}/knowledge/query` | `app_name` (path), `body` (json) | Knowledge Query |
| `POST` | `/api/apps/{app_name}/knowledge/registry-diff` | `app_name` (path), `body` (json) | Knowledge Registry Diff |
| `POST` | `/api/apps/{app_name}/knowledge/static-diff` | `app_name` (path), `body` (json) | Knowledge Static Diff |
| `POST` | `/api/apps/{app_name}/knowledge/verify` | `app_name` (path), `body` (json) | Knowledge Verify |
| `GET` | `/api/apps/{app_name}/knowledge/view` | `app_name` (path), `status` (query), `trial_draft_recipes` (query) | Knowledge View |
| `GET` | `/api/apps/{app_name}/operations` | `app_name` (path), `include_deprecated` (query) | Operations |
| `GET` | `/api/apps/{app_name}/operations/{op_id}` | `app_name` (path), `op_id` (path) | Operation Detail |
| `POST` | `/api/apps/{app_name}/operations/{op_id}/activate` | `app_name` (path), `op_id` (path), `force` (query) | Activate Operation |
| `POST` | `/api/apps/{app_name}/operations/{op_id}/deprecate` | `app_name` (path), `op_id` (path), `force` (query), `body` (json) | Deprecate Operation |
| `GET` | `/api/apps/{app_name}/operations/{op_id}/promotion` | `app_name` (path), `op_id` (path), `required_passes` (query) | Operation Promotion Decision |
| `PATCH` | `/api/apps/{app_name}/operations/{op_id}/steps` | `app_name` (path), `op_id` (path), `body` (json) | Patch Operation Steps |
| `GET` | `/api/apps/{app_name}/package-catalog` | `app_name` (path) | Package Catalog |
| `GET` | `/api/apps/{app_name}/packages` | `app_name` (path), `rig_id` (query) | Packages |
| `POST` | `/api/apps/{app_name}/packages` | `app_name` (path), `body` (json) | Add Package |
| `PUT` | `/api/apps/{app_name}/packages/default` | `app_name` (path), `body` (json) | Set Default Package |
| `POST` | `/api/apps/{app_name}/packages/upload` | `app_name` (path), `filename` (query) | Upload Package |
| `DELETE` | `/api/apps/{app_name}/packages/{slot}` | `app_name` (path), `slot` (path), `dry_run` (query) | Remove Package |
| `POST` | `/api/apps/{app_name}/packages/{slot}/gate` | `app_name` (path), `slot` (path), `body` (json) | Gate Package |
| `POST` | `/api/apps/{app_name}/packages/{slot}/install` | `app_name` (path), `slot` (path), `body` (json) | Install Package |
| `GET` | `/api/apps/{app_name}/recordings` | `app_name` (path), `device` (query) | List Recordings |
| `POST` | `/api/apps/{app_name}/recordings/replay-batch` | `app_name` (path), `body` (json) | Submit Replay Batch |
| `POST` | `/api/apps/{app_name}/recordings/start` | `app_name` (path), `body` (json) | Start Recording |
| `DELETE` | `/api/apps/{app_name}/recordings/{recording_id}` | `app_name` (path), `recording_id` (path) | Delete Recording |
| `PATCH` | `/api/apps/{app_name}/recordings/{recording_id}` | `app_name` (path), `recording_id` (path), `body` (json) | Edit Recording |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/cancel` | `app_name` (path), `recording_id` (path) | Cancel Recording |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/case-recipe` | `app_name` (path), `recording_id` (path) | Recording Case Context |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/case-recipe` | `app_name` (path), `recording_id` (path), `body` (json) | Recording Case Recipe |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/land` | `app_name` (path), `recording_id` (path), `body` (json) | Land Recording |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/lift-preview` | `app_name` (path), `recording_id` (path), `body` (json) | Lift Preview Recording |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/lift-preview-cache` | `app_name` (path), `recording_id` (path) | Get Lift Preview Cache |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/lift-preview-step` | `app_name` (path), `recording_id` (path), `body` (json) | Lift Preview Recording Step |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/recorded-steps` | `app_name` (path), `recording_id` (path), `body` (json) | Land Recorded Steps |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/session` | `app_name` (path), `recording_id` (path) | Web Record Session |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/cancel` | `app_name` (path), `recording_id` (path) | Web Record Cancel |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/claim` | `app_name` (path), `recording_id` (path) | Web Record Claim |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/event` | `app_name` (path), `recording_id` (path), `body` (json) | Web Record Event |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/finish` | `app_name` (path), `recording_id` (path) | Web Record Finish |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/session/frame` | `app_name` (path), `recording_id` (path) | Web Record Frame |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/heartbeat` | `app_name` (path), `recording_id` (path) | Web Record Heartbeat |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/session/label` | `app_name` (path), `recording_id` (path), `body` (json) | Web Record Label |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/shot/{file}` | `app_name` (path), `recording_id` (path), `file` (path) | Recording Shot |
| `GET` | `/api/apps/{app_name}/recordings/{recording_id}/telemetry` | `app_name` (path), `recording_id` (path) | Recording Telemetry |
| `POST` | `/api/apps/{app_name}/recordings/{recording_id}/telemetry/query` | `app_name` (path), `recording_id` (path), `body` (json) | Query Recording Telemetry |
| `GET` | `/api/apps/{app_name}/replay-batches` | `app_name` (path) | List Replay Batches |
| `GET` | `/api/apps/{app_name}/replay-batches/{batch_id}` | `app_name` (path), `batch_id` (path) | Replay Batch Detail |
| `GET` | `/api/apps/{app_name}/scenarios` | `app_name` (path), `env` (query), `include_deprecated` (query) | Scenario Presets |
| `POST` | `/api/apps/{app_name}/scenarios` | `app_name` (path), `body` (json) | Scenario Preset Create |
| `POST` | `/api/apps/{app_name}/scenarios/preflight` | `app_name` (path), `body` (json) | Scenario Preflight |
| `POST` | `/api/apps/{app_name}/scenarios/validate` | `app_name` (path), `body` (json) | Scenario Preset Validate |
| `DELETE` | `/api/apps/{app_name}/scenarios/{preset_id}` | `app_name` (path), `preset_id` (path), `body` (json) | Scenario Preset Delete |
| `GET` | `/api/apps/{app_name}/scenarios/{preset_id}` | `app_name` (path), `preset_id` (path), `version` (query) | Scenario Preset Get |
| `PATCH` | `/api/apps/{app_name}/scenarios/{preset_id}` | `app_name` (path), `preset_id` (path), `body` (json) | Scenario Preset Update |
| `GET` | `/api/apps/{app_name}/screens/{build}` | `app_name` (path), `build` (path) | Get Screen Asset |
| `POST` | `/api/apps/{app_name}/screens/{build}/draft` | `app_name` (path), `build` (path), `body` (json) | Draft Screen Asset |
| `GET` | `/api/apps/{app_name}/screens/{build}/telemetry` | `app_name` (path), `build` (path) | Screen Telemetry Summary |
| `POST` | `/api/apps/{app_name}/screens/{build}/{screen_id}/promote` | `app_name` (path), `build` (path), `screen_id` (path), `body` (json) | Promote Screen Asset |
| `POST` | `/api/apps/{app_name}/telemetry/query` | `app_name` (path), `body` (json) | App Telemetry Query |
| `DELETE` | `/api/apps/{app_name}/versions/{slot}/builds/{build_id}` | `app_name` (path), `slot` (path), `build_id` (path), `dry_run` (query), `force` (query) | Remove Build |
| `POST` | `/api/apps/{app_name}/versions/{slot}/builds/{build_id}/gate` | `app_name` (path), `slot` (path), `build_id` (path), `body` (json) | Gate Build |
| `POST` | `/api/apps/{app_name}/versions/{slot}/builds/{build_id}/install` | `app_name` (path), `slot` (path), `build_id` (path), `body` (json) | Install Build |
| `GET` | `/api/apps/{app}/trend` | `app` (path), `limit` (query) | App Trend |

## 回归运行与证据报告

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `GET` | `/api/runs/history` | `limit` (query), `app` (query), `status` (query), `type` (query), `before` (query) | Run History |
| `GET` | `/api/runs/index/status` | — | Run Index Status |
| `GET` | `/api/runs/{run_id}` | `run_id` (path) | Run Detail |
| `POST` | `/api/runs/{run_id}/abort` | `run_id` (path), `body` (json) | Run Abort |
| `POST` | `/api/runs/{run_id}/ai/apply-fix` | `run_id` (path), `run_dir` (query), `body` (json) | Ai Apply Fix |
| `POST` | `/api/runs/{run_id}/ai/propose-fix` | `run_id` (path), `run_dir` (query), `body` (json) | Ai Propose Fix |
| `POST` | `/api/runs/{run_id}/ai/reject-fix` | `run_id` (path), `run_dir` (query), `body` (json) | Ai Reject Fix |
| `GET` | `/api/runs/{run_id}/api-capture` | `run_id` (path), `allow_raw` (query) | Run Api Capture |
| `POST` | `/api/runs/{run_id}/api-replay` | `run_id` (path), `body` (json) | Run Api Replay |
| `GET` | `/api/runs/{run_id}/artifact/{artifact_path}` | `run_id` (path), `artifact_path` (path) | Run Artifact |
| `POST` | `/api/runs/{run_id}/case-draft-review` | `run_id` (path), `body` (json) | Review Case Draft Run |
| `GET` | `/api/runs/{run_id}/events` | `run_id` (path), `interval_s` (query), `cursor` (query), `logs` (query) | Run Events |
| `GET` | `/api/runs/{run_id}/logs` | `run_id` (path), `cursor` (query), `before` (query), `limit` (query) | Run Logs |
| `POST` | `/api/runs/{run_id}/manual/resume` | `run_id` (path), `body` (json) | Run Manual Resume |
| `POST` | `/api/runs/{run_id}/operation/review` | `run_id` (path), `body` (json) | Run Operation Review |
| `POST` | `/api/runs/{run_id}/rerun` | `run_id` (path), `body` (json) | Rerun Run |
| `GET` | `/api/runs/{run_id}/telemetry` | `run_id` (path) | Run Telemetry |
| `POST` | `/api/runs/{run_id}/telemetry/refresh` | `run_id` (path), `body` (json) | Refresh Run Telemetry |
| `GET` | `/api/runs/{run_id}/trace` | `run_id` (path) | Run Trace |
| `POST` | `/api/runs/{run_id}/verification/review` | `run_id` (path), `body` (json) | Run Verification Review |
| `POST` | `/runs` | `body` (json) | Create Run |

## Control Plane 控制面

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `POST` | `/api/control/assignments` | `body` (json) | Submit |
| `GET` | `/api/control/assignments/{assignment_id}` | `assignment_id` (path) | Get Assignment |
| `POST` | `/api/control/assignments/{assignment_id}/cancel` | `assignment_id` (path), `body` (json) | Cancel Assignment |
| `POST` | `/api/control/assignments/{assignment_id}/retry` | `assignment_id` (path) | Retry Assignment |
| `POST` | `/api/control/host-maintenance/{maintenance_id}/complete` | `maintenance_id` (path), `body` (json) | Finish Host Maintenance |
| `GET` | `/api/control/hosts/{host_id}` | `host_id` (path) | Get Host |
| `POST` | `/api/control/hosts/{host_id}/capacity` | `host_id` (path), `body` (json) | Change Host Capacity |
| `POST` | `/api/control/hosts/{host_id}/maintenance` | `host_id` (path), `body` (json) | Begin Host Maintenance |
| `POST` | `/api/control/reservations` | `body` (json) | Create Reservation |
| `POST` | `/api/control/reservations/{reservation_id}/release` | `reservation_id` (path), `body` (json) | Release Reservation |
| `GET` | `/api/control/rigs` | `include_deregistered` (query) | List Rigs |
| `POST` | `/api/control/rigs/{rig_id}/deregister` | `rig_id` (path), `body` (json) | Deregister Rig |
| `POST` | `/api/control/rigs/{rig_id}/recover` | `rig_id` (path), `body` (json) | Recover Rig |

## 其他核心接口

| 方法 | 路径 | 参数 / 请求体 | 摘要 |
| --- | --- | --- | --- |
| `POST` | `/api/ai/ask` | `body` (json) | Ai Ask |
| `POST` | `/api/ai/ask/{planning_job_id}/approve` | `planning_job_id` (path), `body` (json) | Ai Ask Approve |
| `POST` | `/api/ai/convert-case` | `body` (json) | Ai Convert Case |
| `POST` | `/api/ai/diagnose` | `body` (json) | Ai Diagnose |
| `GET` | `/api/ai/doctor` | — | Ai Doctor |
| `POST` | `/api/ai/draft-operation` | `body` (json) | Ai Draft Operation |
| `GET` | `/api/ai/jobs` | `status` (query), `limit` (query) | Ai Job List |
| `GET` | `/api/ai/jobs/{job_id}` | `job_id` (path) | Ai Job |
| `POST` | `/api/ai/jobs/{job_id}/cancel` | `job_id` (path) | Ai Job Cancel |
| `POST` | `/api/ai/probes` | `body` (json) | Ai Probe |
| `GET` | `/api/ai/verify-doctor` | — | Ai Verify Doctor |
| `GET` | `/api/background/jobs/{job_id}` | `job_id` (path) | Background Job |
| `GET` | `/api/cases/template` | — | Download Case Template |
| `GET` | `/api/doctor` | — | Doctor |
| `POST` | `/api/failure-sample` | `body` (json) | Create Failure Sample Run |
| `GET` | `/api/impact/runs/{run_id}` | `run_id` (path) | Impact Run Detail |
| `GET` | `/api/promotion/{draft_id}` | `draft_id` (path), `app` (query), `required_passes` (query) | Promotion Decision |
| `GET` | `/api/runtime/identity` | — | Runtime Identity |
| `POST` | `/api/scenario-plans/{plan_id}/start` | `plan_id` (path), `body` (json) | Scenario Start |
| `GET` | `/api/scenario-sessions/{session_id}` | `session_id` (path) | Scenario Status |
| `POST` | `/api/scenario-sessions/{session_id}/close` | `session_id` (path) | Scenario Close |
| `GET` | `/api/schedules` | — | Schedules |
| `PATCH` | `/api/schedules/{schedule_id}` | `schedule_id` (path), `body` (json) | Update Schedule |
| `POST` | `/api/schedules/{schedule_id}/fire` | `schedule_id` (path) | Fire Schedule |
| `GET` | `/api/verifications` | `draft_id` (query) | Verifications |
| `GET` | `/api/version` | — | Api Version |
| `DELETE` | `/api/vm/foreground` | — | Clear Foreground |
| `GET` | `/api/vm/foreground` | — | Get Foreground |
| `POST` | `/api/vm/foreground` | `body` (json) | Set Foreground |
| `GET` | `/api/vm/installed-apps` | — | Vm Installed Apps |
| `GET` | `/api/vm/location/points` | `kind` (query) | List Location Points |
| `POST` | `/api/vm/location/points` | `body` (json) | Add Location Point |
| `DELETE` | `/api/vm/location/points/{point_id}` | `point_id` (path) | Delete Location Point |
| `GET` | `/api/vm/location/policies` | — | List App Location Policies |
| `GET` | `/api/vm/recovery` | — | Vm Recovery |
| `GET` | `/api/vm/target` | — | Get Vm Target |
| `GET` | `/workspace` | — | Index |
