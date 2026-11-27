"""Replay batch orchestration with a single sequential worker per batch."""

from __future__ import annotations
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable
from autophone.record import replay_batch
from autophone.record import replay_timing
from autophone.assets.apps import load_app_profile
from autophone.contracts import ContractError
from autophone.util import now_iso as _now

logger = logging.getLogger(__name__)


class ReplayBatchService:
    """Ordered replay submission and persisted batch views over Run ports."""

    _BATCH_POLL_INTERVAL = 0.5

    def __init__(
        self,
        batches_root: Path,
        *,
        submit: Callable[..., Any],
        find_run_dir: Callable[[str], Path],
        is_active: Callable[[str], bool],
        human_holder: Callable[..., Any],
        wait_assignment: Callable[..., Any] | None = None,
    ) -> None:
        self.batches_root = batches_root
        self.submit = submit
        self.wait_assignment = wait_assignment
        self.find_run_dir = find_run_dir
        self._is_active = is_active
        self._human_holder = human_holder

    def submit_replay_batch(
        self,
        *,
        app: str,
        device: str,
        recording_ids: list[str],
        replay_speed: Any = None,
        telemetry_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提交一条多选批量回放：服务端解析 draft（不信前端）+ 起一个顺序 worker。

        **服务端**先把 ``recordings.list_recordings(app)`` 建成 ``recording_id -> row`` map，
        **再严格按请求数组 ``recording_ids`` 顺序**生成 items（批内顺序 = 用户选择顺序，
        不跟 ``list_recordings`` 的目录排序）。任一无 draft 或 ``draft.status∉{active,draft}``
        → 抛 ``ContractError`` fail-closed（不静默跳过、不产生半个批次）。
        """
        from autophone.record import recordings as recordings_mod

        from autophone.application.control_plane import configured_control_plane
        from autophone.execution.dispatch import validate_dispatch_telemetry
        if configured_control_plane() is not None:
            validate_dispatch_telemetry(telemetry_identity)
        load_app_profile(app)  # app 不存在直接抛
        try:
            speed = replay_timing.validate_speed(replay_speed)
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
        if not recording_ids:
            raise ContractError("recording_ids 不能为空")
        if telemetry_identity is not None:
            if not isinstance(telemetry_identity, dict) or set(telemetry_identity) != {
                "field",
                "value",
            }:
                raise ContractError(
                    "telemetry_identity must contain exactly field and value"
                )
            identity_field = str(telemetry_identity.get("field") or "").strip()
            identity_value = telemetry_identity.get("value")
            if identity_field not in {"phone_no", "device_id"}:
                raise ContractError(
                    "telemetry_identity field must be phone_no or device_id"
                )
            if not isinstance(identity_value, str) or not identity_value.strip():
                raise ContractError("telemetry_identity value is required")
            telemetry_identity = {
                "field": identity_field,
                "value": identity_value.strip(),
            }

        rows = {r["recording_id"]: r for r in recordings_mod.list_recordings(app)}
        items: list[dict[str, Any]] = []
        for rid in recording_ids:
            row = rows.get(rid)
            if row is None:
                raise ContractError(f"录制不存在：{rid}")
            draft = row.get("draft")
            if not draft or str(draft.get("status") or "") not in ("active", "draft"):
                raise ContractError(
                    f"录制 {rid} 无可回放 draft（需 active/draft，实为 {draft.get('status') if draft else None}）"
                )
            items.append(
                {
                    "recording_id": rid,
                    "draft_id": draft.get("id"),
                    "op_name": draft.get("name"),
                    "name": row.get("display_name") or row.get("name") or rid,
                    "run_id": None,
                    "status": "queued",
                    "note": None,
                }
            )

        batch_id = replay_batch.mint_batch_id(app)
        batch = replay_batch.new_batch(
            batch_id=batch_id, app=app, device=device, replay_speed=speed, items=items
        )
        replay_batch.write_batch(self.batches_root, batch)
        logger.info(
            "批量回放建档 %s app=%s device=%s 共 %d 条",
            batch_id,
            app,
            device,
            len(items),
        )
        threading.Thread(
            target=self._run_replay_batch,
            args=(batch_id, app, device, speed, items, telemetry_identity),
            name=f"batch-{batch_id}",
            daemon=True,
        ).start()
        # 立即返回早期 handle（全 queued）；前端打开批次视图后轮询。
        return {
            "batch_id": batch_id,
            "app": app,
            "device": device,
            "replay_speed": speed,
            "phase": "queued",
            "items": [dict(it) for it in items],
        }

    def _run_replay_batch(
        self,
        batch_id: str,
        app: str,
        device: str,
        speed: float,
        items: list[dict[str, Any]],
        telemetry_identity: dict[str, Any] | None = None,
    ) -> None:
        """批次 worker：顺序提交子 run，每条等 settled 再提下一条（同进程 daemon thread）。

        主体包 try/except/finally，**finally 必落 terminal_phase**（默认 stopped，防回写前
        崩溃误判 done）。正常跑完每条才 done；guard 命中 / 异常分支一律 stopped + stop_reason。
        """
        terminal_phase = "stopped"
        stop_reason: str | None = "worker-error"
        try:
            for idx, item in enumerate(items):
                # 每条提交前查询设备所属 Rig 的人工占用；兼容路径查本地锁。
                # 占用或查询失败时不铸造后续 Run ID，剩余项 blocked 并停止批次。
                if self._human_holder(device=device) is not None:
                    for it in items[idx:]:
                        if it["status"] == "queued":
                            it["status"] = "blocked"
                            it["note"] = "device-recording"
                    terminal_phase = "stopped"
                    stop_reason = "device-recording"
                    logger.warning(
                        "批量回放 %s 第 %d 条前命中录制占用，剩余标 blocked 停批",
                        batch_id,
                        idx,
                    )
                    return
                run_id = replay_batch.mint_sub_run_id(app, idx)
                item["run_id"] = run_id
                item["status"] = "running"
                self._persist_batch(
                    batch_id, items=items, phase="running", current_run_id=run_id
                )
                op: dict[str, Any] = {
                    "name": item["op_name"],
                    "with": {},
                    "draft_id": item["draft_id"],
                }
                if speed != 1.0:
                    op["replay_speed"] = speed
                spec = {
                    "schema_version": "v1",
                    "run_id": run_id,
                    "type": "operation",
                    "app": app,
                    "device": device,
                    "operation": op,
                    "requested_by": "web",
                }
                if telemetry_identity is None:
                    handle = self.submit(spec, background=True)
                else:
                    handle = self.submit(
                        spec,
                        background=True,
                        telemetry_identity=telemetry_identity,
                    )
                result = getattr(handle, "result", {}) or {}
                if result.get("assignment_id"):
                    item["assignment_id"] = result["assignment_id"]
                    settled = self.wait_assignment(
                        result, spec, on_tick=lambda: self._persist_batch(batch_id, items=items)
                    )
                    item["status"] = replay_batch.map_run_status(settled.result.get("status"))
                else:
                    item["status"] = self._wait_batch_settled(
                        run_id, on_tick=lambda: self._persist_batch(batch_id, items=items)
                    )
                self._persist_batch(batch_id, items=items, current_run_id=run_id)
            terminal_phase = "done"
            stop_reason = None
        except Exception as exc:  # noqa: BLE001 - worker 异常一律收口成 stopped，不留无限 running
            logger.exception("批量回放 worker 异常 %s", batch_id)
            terminal_phase = "stopped"
            stop_reason = getattr(exc, "code", "worker-error")
            for item in items:
                if item["status"] in {"running", "queued"}:
                    item["status"] = "error" if item.get("run_id") else "blocked"
                    item["note"] = stop_reason
        finally:
            self._persist_batch(
                batch_id,
                items=items,
                phase=terminal_phase,
                current_run_id=None,
                stop_reason=stop_reason,
            )

    def _wait_batch_settled(self, run_id: str, *, on_tick: Callable[[], None]) -> str:
        """阻塞等子 run settled：``platform-run-result.json`` 落盘 **且** run_id 不在 _active。

        每个 tick 刷 heartbeat（``on_tick``）。settled 后取平台 status：passed/failed/error
        照收，残留 pending → error。子 run 崩溃也由现有 ``_write_error_result`` 兜底落盘 +
        清 active → 必然 settled，轮询有界。
        """
        from autophone.record import recordings as recordings_mod

        while True:
            on_tick()
            try:
                run_dir = self.find_run_dir(run_id)
            except FileNotFoundError:
                run_dir = None
            if run_dir is not None:
                result_path = run_dir / "platform-run-result.json"
                if result_path.exists() and not self._is_active(run_id):
                    result = recordings_mod._read_json(result_path) or {}
                    return replay_batch.map_run_status(result.get("status"))
            time.sleep(self._BATCH_POLL_INTERVAL)

    def _persist_batch(
        self,
        batch_id: str,
        *,
        items: list[dict[str, Any]],
        phase: str | None = None,
        current_run_id: Any = replay_batch._UNSET,
        stop_reason: Any = replay_batch._UNSET,
    ) -> None:
        """worker 单写者：把本地 items + 可选 phase/current/stop_reason 写回，并刷新 heartbeat。"""
        batch = replay_batch.read_batch(self.batches_root, batch_id)
        if batch is None:
            return
        batch["items"] = items
        if phase is not None:
            batch["phase"] = phase
        if current_run_id is not replay_batch._UNSET:
            batch["current_run_id"] = current_run_id
        if stop_reason is not replay_batch._UNSET:
            batch["stop_reason"] = stop_reason
        batch["heartbeat_at"] = _now()
        batch["heartbeat_ts"] = time.time()
        replay_batch.write_batch(self.batches_root, batch)

    def _batch_result_status(self, run_id: str) -> str | None:
        """读子 run platform 终态供视图实时回填；无结果文件 → None（保留存储态）。"""
        from autophone.record import recordings as recordings_mod

        try:
            run_dir = self.find_run_dir(run_id)
        except FileNotFoundError:
            return None
        result_path = run_dir / "platform-run-result.json"
        if not result_path.exists():
            return None
        result = recordings_mod._read_json(result_path) or {}
        return replay_batch.map_run_status(result.get("status"))

    def replay_batch_detail(self, batch_id: str) -> dict[str, Any] | None:
        batch = replay_batch.read_batch(self.batches_root, batch_id)
        if batch is None:
            return None
        return replay_batch.build_view(
            batch,
            result_status_for=self._batch_result_status,
            is_active=self._is_active,
            now_ts=time.time(),
        )

    def replay_batches(self, *, app: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for bid in replay_batch.list_batch_ids(self.batches_root):
            batch = replay_batch.read_batch(self.batches_root, bid)
            if batch is None:
                continue
            if app is not None and batch.get("app") != app:
                continue
            out.append(
                replay_batch.build_view(
                    batch,
                    result_status_for=self._batch_result_status,
                    is_active=self._is_active,
                    now_ts=time.time(),
                )
            )
        out.sort(key=lambda b: str(b.get("created_at") or ""), reverse=True)
        return out
