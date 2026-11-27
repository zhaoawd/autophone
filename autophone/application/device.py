"""Application operations for device inventory and screen reads."""
from __future__ import annotations

import base64
from typing import Any


class DeviceApplicationError(RuntimeError):
    pass


class DeviceApplication:
    @property
    def default_device(self) -> str:
        from autophone.device.target import default_device

        return default_device()

    def target(self) -> dict[str, str]:
        from autophone.device.target import resolve_target

        return resolve_target().public_dict()

    def installed_apps(self) -> dict:
        from autophone.assets.apps import list_app_profiles, suggest_app_slug
        from autophone.device.uds import request
        from autophone.execution.engine.actions import resolve_sock

        try:
            response, _ = request(
                {"t": "app_list", "filter": "user"},
                sock_path=resolve_sock(),
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - adapter errors become one application error
            raise DeviceApplicationError(f"device unreachable: {exc}") from exc
        if not response.get("ok", False):
            detail = response.get("error") or "unknown error"
            raise DeviceApplicationError(f"app_list failed: {detail}")

        by_bundle = {profile.bundle_id: profile.app for profile in list_app_profiles()}
        apps: list[dict] = []
        for item in response.get("apps", []):
            bundle_id = str(item.get("bundle_id") or "").strip()
            if not bundle_id:
                continue
            onboarded_app = by_bundle.get(bundle_id)
            apps.append({
                "bundle_id": bundle_id,
                "name": str(item.get("name") or ""),
                "version": str(item.get("version") or ""),
                "onboarded": onboarded_app is not None,
                "app": onboarded_app,
                "suggested_app": suggest_app_slug(bundle_id),
            })
        apps.sort(key=lambda item: (
            item["onboarded"], item["name"].lower(), item["bundle_id"],
        ))
        return {"apps": apps, "count": len(apps)}

    def screenshot(self) -> bytes:
        from autophone.device.uds import request
        from autophone.execution.engine.actions import resolve_sock

        try:
            response, _ = request(
                {"t": "screenshot", "color": True},
                sock_path=resolve_sock(),
                timeout=5,
            )
            image = response.get("image")
            if not image:
                raise DeviceApplicationError("no image from device")
            return base64.b64decode(image)
        except DeviceApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter errors become one application error
            raise DeviceApplicationError(f"device unreachable: {exc}") from exc


    def executor_preview(self) -> dict | None:
        """执行侧最近帧（BL-227）：本进程无活动 Stage 时返回 None，不访问设备。"""
        from autophone.device.executor_frames import preview_state
        from autophone.execution.engine.actions import resolve_sock

        return preview_state(resolve_sock())


class ForegroundApplicationError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ForegroundApplication:
    """Resolve an App profile and manage the foreground keeper target."""

    def __init__(
        self,
        *,
        keeper: Any,
        profile_loader: Any,
        default_device: str,
    ) -> None:
        self.keeper = keeper
        self.profile_loader = profile_loader
        self.default_device = default_device

    def status(self) -> dict:
        return self.keeper.status()

    def pin(self, payload: dict) -> dict:
        app_name = str(payload.get("app") or "").strip()
        if not app_name:
            raise ForegroundApplicationError(400, "app is required")
        mode = str(payload.get("mode") or "strong").strip().lower()
        if mode not in {"strong", "gentle"}:
            raise ForegroundApplicationError(
                400,
                "mode must be 'strong' or 'gentle'",
            )
        try:
            profile = self.profile_loader(app_name)
        except Exception as exc:  # noqa: BLE001 - profile errors become a stable contract
            raise ForegroundApplicationError(
                404,
                f"unknown app: {app_name}",
            ) from exc
        device = (
            str(payload.get("device") or self.default_device).strip()
            or self.default_device
        )
        raw_preview = (
            ((profile.raw.get("capabilities") or {}).get("launch") or {}).get(
                "springboard_icon_preview"
            )
        )
        icon_preview = None
        if isinstance(raw_preview, (list, tuple)) and len(raw_preview) == 2:
            try:
                icon_preview = (int(raw_preview[0]), int(raw_preview[1]))
            except (TypeError, ValueError):
                pass
        return self.keeper.pin(
            app=app_name,
            bundle_id=profile.bundle_id,
            device=device,
            springboard_icon_preview=icon_preview,
            mode=mode,
        )

    def clear(self) -> dict:
        return self.keeper.unpin()
