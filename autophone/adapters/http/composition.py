from __future__ import annotations

try:
    from fastapi import FastAPI, HTTPException, Query, Request, Response
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as import_error:  # pragma: no cover - exercised only without platform deps
    raise RuntimeError(
        "FastAPI is required for the HTTP service. Install with "
        "`uv sync --group platform` or run with `uv run --group platform ...`."
    ) from import_error

import atexit
import base64
import asyncio
import json
import re
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from urllib.parse import unquote
from autophone.paths import (
    CODE_ROOT,
    STATE_ROOT,
    AppAssetRootError,
    app_asset_root_overridden,
    describe_app_asset_root,
)
from autophone.execution.release_lock import (
    ReleaseLockBusy,
    inherited_exclusive_release_lock,
    release_lock,
)
from autophone.adapters.extensions import register_extensions
from autophone.assets.apps import (
    create_app_profile,
    list_app_profiles,
    load_app_profile,
    set_app_lifecycle,
    suggest_app_slug,
)
from autophone.application.app_summary import build_app_summaries
from autophone.assets import store as asset_store
from autophone.assets.policy import ReadonlyAssetsError, assert_assets_writable
from autophone.version import build_identity, get_version
from autophone.assets import cases as cases_store
from autophone.execution.admission import activation_blocker
from autophone.assets.promotion import (
    case_requires_discrimination,
    discrimination_gaps,
    discrimination_status,
    load_discrimination_proof,
)
from autophone.assets.promotion_identity import (
    current_artifact_content_digest,
    current_discrimination_context,
)
from autophone.adapters.http import _common as _http_common
from autophone.application.package_install import PackageInstallApplication
from autophone.application import case_exceptions as exc
from autophone.assets import operations_store
from autophone.packages import store as pkg
from autophone.packages.model import (
    BuildSelector,
    PackageModelError,
    SourceRevision,
)
from autophone.record import recordings
from autophone.application.device_console import DeviceConsoleManager, UdsDeviceDriver
from autophone.record.web import WebRecordError
from autophone.application.recording_session import RecordingSessionOps
from autophone.application import schedule_runner as scheduler_mod
from autophone.application import screen_draft
from autophone.assets import screens as screen_assets
from autophone.application import gate as gate_mod
from autophone.application import ai_fix as ai_fix_mod
from autophone.execution.foreground import ForegroundKeeper
from autophone.application.failure_sample import create_failure_sample, failure_sample_spec
from autophone.observation.artifacts import GovernanceError
from autophone.application import health as health_mod
from autophone.location.coordinator import (
    LocationSessionCoordinator,
    location_checkpoint_store,
)
from autophone.location import points as location_points
from autophone.execution.manual import ManualCheckpointError
from autophone.application.ai_jobs import AiJobService
from autophone.util import now_iso, read_json
from autophone.execution.run_abort import RunNotAbortable
from autophone.scenario.presets import (
    ScenarioPresetConflict,
    ScenarioPresetError,
    ScenarioPresetNotFound,
    ScenarioPresetStore,
)
from autophone.scenario.runtime import (
    ScenarioDeviceBusy,
    ScenarioPlanNotFound,
    ScenarioRuntime,
    ScenarioRuntimeError,
    ScenarioSessionNotFound,
    production_controller,
)
from autophone.scenario.evidence import ScenarioEvidenceStore
from autophone.scenario.upstream_witness import upstream_witness_context
from autophone.execution.run_service import (
    ContractError,
    RunService,
)
from autophone.contracts import validate_or_raise
from autophone.application.control_plane import (
    ControlPlaneNotConfigured,
    ControlPlaneUnavailable,
    configured_control_plane,
    require_control_plane,
)
from autophone.control.routes import create_control_router, validate_idempotency_key
from autophone.rig.observed_state import FileRigObservedState
from autophone.application.cases import (
    CaseLifecycleApplication,
    CaseLifecycleApplicationError,
    CaseRecipeApplication,
    CaseRecipeApplicationError,
)
from autophone.application.ai_scenes import AiSceneApplication, AiSceneApplicationError
from autophone.application.device import (
    DeviceApplication,
    DeviceApplicationError,
    ForegroundApplication,
    ForegroundApplicationError,
)
from autophone.application.device_session import (
    DeviceSessionOps,
    DeviceSessionRefused,
    RemoteDeviceSessions,
    control_http_base_url,
)
from autophone.application.embed_access import (
    SELF_PLATFORM_ID as EMBED_SELF_PLATFORM,
    EmbedAccessError,
    EmbedAccessRegistry,
)
from autophone.application.access_grants import GrantConflict
from autophone.application.console_release import ConsoleReleaseReset
from autophone.application.device_access import (
    DeviceAccessError,
    DeviceAccessService,
    reconcile_console_reservations,
)
from autophone.application.location import (
    LocationApplicationError,
    UnifiedLocationApplication,
)
from autophone.application.uploads import UploadApplication, UploadApplicationError
from autophone.control import ControlStateError
from autophone.observation.telemetry import TelemetryQueryValidationError, query_telemetry
from autophone.observation.data_trace import (
    DataTraceAdapterError,
    DataTraceBackendError,
    DataTraceConfigurationError,
    DataTraceContractError,
    DataTraceQueryError,
)
from autophone.logging import level_from_env, setup_logging
from autophone.adapters.http.web import render_dashboard_html, render_device_console_embed_html
from .deps import HttpDeps
from . import ai as ai_module
from . import cases as cases_module
from . import devices as devices_module
from . import impact as impact_module
from . import knowledge as knowledge_module
from . import location as location_module
from . import misc as misc_module
from . import packages as packages_module
from . import recordings as recordings_module
from . import runs as runs_module
from . import runtime_routes as runtime_module
from . import scenario as scenario_module


def build_dependencies(acquire, release) -> HttpDeps:
    deps = HttpDeps()
    deps.AiJobService = AiJobService
    deps.AiSceneApplication = AiSceneApplication
    deps.AiSceneApplicationError = AiSceneApplicationError
    deps.BuildSelector = BuildSelector
    deps.CODE_ROOT = CODE_ROOT
    deps.CaseLifecycleApplication = CaseLifecycleApplication
    deps.CaseLifecycleApplicationError = CaseLifecycleApplicationError
    deps.CaseRecipeApplication = CaseRecipeApplication
    deps.CaseRecipeApplicationError = CaseRecipeApplicationError
    deps.ConsoleReleaseReset = ConsoleReleaseReset
    deps.ContextVar = ContextVar
    deps.ContractError = ContractError
    deps.ControlPlaneNotConfigured = ControlPlaneNotConfigured
    deps.ControlPlaneUnavailable = ControlPlaneUnavailable
    deps.ControlStateError = ControlStateError
    deps.DataTraceAdapterError = DataTraceAdapterError
    deps.DataTraceBackendError = DataTraceBackendError
    deps.DataTraceConfigurationError = DataTraceConfigurationError
    deps.DataTraceContractError = DataTraceContractError
    deps.DataTraceQueryError = DataTraceQueryError
    deps.DeviceAccessError = DeviceAccessError
    deps.DeviceAccessService = DeviceAccessService
    deps.DeviceApplication = DeviceApplication
    deps.DeviceApplicationError = DeviceApplicationError
    deps.DeviceConsoleManager = DeviceConsoleManager
    deps.DeviceSessionOps = DeviceSessionOps
    deps.DeviceSessionRefused = DeviceSessionRefused
    deps.EMBED_SELF_PLATFORM = EMBED_SELF_PLATFORM
    deps.EmbedAccessError = EmbedAccessError
    deps.EmbedAccessRegistry = EmbedAccessRegistry
    deps.FastAPI = FastAPI
    deps.FileResponse = FileResponse
    deps.FileRigObservedState = FileRigObservedState
    deps.ForegroundApplication = ForegroundApplication
    deps.ForegroundApplicationError = ForegroundApplicationError
    deps.ForegroundKeeper = ForegroundKeeper
    deps.GovernanceError = GovernanceError
    deps.GrantConflict = GrantConflict
    deps.HTMLResponse = HTMLResponse
    deps.HTTPException = HTTPException
    deps.JSONResponse = JSONResponse
    deps.LocationApplicationError = LocationApplicationError
    deps.LocationSessionCoordinator = LocationSessionCoordinator
    deps.ManualCheckpointError = ManualCheckpointError
    deps.PackageInstallApplication = PackageInstallApplication
    deps.PackageModelError = PackageModelError
    deps.Path = Path
    deps.PlainTextResponse = PlainTextResponse
    deps.Query = Query
    deps.RLock = RLock
    deps.ReadonlyAssetsError = ReadonlyAssetsError
    deps.ReleaseLockBusy = ReleaseLockBusy
    deps.RemoteDeviceSessions = RemoteDeviceSessions
    deps.Request = Request
    deps.Response = Response
    deps.RunNotAbortable = RunNotAbortable
    deps.RunService = RunService
    deps.STATE_ROOT = STATE_ROOT
    deps.ScenarioDeviceBusy = ScenarioDeviceBusy
    deps.ScenarioEvidenceStore = ScenarioEvidenceStore
    deps.ScenarioPlanNotFound = ScenarioPlanNotFound
    deps.ScenarioPresetConflict = ScenarioPresetConflict
    deps.ScenarioPresetError = ScenarioPresetError
    deps.ScenarioPresetNotFound = ScenarioPresetNotFound
    deps.ScenarioPresetStore = ScenarioPresetStore
    deps.ScenarioRuntime = ScenarioRuntime
    deps.ScenarioRuntimeError = ScenarioRuntimeError
    deps.ScenarioSessionNotFound = ScenarioSessionNotFound
    deps.SourceRevision = SourceRevision
    deps.StaticFiles = StaticFiles
    deps.StreamingResponse = StreamingResponse
    deps.TelemetryQueryValidationError = TelemetryQueryValidationError
    deps.UdsDeviceDriver = UdsDeviceDriver
    deps.UnifiedLocationApplication = UnifiedLocationApplication
    deps.UploadApplication = UploadApplication
    deps.UploadApplicationError = UploadApplicationError
    deps.WebRecordError = WebRecordError
    deps._http_common = _http_common
    deps.activation_blocker = activation_blocker
    deps.ai_fix_mod = ai_fix_mod
    deps.assert_assets_writable = assert_assets_writable
    deps.asset_store = asset_store
    deps.asynccontextmanager = asynccontextmanager
    deps.asyncio = asyncio
    deps.atexit = atexit
    deps.base64 = base64
    deps.build_app_summaries = build_app_summaries
    deps.build_identity = build_identity
    deps.case_requires_discrimination = case_requires_discrimination
    deps.cases_store = cases_store
    deps.configured_control_plane = configured_control_plane
    deps.control_http_base_url = control_http_base_url
    deps.create_app_profile = create_app_profile
    deps.create_control_router = create_control_router
    deps.create_failure_sample = create_failure_sample
    deps.current_artifact_content_digest = current_artifact_content_digest
    deps.current_discrimination_context = current_discrimination_context
    deps.discrimination_gaps = discrimination_gaps
    deps.discrimination_status = discrimination_status
    deps.exc = exc
    deps.failure_sample_spec = failure_sample_spec
    deps.gate_mod = gate_mod
    deps.get_version = get_version
    deps.health_mod = health_mod
    deps.inherited_exclusive_release_lock = inherited_exclusive_release_lock
    deps.json = json
    deps.level_from_env = level_from_env
    deps.list_app_profiles = list_app_profiles
    deps.load_app_profile = load_app_profile
    deps.load_discrimination_proof = load_discrimination_proof
    deps.location_checkpoint_store = location_checkpoint_store
    deps.location_points = location_points
    deps.logging = logging
    deps.now_iso = now_iso
    deps.operations_store = operations_store
    deps.os = os
    deps.pkg = pkg
    deps.production_controller = production_controller
    deps.query_telemetry = query_telemetry
    deps.re = re
    deps.read_json = read_json
    deps.reconcile_console_reservations = reconcile_console_reservations
    deps.recordings = recordings
    deps.release_lock = release_lock
    deps.render_dashboard_html = render_dashboard_html
    deps.render_device_console_embed_html = render_device_console_embed_html
    deps.require_control_plane = require_control_plane
    deps.scheduler_mod = scheduler_mod
    deps.screen_assets = screen_assets
    deps.screen_draft = screen_draft
    deps.secrets = secrets
    deps.set_app_lifecycle = set_app_lifecycle
    deps.setup_logging = setup_logging
    deps.shutil = shutil
    deps.suggest_app_slug = suggest_app_slug
    deps.time = time
    deps.unquote = unquote
    deps.upstream_witness_context = upstream_witness_context
    deps.validate_idempotency_key = validate_idempotency_key
    deps.validate_or_raise = validate_or_raise
    deps.__file__ = __file__
    deps.logger = logging.getLogger(__name__)
    deps._acquire_runtime_release_lock = acquire
    deps._release_runtime_release_lock = release
    ai_module.install_helpers(deps)
    cases_module.install_helpers(deps)
    devices_module.install_helpers(deps)
    impact_module.install_helpers(deps)
    knowledge_module.install_helpers(deps)
    location_module.install_helpers(deps)
    misc_module.install_helpers(deps)
    packages_module.install_helpers(deps)
    recordings_module.install_helpers(deps)
    runs_module.install_helpers(deps)
    runtime_module.install_helpers(deps)
    scenario_module.install_helpers(deps)


    deps.setup_logging(deps.level_from_env())

    # App 资产根决定用例库、Recipe、操作库等真源的位置。被 AUTOPHONE_APP_ASSET_DIR
    # 移走时按 WARNING 打出，避免生产进程在不知情的情况下读写另一棵资产树。
    if app_asset_root_overridden():
        deps.logger.warning(describe_app_asset_root())
    else:
        deps.logger.info(describe_app_asset_root())

    deps.device_application = deps.DeviceApplication()

    deps.DEFAULT_DEVICE = deps.device_application.default_device

    deps.app = deps.FastAPI(title="autophone Run service", version=deps.get_version(), lifespan=deps._lifespan)

    deps.app.exception_handler(ReadonlyAssetsError)(deps._readonly_assets_error_handler)

    deps.app.exception_handler(AppAssetRootError)(deps._app_asset_root_error_handler)

    deps.app.exception_handler(ReleaseLockBusy)(deps._release_lock_busy_handler)

    deps.static_dir = deps.Path(deps.__file__).parent / "static"

    if deps.static_dir.exists():
        deps.app.mount("/static", deps.StaticFiles(directory=str(deps.static_dir)), name="static")

    deps.service = deps.RunService()

    deps.recording_sessions = RecordingSessionOps(lock=deps.service.lock)

    deps.device_console = deps.DeviceConsoleManager(
        lock=deps.service.lock,
        # 释放复位（§7.3）：兼容路径下 console 会话在本进程里，钩子也就装在这里。
        # 控制面模式下会话在 rig 进程，那一份由 `execution/handlers.py` 装配。
        release_cleanup=deps.ConsoleReleaseReset(driver_factory=deps.UdsDeviceDriver),
    )

    deps.local_device_sessions = deps.DeviceSessionOps(
        human_holder=deps.service.lock.read_human_holder,
        console=deps.device_console,
        recordings=deps.recording_sessions,
        screenshot=deps.device_application.screenshot,
        status=lambda device: deps.service.status(device=device),
        target=deps.device_application.target,
        # BL-227：本进程有活动 Stage（线程 worker 执行 Run）时预览返回执行侧最近帧。
        executor_preview=deps.device_application.executor_preview,
    )

    deps.scenario_evidence = deps.ScenarioEvidenceStore(
        run_root=deps.service.run_root,
        persist_result=deps.service.persist_run_result,
    )

    deps.scenario_runtime = deps.ScenarioRuntime(
        controller_factory=deps.production_controller,
        lock=deps.service.lock,
        evidence_store=deps.scenario_evidence,
        observation_context_factory=deps.upstream_witness_context,
    )

    from autophone.application.package_gate_jobs import PackageGateJobs
    deps.package_gate_jobs = PackageGateJobs(deps.gate_mod, deps.service)
    deps.ai_jobs = deps.AiJobService()

    deps.ai_scenes = deps.AiSceneApplication(
        jobs=deps.ai_jobs,
        run_service=deps.service,
        recordings=deps.recordings,
        fix=deps.ai_fix_mod,
        default_device=deps.DEFAULT_DEVICE,
    )

    deps.ai_scenes.register()

    deps.schedule_runner = deps.scheduler_mod.Scheduler(deps.service)

    deps._schedule_task: asyncio.Task | None = None

    deps.foreground_keeper = deps.ForegroundKeeper(
        interval_s=deps._foreground_interval(),
        enabled=deps.os.environ.get("AUTOPHONE_FOREGROUND_ENABLED", "1") not in ("0", "false", "False"),
        # BL-232 阶段 1（P1）：不注入 SpringBoard/Frida 前台后备；UDS 前台来源不可信时进入降级模式。
        lock_status_fn=lambda device: deps.service.lock.status(device=device))

    from autophone.location.coordinator import production_location_target_factory
    from autophone.location.system import CallableSystemLocationTransport, SystemLocationClient

    def location_adapter(session, target, replace):
        transport = CallableSystemLocationTransport(
            lambda payload: deps._device_sessions(session.device).call(
                session.device, "system_location", dict(payload),
            )
        )
        return production_location_target_factory(
            session, target, replace, client=SystemLocationClient(transport),
        )

    deps.location_session_coordinator = deps.LocationSessionCoordinator(
        adapter_factory=location_adapter,
        lock_status_fn=lambda device: deps._device_sessions(device).call(device, "location_lock_status", {}),
    )

    deps.location_control_mu = deps.RLock()

    deps.service.location_quiesce_fn = deps._await_location_quiesced

    deps.MAX_IPA_UPLOAD_BYTES = int(deps.os.environ.get("AUTOPHONE_MAX_IPA_UPLOAD_BYTES", str(1024 * 1024 * 1024)))

    deps._BASIC_AUTH_USERS = deps._load_basic_auth_users()

    deps._REQUEST_ACTOR = deps._http_common.REQUEST_ACTOR

    deps._REQUEST_IDEMPOTENCY_KEY = deps._http_common.REQUEST_IDEMPOTENCY_KEY

    deps._current_actor = deps._http_common.current_actor

    deps.MAX_CASE_XLSX_BYTES = int(deps.os.environ.get("AUTOPHONE_MAX_CASE_XLSX_BYTES", str(16 * 1024 * 1024)))

    deps._request_content_length = deps._http_common.request_content_length

    deps.EMBED_LAUNCH_PATH = "/api/access/launch-tokens"

    deps.EMBED_EXCHANGE_PATH = "/api/access/embed-sessions"

    deps.CONSOLE_ACCESS_HEADER = "x-console-access-token"

    deps.LAUNCH_TOKEN_HEADER = "x-console-launch-token"

    deps._CONSOLE_SESSION_PATH = deps.re.compile(
        r"^/api/devices/(?P<device>[^/]+)/console/(?P<session>[^/]+?)"
        r"(?P<action>/frame|/event|/heartbeat|/stop)?$"
    )

    deps.CONSOLE_CONTROL_ACTIONS = frozenset({"event", "heartbeat", "stop"})

    deps.ACCESS_DEVICES_PATH = "/api/access/devices"

    deps.ACCESS_SESSIONS_PATH = "/api/access/sessions"

    deps._ACCESS_API_PATH = deps.re.compile(r"^/api/access/(devices|sessions(?:/[^/]+)?)$")

    deps.embed_access = deps.EmbedAccessRegistry.from_environment()

    deps.device_access = deps.DeviceAccessService(
        access=deps.embed_access,
        call=lambda device, op, payload: deps._device_sessions(device).call(device, op, payload),
        connected=deps._access_connected_devices,
        embed_base_url=deps.os.environ.get("AUTOPHONE_EMBED_BASE_URL", "").strip(),
    )

    deps.app.middleware('http')(deps._console_session_access)

    deps.app.middleware('http')(deps._basic_auth)

    deps.app.middleware('http')(deps._no_store_api)

    deps.app.exception_handler(ControlPlaneUnavailable)(deps._control_plane_unavailable_handler)

    deps.app.include_router(deps.create_control_router(
        lambda: deps._control_or_http_error(),
        actor_provider=lambda: deps._current_actor(),
        host_provider=lambda: deps._control_or_http_error(),
    ))

    register_extensions("http", deps)
    deps.app.include_router(ai_module.create_router(deps))
    deps.app.include_router(cases_module.create_router(deps))
    deps.app.include_router(devices_module.create_router(deps))
    deps.app.include_router(impact_module.create_router(deps))
    deps.app.include_router(knowledge_module.create_router(deps))
    deps.app.include_router(location_module.create_router(deps))
    deps.app.include_router(misc_module.create_router(deps))
    deps.app.include_router(packages_module.create_router(deps))
    deps.app.include_router(recordings_module.create_router(deps))
    deps.app.include_router(runs_module.create_router(deps))
    deps.app.include_router(scenario_module.create_router(deps))
    return deps
