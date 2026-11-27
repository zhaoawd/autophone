from __future__ import annotations

from pathlib import Path
from autophone.application.control_plane import (
    ControlPlaneRequiredForRigTarget,
    TargetRigPinRefused,
)
from autophone.application.package_install import PackageInstallApplication
from autophone.packages.model import (
    SourceRevision,
)
from fastapi import Request
from fastapi import APIRouter
from .deps import HttpDeps

def _rig_target_without_control_plane(
    deps: HttpDeps, exc: ControlPlaneRequiredForRigTarget,
):
    """`rig_id` 点名了目标 Rig 而本机没有控制面：503 + 稳定 code。

    503 与「控制面已配置但不可用」同一档：请求本身合法，缺的是本机的控制面配置，
    不是请求内容。返回 200 并在本机装一遍才是要消除的静默回退。
    """
    return deps.HTTPException(
        status_code=503,
        detail={"code": exc.code, "message": str(exc)},
    )


def _explicit_rig(body: dict) -> str | None:
    """安装请求显式点名的 Rig：`rig_id`，兼容 `target_rig_id`（BL-218）。"""
    return body.get("rig_id") or body.get("target_rig_id")


def _unpinned_install(deps: HttpDeps, exc: TargetRigPinRefused):
    """控制面已配置而目标 Rig 未能按设备固定：422 + 稳定 code，未提交 Assignment。"""
    return deps.HTTPException(status_code=422, detail=exc.to_detail())


def install_helpers(deps: HttpDeps) -> None:

    async def _stage_request_ipa(request: Request, filename: str) -> tuple[Path, int]:
        try:
            return await deps._uploads().stage_ipa(
                filename,
                request.stream(),
                content_length=deps._request_content_length(request),
            )
        except deps.UploadApplicationError as error:
            raise deps.HTTPException(error.status_code, detail=error.detail) from error

    deps._stage_request_ipa = _stage_request_ipa

    def _submit_package_gate(body: dict) -> dict:
        from autophone.application.background_jobs import JobConflict, JobQueueFull
        try:
            return deps.package_gate_jobs.submit_gate(
                body, actor=deps._current_actor(),
                idempotency_key=deps._current_idempotency_key(),
            )
        except JobConflict as exc:
            raise deps.HTTPException(409, detail={"code": "idempotency_conflict", "message": str(exc)}) from exc
        except JobQueueFull as exc:
            raise deps.HTTPException(429, detail={"code": "background_queue_full", "message": str(exc)}) from exc

    deps._submit_package_gate = _submit_package_gate

    def _package_install_application() -> PackageInstallApplication:
        return deps.PackageInstallApplication(
            deps.pkg,
            lambda: deps.configured_control_plane(),
            deps.FileRigObservedState.compatibility(),
        )

    deps._package_install_application = _package_install_application

    def _source_revision_from_body(body: dict) -> "SourceRevision | None":
        """Optional source revision on an upload; absence records `unknown`."""
        commit = str(body.get("commit") or "").strip()
        tree = str(body.get("source_tree_sha256") or "").strip()
        if commit and tree:
            raise deps.HTTPException(
                status_code=422, detail="commit and source_tree_sha256 are mutually exclusive")
        try:
            if commit:
                return deps.SourceRevision.git_commit(commit)
            if tree:
                return deps.SourceRevision.source_tree(
                    tree,
                    base_commit_sha=str(body.get("base_commit") or "").strip() or None,
                    state=str(body.get("source_state") or "dirty"),
                )
        except deps.PackageModelError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return None

    deps._source_revision_from_body = _source_revision_from_body


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/background/jobs/{job_id}")
    def background_job(job_id: str) -> dict:
        job = deps.package_gate_jobs.get(job_id, actor=deps._current_actor())
        if job is None:
            raise deps.HTTPException(404, detail="background job not found")
        return job

    @router.get("/api/apps")
    def apps() -> dict:
        return {"apps": [profile.raw for profile in deps.list_app_profiles()]}

    deps.apps = apps

    @router.get("/api/apps/summary")
    def apps_summary() -> dict:
        """Bulk read model for the App center.

        Run history is loaded once, then reduced per App by ``build_app_summaries``;
        policy fields such as effect/recipient allowlists are intentionally absent.
        """
        profiles = deps.list_app_profiles()
        runs = deps.service.queries.history(limit=500)
        return {"apps": deps.build_app_summaries(profiles, runs=runs)}

    deps.apps_summary = apps_summary

    @router.post("/api/apps")
    def create_app(payload: dict | None = None) -> dict:
        """注册一个新被测 App（CLI `app add` 的对等面）。

        body: {app, product_key?, bundle_id, display_name, env?, login_method?, reset?}。只固化身份
        字段 + scaffold 子目录与空注册表，capabilities 留空（降级 not_configured）。
        fail-closed：非法 slug / 空字段 / 非法 login|reset → 422；app 已存在 → 409。
        """
        body = payload or {}
        try:
            profile = deps.create_app_profile(
                str(body.get("app") or ""),
                str(body.get("bundle_id") or ""),
                str(body.get("display_name") or ""),
                product_key=(body.get("product_key") or None),
                env=(body.get("env") or None),
                login_method=str(body.get("login_method") or "manual"),
                reset=str(body.get("reset") or "ui_home"),
            )
        except ValueError as exc:
            # 「已存在」语义上是冲突（409），其余入参问题是 422
            status = 409 if "already exists" in str(exc) else 422
            raise deps.HTTPException(status_code=status, detail=str(exc)) from exc
        deps.logger.info(f"app created: app={profile.app} bundle_id={profile.raw.get('bundle_id')}")
        return {"app": profile.app, "profile": profile.raw}

    deps.create_app = create_app

    @router.patch("/api/apps/{app_name}")
    def update_app(app_name: str, payload: dict | None = None) -> dict:
        """Update the intentionally narrow App Profile lifecycle interface."""
        body = payload or {}
        if set(body) != {"lifecycle"}:
            raise deps.HTTPException(
                status_code=422,
                detail="only lifecycle may be updated here",
            )
        try:
            profile = deps.set_app_lifecycle(app_name, str(body.get("lifecycle") or ""))
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        deps.logger.info(f"app lifecycle updated: app={profile.app} lifecycle={body['lifecycle']}")
        return {"app": profile.app, "profile": profile.raw}

    deps.update_app = update_app

    @router.post("/api/apps/inspect-ipa")
    async def inspect_ipa(request: Request, filename: str = deps.Query(...)) -> dict:
        """Stage a browser-uploaded IPA and echo back its identity for onboarding.

        The onboarding flow is IPA-first: the form picks a build, POSTs the raw
        bytes here, and prefills bundle_id / version / display name from the parsed
        Info.plist. Returns the staged ``ipa_path`` so the subsequent ``POST
        .../packages`` ingests the same file (no second upload). App-agnostic by
        design — no profile exists yet at inspect time. ``suggested_app`` /
        ``suggested_display_name`` are editable defaults, never authoritative.
        """
        try:
            dest, size = await deps._stage_request_ipa(request, filename)
            info = deps.pkg.read_ipa_info(dest)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        suggested_name = info["display_name"] or dest.stem
        return {
            "ipa_path": str(dest),
            "filename": dest.name,
            "size": size,
            "bundle_id": info["bundle_id"],
            "version": info["version"],
            "display_name": info["display_name"],
            "suggested_app": deps.suggest_app_slug(info["bundle_id"]),
            "suggested_display_name": suggested_name,
        }

    deps.inspect_ipa = inspect_ipa

    @router.post("/api/apps/from-ipa")
    def create_app_from_ipa(payload: dict | None = None) -> dict:
        """Atomically create an app profile and ingest the staged IPA as slot 001."""
        body = payload or {}
        ipa_path = str(body.get("ipa_path") or "").strip()
        if not ipa_path:
            raise deps.HTTPException(status_code=422, detail="ipa_path is required")

        profile = None
        try:
            profile = deps.create_app_profile(
                str(body.get("app") or ""),
                str(body.get("bundle_id") or ""),
                str(body.get("display_name") or ""),
                product_key=(body.get("product_key") or None),
                env=(body.get("env") or None),
                login_method=str(body.get("login_method") or "manual"),
                reset=str(body.get("reset") or "ui_home"),
            )
            entry = deps.pkg.add_package(profile.app, ipa_path)
        except ValueError as exc:
            status = 409 if "already exists" in str(exc) else 422
            raise deps.HTTPException(status_code=status, detail=str(exc)) from exc
        except deps.pkg.PackageError as exc:
            if profile is not None:
                deps.shutil.rmtree(profile.profile_dir, ignore_errors=True)
                deps.shutil.rmtree(deps.pkg.store_dir(profile.app), ignore_errors=True)
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        deps.logger.info(
            f"app created from ipa: app={profile.app} bundle_id={profile.raw.get('bundle_id')} "
            f"slot={entry.slot} version={entry.version}"
        )
        return {"app": profile.app, "profile": profile.raw, "package": entry.to_dict()}

    deps.create_app_from_ipa = create_app_from_ipa

    @router.get("/api/apps/{app_name}/package-catalog")
    def package_catalog(app_name: str) -> dict:
        """Version-Slot-grouped read model plus this app's recent gates."""
        try:
            catalog = deps.pkg.catalog(app_name)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        catalog["gates"] = deps.gate_mod.list_gate_results(deps.service.run_root, app=app_name, limit=30)
        return catalog

    deps.package_catalog = package_catalog

    @router.get("/api/apps/{app_name}/packages")
    def packages(app_name: str, rig_id: str | None = None) -> dict:
        """Flat Build listing (compatibility surface for existing callers).

        Two different facts are reported side by side and never merged:
        ``installed`` / ``installed_build`` is what is on the Target, while
        ``default_slot`` is the Asset catalog's default selection.
        """
        catalog = deps.pkg.catalog(app_name)
        entries = deps.pkg.list_packages(app_name)
        gates = deps.gate_mod.list_gate_results(deps.service.run_root, app=app_name, limit=30)
        installed = (catalog.get("installed") or {}).get("build_ref") or {}
        observed_rig_id, installed_build = deps._package_install_application().observed_build(
            app_name,
            rig_id=rig_id,
        )
        return {
            "app": app_name,
            "bundle_id": catalog.get("bundle_id"),
            "active_slot": installed.get("slot"),
            "default_slot": catalog.get("default_slot"),
            "observed_rig_id": observed_rig_id,
            "installed_build": installed_build,
            "packages": [entry.to_view() for entry in entries],
            "version_slots": catalog["version_slots"],
            "installed": catalog.get("installed"),
            "stats": catalog["stats"],
            "gates": gates,
        }

    deps.packages = packages

    @router.post("/api/apps/{app_name}/packages")
    def add_package(app_name: str, payload: dict | None = None) -> dict:
        """Ingest a host-side IPA file into the ordered store (CLI parity).

        Takes a host filesystem path rather than an upload: the IPA already lives
        on the host and add_package fail-closes on bundle-id mismatch anyway.
        """
        body = payload or {}
        ipa_path = str(body.get("ipa_path") or "").strip()
        if not ipa_path:
            raise deps.HTTPException(status_code=422, detail="ipa_path is required")
        try:
            result = deps.pkg.register_build(
                app_name,
                ipa_path,
                source_revision=deps._source_revision_from_body(body),
                label=body.get("label") or None,
                force=bool(body.get("force")),
            )
        except (deps.pkg.PackageError, deps.PackageModelError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        entry = result.entry
        deps.logger.info(
            f"package added: app={app_name} slot={entry.slot} version={entry.version} "
            f"build={result.ref.build_id} label={entry.label!r} duplicate={result.duplicate}"
        )
        return result.to_dict()

    deps.add_package = add_package

    @router.post("/api/apps/{app_name}/packages/upload")
    async def upload_package(
        app_name: str, request: Request, filename: str = deps.Query(...)
    ) -> dict:
        """Stage browser-uploaded IPA bytes on the host and return the saved path.

        Browsers can't hand the host an absolute path, so the dashboard file-picker
        POSTs the raw bytes here; the returned ``ipa_path`` is then fed to
        ``POST .../packages`` for ingestion. ``filename`` is sanitized to its
        basename inside stage_uploaded_ipa.
        """
        try:
            dest, size = await deps._stage_request_ipa(request, filename)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ipa_path": str(dest), "filename": dest.name, "size": size}

    deps.upload_package = upload_package

    @router.post("/api/apps/{app_name}/packages/{slot}/install")
    def install_package(app_name: str, slot: str, payload: dict | None = None) -> dict:
        """Submit provisioning through Control Plane or use the compatibility target.

        ``dry_run`` (query or body) resolves the slot and reports the plan — what
        would be uploaded and installed without touching the VM or Asset catalog.
        The VM-touching install only runs when ``dry_run`` is false.
        """
        body = payload or {}
        if body.get("dry_run"):
            try:
                entry = deps.pkg.resolve_slot(app_name, slot)
                manifest = deps.pkg.catalog(app_name)
            except deps.pkg.PackageError as exc:
                raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "dry_run": True,
                "app": app_name,
                "slot": entry.slot,
                "label": entry.label,
                "bundle_id": entry.bundle_id,
                "version": entry.version,
                "build_id": entry.build_id,
                "current_active_slot": (
                    (manifest.get("installed") or {}).get("build_ref") or {}
                ).get("slot"),
                "would_set_active": entry.slot,
                "default_slot": manifest.get("default_slot"),
                "would_verify": body.get("verify", True),
                "plan": (
                    f"transfer slot {entry.slot} ({entry.file}) → guest, "
                    f"vphone native install, "
                    f"{'then verify LaunchServices' if body.get('verify', True) else 'without readback'}; "
                    "Asset default_slot is unchanged"
                ),
            }
        try:
            deps.logger.info(f"Installing package: app={app_name}, slot={slot}, verify={body.get('verify', True)}")
            result = deps._package_install_application().install(
                app_name,
                slot,
                verify=body.get("verify", True),
                target_rig_id=_explicit_rig(body),
                device=body.get("device"),
                priority=str(body.get("priority") or "normal"),
                idempotency_key=(
                    deps._current_idempotency_key()
                    or str(body.get("idempotency_key") or "")
                    or None
                ),
            )
            deps.logger.info(
                "Package request accepted: app=%s slot=%s assignment=%s verified=%s",
                app_name,
                result.get("slot"),
                result.get("assignment_id"),
                result.get("verified"),
            )
            return result
        except ControlPlaneRequiredForRigTarget as exc:
            raise _rig_target_without_control_plane(deps, exc) from exc
        except TargetRigPinRefused as exc:
            deps.logger.warning("Package install refused before submission: app=%s slot=%s code=%s", app_name, slot, exc.code)
            raise _unpinned_install(deps, exc) from exc
        except (deps.pkg.PackageError, deps.ControlStateError) as exc:
            deps.logger.error(f"Package error during install: app={app_name}, slot={slot}, error={exc}")
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except (deps.pkg.TransferError, deps.pkg.InstallError) as exc:
            deps.logger.error(f"Install/transfer error: app={app_name}, slot={slot}, error={exc}", exc_info=True)
            raise deps.HTTPException(status_code=502, detail=str(exc)) from exc

    deps.install_package = install_package

    @router.post("/api/apps/{app_name}/packages/{slot}/gate")
    def gate_package(app_name: str, slot: str, payload: dict | None = None) -> dict:
        """Install a package slot, run the smoke selector, then diff against baseline."""
        body = payload or {}
        selector = body.get("case_selector") or {"suite": body.get("suite") or "smoke"}
        verify = bool(body.get("verify", True))

        return deps._submit_package_gate({
            "app": app_name, "slot": slot, "selector": selector,
            "verify": verify,
        })

    deps.gate_package = gate_package

    @router.get("/api/apps/{app_name}/gates/{gate_id}")
    def package_gate(app_name: str, gate_id: str) -> dict:
        try:
            out = deps.gate_mod.load_gate_result(deps.service.run_root, gate_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        if out.get("app") != app_name:
            raise deps.HTTPException(status_code=404, detail=f"gate not found for app {app_name}: {gate_id}")
        return out

    deps.package_gate = package_gate

    @router.delete("/api/apps/{app_name}/packages/{slot}")
    def remove_package(app_name: str, slot: str, dry_run: bool = False) -> dict:
        """Drop a slot from the host store (does not uninstall from the VM).

        ``dry_run=true`` reports what would be removed (and whether it is the default
        slot) without deleting the file or touching the manifest.
        """
        try:
            entry = deps.pkg.get_package(app_name, slot)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        if dry_run:
            holds = deps.pkg.build_retention_holds(app_name, str(entry.build_id))
            default_slot = deps.pkg.catalog(app_name).get("default_slot")
            return {
                "dry_run": True,
                "would_remove": entry.to_view(),
                "is_active": "installed_build" in holds,
                "is_default": default_slot == entry.slot,
                "retention_holds": holds,
                "plan": (
                    f"delete host file {entry.file}; "
                    f"{'clear installed receipt; ' if 'installed_build' in holds else ''}"
                    f"{'clear default_slot; ' if default_slot == entry.slot else ''}"
                    "VM stays installed"
                ),
            }
        removed = deps.pkg.remove_package(app_name, slot)
        deps.logger.info(f"package removed (host only): app={app_name} slot={removed.slot} version={removed.version}")
        return {"removed": removed.to_view()}

    deps.remove_package = remove_package

    @router.post("/api/apps/{app_name}/versions/{slot}/builds/{build_id}/install")
    def install_build(
        app_name: str, slot: str, build_id: str, payload: dict | None = None,
    ) -> dict:
        """Install one exact Build. Automation should use this, not the slot route.

        The slot route resolves whichever Build is Candidate at call time; this one
        installs the Build the caller reviewed.
        """
        body = payload or {}
        # Resolve first so an unknown Build is a 404 rather than a failed install.
        try:
            deps.pkg.resolve_build(app_name, deps.BuildSelector(slot=slot, build_id=build_id))
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        # Go through the same Application entry point as the slot route.  Calling
        # ``pkg.install_build`` here would install locally even when a Control
        # Plane owns the Rigs, and would leave the Rig observed state naming the
        # previously installed Build.
        try:
            return deps._package_install_application().install(
                app_name,
                slot,
                build_id=build_id,
                verify=bool(body.get("verify", True)),
                target_rig_id=_explicit_rig(body),
                device=body.get("device"),
                priority=str(body.get("priority") or "normal"),
                idempotency_key=(
                    deps._current_idempotency_key()
                    or str(body.get("idempotency_key") or "")
                    or None
                ),
            )
        except ControlPlaneRequiredForRigTarget as exc:
            raise _rig_target_without_control_plane(deps, exc) from exc
        except TargetRigPinRefused as exc:
            deps.logger.warning("Package install refused before submission: app=%s slot=%s code=%s", app_name, slot, exc.code)
            raise _unpinned_install(deps, exc) from exc
        except (deps.pkg.PackageError, deps.ControlStateError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except (deps.pkg.TransferError, deps.pkg.InstallError) as exc:
            raise deps.HTTPException(status_code=502, detail=str(exc)) from exc

    deps.install_build = install_build

    @router.post("/api/apps/{app_name}/versions/{slot}/builds/{build_id}/gate")
    def gate_build(
        app_name: str, slot: str, build_id: str, payload: dict | None = None,
    ) -> dict:
        """Gate one exact Build, with an explicit baseline policy."""
        body = payload or {}
        selector = body.get("case_selector") or {"suite": body.get("suite") or "smoke"}
        try:
            deps.pkg.resolve_build(app_name, deps.BuildSelector(slot=slot, build_id=build_id))
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        return deps._submit_package_gate({
            "app": app_name, "slot": slot, "selector": selector,
            "verify": bool(body.get("verify", True)),
            "build_id": build_id,
            "baseline_policy": str(body.get("baseline_policy") or "auto"),
            "baseline_build_id": body.get("baseline_build_id") or None,
        })

    deps.gate_build = gate_build

    @router.delete("/api/apps/{app_name}/versions/{slot}/builds/{build_id}")
    def remove_build(
        app_name: str, slot: str, build_id: str, dry_run: bool = False, force: bool = False,
    ) -> dict:
        """Remove one Build Record; its Blob survives while another Build uses it."""
        try:
            ref = deps.pkg.resolve_build(app_name, deps.BuildSelector(slot=slot, build_id=build_id))
            holds = deps.pkg.build_retention_holds(app_name, ref.build_id)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        if dry_run:
            return {
                "dry_run": True,
                "would_remove": ref.to_dict(),
                "retention_holds": holds,
                "blocked": bool(holds) and not force,
            }
        try:
            removed = deps.pkg.remove_build(
                app_name, ref.build_id, policy="force" if force else "guarded",
            )
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        return {"removed": removed.to_view()}

    deps.remove_build = remove_build

    @router.put("/api/apps/{app_name}/packages/default")
    def set_default_package(app_name: str, payload: dict | None = None) -> dict:
        body = payload or {}
        slot = str(body.get("slot") or "").strip()
        if not slot:
            raise deps.HTTPException(status_code=422, detail="slot is required")
        try:
            deps.pkg.set_default_slot(app_name, slot)
        except deps.pkg.PackageError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"app": app_name, "default_slot": slot}

    deps.set_default_package = set_default_package

    return router
