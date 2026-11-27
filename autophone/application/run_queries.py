"""Run queries and explicit telemetry refresh; execution remains behind injected ports."""

from __future__ import annotations
import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable
from autophone.application import run_index
from autophone.assets.apps import load_app_profile
from autophone.observation.artifacts import Artifact, append_artifacts
from autophone.contracts import ContractError, write_json
from autophone.execution.manual import load_manual_state
from autophone.observation.run_result import RunResultAssembler, phase2_result_from_json
from autophone.observation.telemetry import (
    collect_telemetry_record,
    load_telemetry_view,
)
from autophone.util import now_iso as _now

logger = logging.getLogger(__name__)


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _collect_duty_evidence(run_dir: Path) -> list[dict[str, Any]]:
    """Derive dashboard-ready source/transport/consume/business cards.

    The raw files remain the authority and every layer links back to them.  A
    transition is included only when the operation presented a QR generation;
    noop setup/teardown probes therefore do not look like scan evidence.
    """
    operations = run_dir / "operations"
    if not operations.is_dir():
        return []
    collected: list[dict[str, Any]] = []
    for output_path in sorted(operations.glob("**/operation-executor-output.json")):
        op_dir = output_path.parent
        request_path = op_dir / "vcam-control" / "camera-present-request.json"
        receipt_path = op_dir / "vcam-control" / "camera-transport-receipt.json"
        frame_path = op_dir / "camera-frame.png"
        restaurant_path = op_dir / "restaurant-emulator.json"
        if not request_path.exists() or not frame_path.exists():
            continue

        output = _json_object(output_path)
        duty = output.get("duty") if isinstance(output.get("duty"), dict) else {}
        evidence = (
            duty.get("evidence") if isinstance(duty.get("evidence"), dict) else {}
        )
        request_doc = _json_object(request_path)
        receipt = _json_object(receipt_path)
        transport = receipt.get("transport_receipt")
        transport = transport if isinstance(transport, dict) else {}
        generation = str(request_doc.get("generation") or "")
        published = transport.get("vphoned_published_frame_index")
        observed = transport.get("libvcam_observed_frame_index")
        transport_ok = bool(
            receipt.get("ok") is True
            and receipt.get("generation") == generation
            and receipt.get("protocol_version") == 2
            and published is not None
            and published == observed
        )

        probes = sorted((op_dir / "duty-readback").glob("probe-*.json"))
        final_probe = _json_object(probes[-1]) if probes else {}
        readback = final_probe.get("readback")
        readback = readback if isinstance(readback, dict) else {}
        outcome = str(duty.get("outcome") or "")
        transitioned = outcome == "passed" and duty.get("from") != duty.get("to")
        if outcome == "noop":
            # Old runs reused one operation directory for setup and teardown.
            # A teardown noop could overwrite the setup verdict while leaving
            # its QR files behind; that mixed directory is not attributable
            # four-layer evidence and must not render as a false red card.
            continue
        # Older raw outputs predate evidence persistence. Their passed
        # transition still came from DutyStateController's four-layer verdict;
        # use that explicit outcome as a backwards-compatible fallback.
        consume_ok = evidence.get("consume") is True or (
            "consume" not in evidence and transitioned
        )
        business_ok = evidence.get("business") is True or (
            "business" not in evidence
            and transitioned
            and final_probe.get("ok") is True
            and readback.get("state") == duty.get("to")
        )
        source_ok = bool(
            evidence.get("source") is True
            or (
                restaurant_path.exists()
                and frame_path.exists()
                and generation.startswith("qr-")
            )
        )

        def rel(path: Path) -> str:
            return str(path.relative_to(run_dir))

        store = _json_object(restaurant_path).get("store_code")
        layers = [
            {
                "name": "source",
                "status": "pass" if source_ok else "fail",
                "summary": f"store {store or '—'} · generation {generation or '—'}",
                "artifacts": [rel(restaurant_path), rel(frame_path), rel(request_path)],
            },
            {
                "name": "transport",
                "status": "pass" if transport_ok else "fail",
                "summary": f"generation {generation or '—'} · frame {published}/{observed}",
                "artifacts": [rel(receipt_path)],
            },
            {
                "name": "consume",
                "status": "pass" if consume_ok else "fail",
                "summary": f"scanner verdict {outcome or 'unknown'}",
                "artifacts": [rel(output_path)],
            },
            {
                "name": "business",
                "status": "pass" if business_ok else "fail",
                "summary": (
                    f"{duty.get('from') or 'unknown'}→{duty.get('to') or 'unknown'}"
                    f" · work_status={readback.get('work_status', '—')}"
                    f" · work_type={readback.get('work_type', '—')}"
                ),
                "artifacts": ([rel(probes[-1])] if probes else []) + [rel(output_path)],
            },
        ]
        collected.append(
            {
                "operation": op_dir.name,
                "path": str(op_dir.relative_to(operations)),
                "generation": generation,
                "layers": layers,
            }
        )
    return collected


class RunQueries:
    """Run history and evidence projections, with live state and persistence ports."""

    def __init__(
        self,
        run_root: Path,
        *,
        active_runs: Callable[[], dict[str, Any]],
        persist_result: Callable[[Path, dict[str, Any]], Path],
    ) -> None:
        self.run_root = run_root
        self.run_catalog = run_index.RunCatalog(run_root)
        self._active_runs = active_runs
        self.persist_run_result = persist_result
        self._result_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._result_cache_lock = threading.Lock()

    def history(
        self,
        *,
        limit: int = 20,
        app: str | None = None,
        status: str | None = None,
        type: str | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        # active（在跑）run 只在首页合并（before is None）；翻页只走已终态的索引游标。
        if before is None:
            active_items = list(self._active_runs().values())
            for active in active_items:
                run_dir = Path(str(active["run_dir"]))
                manual = load_manual_state(run_dir)
                run_status = (
                    "manual_pending"
                    if (manual or {}).get("checkpoint", {}).get("status") == "waiting"
                    else active["status"]
                )
                # 过滤对 active 同样生效，保持列表语义一致（status='failed' 是终态聚合，active 不入）。
                if app and active.get("app") != app:
                    continue
                if type and active.get("type") != type:
                    continue
                if status == "failed" or (status and status != run_status):
                    continue
                items.append(
                    {
                        "run_id": active["run_id"],
                        "status": run_status,
                        "app": active.get("app"),
                        "device": active.get("device"),
                        "type": active.get("type"),
                        "queued_at": active.get("queued_at"),
                        "ended_at": None,
                        "stat": None,
                        "run_dir": str(run_dir),
                        "report_html": None,
                        "active": True,
                        "mtime": run_dir.stat().st_mtime if run_dir.exists() else 0,
                    }
                )
        try:
            items.extend(
                run_index.history(
                    self.run_root,
                    limit=limit,
                    app=app,
                    status=status,
                    type=type,
                    before_mtime=before,
                )
            )
        except Exception:
            logger.warning("run_index history 不可用，回落文件扫描", exc_info=True)
            # 先按 mtime 排序，只解析最新 limit 份；其余目录连 JSON 都不读。
            candidates: list[tuple[float, Path]] = []
            for result_path in self.run_root.glob("*/*/platform-run-result.json"):
                try:
                    candidates.append((result_path.stat().st_mtime, result_path))
                except OSError:
                    continue
            candidates.sort(key=lambda item: item[0], reverse=True)
            failed_set = {"failed", "error", "blocked"}
            taken = 0
            for mtime, result_path in candidates:
                if taken >= limit:
                    break
                mtime_ns = int(mtime * 1_000_000_000)
                if before is not None and mtime_ns >= int(before):
                    continue
                result = self._read_result_cached(result_path)
                if result is None:
                    continue
                run_status = result.get("status")
                run_app = result.get("app")
                run_type = result.get("type")
                if app and run_app != app:
                    continue
                if type and run_type != type:
                    continue
                if status == "failed":
                    if run_status not in failed_set:
                        continue
                elif status and run_status != status:
                    continue
                run_dir = result_path.parent
                cases = result.get("cases") or []
                stat = None
                if cases:
                    stat = {
                        "total": len(cases),
                        "passed": sum(1 for c in cases if c.get("status") == "passed"),
                        "failed": sum(1 for c in cases if c.get("status") == "failed"),
                        "error": sum(1 for c in cases if c.get("status") == "error"),
                        "blocked": sum(
                            1 for c in cases if c.get("status") == "blocked"
                        ),
                    }
                items.append(
                    {
                        "run_id": result.get("run_id") or run_dir.name,
                        "status": run_status,
                        "app": run_app,
                        "device": result.get("device"),
                        "type": run_type,
                        "day": run_dir.parent.name,
                        "appv": (
                            (result.get("package") or {}).get("device_version")
                            or (result.get("package") or {}).get("default_version")
                            or (result.get("package") or {}).get("active_version")
                            or result.get("app_version")
                        ),
                        "queued_at": (result.get("timestamps") or {}).get("queued_at"),
                        "ended_at": (result.get("timestamps") or {}).get(
                            "collect_ended_at"
                        ),
                        "stat": stat,
                        "run_dir": str(run_dir),
                        "report_html": str(run_dir / "report.html"),
                        "mtime": mtime,
                        "mtime_ns": mtime_ns,
                    }
                )
                taken += 1
        items.sort(key=lambda item: float(item.get("mtime") or 0), reverse=True)
        # `limit` applies to settled history from the index/fallback scan. Active
        # runs are an extra live section in the reports UI and must not consume
        # the terminal page size, otherwise the first page can look exhausted
        # whenever anything is currently running.
        return items

    def app_trend(self, app: str, *, limit: int = 20) -> dict[str, Any]:
        """某 app 最近 `limit` 份回归 run 的执行耗时跨 run 趋势（trace P3）。

        扫 mtime 倒序的 regression run，对每份拼一个**轻量 detail**（只读 platform-run-result.json
        ＋ result.json 两个文件，不走重的 detail()）→ build_trace → 汇进 build_trend。
        只看终态 run（有 result.json，含逐用例 duration_ms/actions）。无任何可用 run → 空趋势。
        """
        from autophone.execution.run_trace import build_trace, build_trend

        traces: list[dict[str, Any]] = []
        for fact in self.run_catalog.runs(
            run_index.RunQuery(
                app=app,
                types=frozenset({"regression"}),
            )
        ):
            run_dir = fact.run_dir
            if run_index.is_negative_control_run(run_dir):
                continue  # 区分力反向 Run 执行扰动后的 Recipe，不计入回归耗时趋势
            phase2_path = run_dir / "result.json"
            if not phase2_path.exists():
                continue  # 仍在跑或未收口，无逐用例 timing
            try:
                phase2 = json.loads(phase2_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            traces.append(
                build_trace(
                    {
                        "run_id": fact.result.get("run_id") or run_dir.name,
                        "result": fact.result,
                        "phase2": phase2,
                    }
                )
            )
            if len(traces) >= limit:
                break
        return build_trend(traces, app=app)

    def latest_case_verdicts(
        self, app: str, *, limit: int = 50
    ) -> dict[str, dict[str, Any]]:
        """各用例**最近一次** run 的裁决（喂 `exceptions.derive_exceptions` 的 failure kind）。

        扫最近 `limit` 份 run 结果（按 mtime 倒序），只取每个 case_id 第一次出现（即最新）的
        per-case status。只看 type=regression 的真机回归（验证单/operation 不进 case 失败视图）。
        返回 {case_id -> {status, run_id, ended_at, item, build_id, build_label, build_identity_match}}；
        构建字段取自该 Run 结果冻结的构建身份（见 `run_index.run_build_identity`），缺失为 None。
        """
        return self.run_catalog.case_verdicts(app, limit=limit)

    def latest_case_api_capture(
        self, app: str, case_id: str, *, limit: int = 50, allow_raw: bool = False
    ) -> dict[str, Any]:
        """某用例**最近一次**回归 run 抓到的接口数据（按 case_id 归因过滤）。供用例编辑页就地查看。

        扫最近 `limit` 份 run 结果（mtime 倒序），取第一份 `planes/api/capture.jsonl` 里有该
        case_id 归因调用的，过滤出该用例的分组返回（含 run_id/ended_at 溯源，replay 仍按 run_id+idx）。
        无任何归因抓取 → 空态 `available=False`（与 run 抓取面同口径，缺即空、不崩）。
        """
        from autophone.observation.api_capture import CAPTURE_REL, load_capture

        empty = {
            "available": False,
            "allow_raw": allow_raw,
            "case_id": case_id,
            "run_id": None,
            "ended_at": None,
            "total": 0,
            "groups": [],
        }
        for fact in self.run_catalog.runs(
            run_index.RunQuery(
                app=app,
                types=frozenset({"regression"}),
                candidate_limit=limit,
            )
        ):
            run_dir = fact.run_dir
            if not (run_dir / CAPTURE_REL).exists():
                continue
            cap = load_capture(run_dir, allow_raw=allow_raw)
            groups = [g for g in cap.get("groups", []) if g.get("case_id") == case_id]
            if not groups:
                continue
            return {
                "available": True,
                "allow_raw": allow_raw,
                "case_id": case_id,
                "run_id": fact.result.get("run_id") or run_dir.name,
                "ended_at": (fact.result.get("timestamps") or {}).get(
                    "collect_ended_at"
                ),
                "total": sum(int(g.get("count") or 0) for g in groups),
                "groups": groups,
            }
        return empty

    def _read_result_cached(self, path: Path) -> dict[str, Any] | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        with self._result_cache_lock:
            cached = self._result_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        with self._result_cache_lock:
            self._result_cache[key] = (mtime, data)
        return data

    def find_run_dir(self, run_id: str) -> Path:
        active = self._active_runs().get(run_id)
        if active is not None:
            run_dir = Path(str(active["run_dir"]))
            if run_dir.exists():
                return run_dir
        matches = sorted(
            [*self.run_root.glob(f"*/{run_id}"), *self.run_root.glob(f"*/{run_id}.attempt-*")],
            key=lambda p: ((p / 'platform-run-result.json').stat().st_mtime
                           if (p / 'platform-run-result.json').exists() else p.stat().st_mtime),
            reverse=True,
        )
        if not matches:
            raise FileNotFoundError(f"run not found: {run_id}")
        return matches[0]

    def detail(self, run_id: str) -> dict[str, Any]:
        from autophone.execution.closure_journal import ClosureJournal
        run_dir = self.find_run_dir(run_id)
        spec_path = run_dir / "run-spec.json"
        result_path = run_dir / "platform-run-result.json"
        manifest_path = run_dir / "artifact-manifest.json"
        phase2_path = run_dir / "result.json"
        verification_path = run_dir / "verification-result.json"
        operation_path = run_dir / "operation-result.json"
        scenario_path = run_dir / "scenario-evidence.json"
        progress_path = run_dir / "run-progress.json"
        active = dict(self._active_runs().get(run_id) or {})
        result = (
            json.loads(result_path.read_text(encoding="utf-8"))
            if result_path.exists()
            else None
        )
        operation = (
            json.loads(operation_path.read_text(encoding="utf-8"))
            if operation_path.exists()
            else None
        )
        run_spec = (
            json.loads(spec_path.read_text(encoding="utf-8"))
            if spec_path.exists()
            else None
        )
        # 旧 operation run 的 S1/S2… 行没有 item。读取时以同 run 的 operation-result
        # 补齐可读标题，不改写历史产物，也不要求重新驱动设备。
        if isinstance(result, dict) and result.get("type") == "operation":
            result_cases = result.get("cases") or []
            operation_steps = (operation or {}).get("steps") or []
            result_steps = result.get("steps") or []
            for index, case in enumerate(result_cases):
                if not isinstance(case, dict) or case.get("item"):
                    continue
                op_step = operation_steps[index] if index < len(operation_steps) else {}
                run_step = result_steps[index] if index < len(result_steps) else {}
                case["item"] = str(
                    (op_step.get("text") if isinstance(op_step, dict) else "")
                    or (run_step.get("label") if isinstance(run_step, dict) else "")
                    or f"步骤 {index + 1}"
                )
        detail: dict[str, Any] = {
            "run_id": run_id,
            "closure": ClosureJournal(run_dir).projection(),
            "run_dir": str(run_dir),
            "store_context": (
                copy.deepcopy(run_spec.get("store_context"))
                if isinstance(run_spec, dict)
                and isinstance(run_spec.get("store_context"), dict)
                else None
            ),
            "result": result,
            # 执行期逐用例进度（runner 边界写）。终态有 result.cases 后前端以那个为准；
            # 缺 result 时（run 仍在跑）左栏靠它展示哪条过/败/在跑。
            "progress": json.loads(progress_path.read_text(encoding="utf-8"))
            if progress_path.exists()
            else None,
            "manifest": json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else None,
            "phase2": json.loads(phase2_path.read_text(encoding="utf-8"))
            if phase2_path.exists()
            else None,
            "verification": json.loads(verification_path.read_text(encoding="utf-8"))
            if verification_path.exists()
            else None,
            "operation": operation,
            "scenario": json.loads(scenario_path.read_text(encoding="utf-8"))
            if scenario_path.exists()
            else None,
            "duty_evidence": _collect_duty_evidence(run_dir),
            "active": active or None,
            "manual": load_manual_state(run_dir),
            "artifacts": {
                "report_html": "report.html"
                if (run_dir / "report.html").exists()
                else None,
                "coverage_html": "coverage.html"
                if (run_dir / "coverage.html").exists()
                else None,
                "coverage_json": "coverage.json"
                if (run_dir / "coverage.json").exists()
                else None,
                "run_log": "run.log" if (run_dir / "run.log").exists() else None,
                "run_events": "events.jsonl"
                if (run_dir / "events.jsonl").exists()
                else None,
                "run_failure": "run-failure.json"
                if (run_dir / "run-failure.json").exists()
                else None,
                "telemetry_events": "planes/telemetry/events.json"
                if (run_dir / "planes" / "telemetry" / "events.json").exists()
                else None,
                "api_capture": "planes/api/capture.jsonl"
                if (run_dir / "planes" / "api" / "capture.jsonl").exists()
                else None,
                "recording": "recording.mp4"
                if (run_dir / "recording.mp4").exists()
                else None,
                "verification_result": "verification-result.json"
                if (run_dir / "verification-result.json").exists()
                else None,
                "verification_report": "verification-report.html"
                if (run_dir / "verification-report.html").exists()
                else None,
                "operation_result": "operation-result.json"
                if (run_dir / "operation-result.json").exists()
                else None,
                "operation_report": "operation-report.html"
                if (run_dir / "operation-report.html").exists()
                else None,
                "scenario_evidence": "scenario-evidence.json"
                if scenario_path.exists()
                else None,
                "ai_diagnosis_json": "ai-diagnosis.json"
                if (run_dir / "ai-diagnosis.json").exists()
                else None,
                "ai_diagnosis_html": "ai-diagnosis.html"
                if (run_dir / "ai-diagnosis.html").exists()
                else None,
                "ai_fix_proposal": "ai-fix-proposal.json"
                if (run_dir / "ai-fix-proposal.json").exists()
                else None,
            },
        }
        return detail

    def api_capture(self, run_id: str, *, allow_raw: bool = False) -> dict[str, Any]:
        """run 的 API 抓取面（planes/api/capture.jsonl）→ 脱敏 + 按动作分组。缺文件即空态。"""
        from autophone.observation.api_capture import load_capture

        run_dir = self.find_run_dir(run_id)
        result = load_capture(run_dir, allow_raw=allow_raw)
        result["run_id"] = run_id
        return result

    def telemetry(self, run_id: str) -> dict[str, Any]:
        """读取 run 已落盘的脱敏行为轨迹；GET 不触发后端查询。"""

        return load_telemetry_view(self.find_run_dir(run_id))

    def latest_case_telemetry(
        self,
        app: str,
        case_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """返回某用例最近一份有事件或断言证据的脱敏 telemetry 轨迹。"""

        for fact in self.run_catalog.runs(
            run_index.RunQuery(
                app=app,
                types=frozenset({"regression"}),
                candidate_limit=limit,
            )
        ):
            view = load_telemetry_view(fact.run_dir, case_id=case_id)
            if not view.get("available"):
                continue
            if not view.get("event_count") and not view.get("assertions"):
                continue
            view["run_id"] = fact.result.get("run_id") or fact.run_dir.name
            view["ended_at"] = (fact.result.get("timestamps") or {}).get(
                "collect_ended_at"
            )
            view["case_id"] = case_id
            return view
        return {
            "available": False,
            "app": app,
            "case_id": case_id,
            "run_id": None,
            "ended_at": None,
            "event_count": 0,
            "groups": [],
            "assertions": [],
        }

    def refresh_telemetry(
        self,
        run_id: str,
        *,
        actor: str = "web",
        data_trace_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """按原 run 身份和执行窗口重新 readback，并原子刷新裁决与索引。"""

        if run_id in self._active_runs():
            raise ContractError("active run telemetry cannot be refreshed")
        run_dir = self.find_run_dir(run_id)
        if (run_dir / 'closure-intent.json').exists() or (run_dir / 'closure-intent.quarantine.json').exists():
            raise ContractError('closure-managed run evidence is immutable; start a new run for telemetry refresh')
        spec = self._read_result_cached(run_dir / "run-spec.json")
        platform_result = self._read_result_cached(run_dir / "platform-run-result.json")
        phase2_json = self._read_result_cached(run_dir / "result.json")
        if not isinstance(spec, dict) or not isinstance(platform_result, dict):
            raise ContractError(
                "run telemetry refresh requires run spec and platform result"
            )
        if not isinstance(phase2_json, dict):
            raise ContractError("run telemetry refresh requires phase2 result")
        if spec.get("type") != "regression":
            raise ContractError("only regression run telemetry can be refreshed")
        profile = load_app_profile(
            str(spec.get("app") or platform_result.get("app") or "")
        )
        if not ((profile.raw.get("capabilities") or {}).get("telemetry") or {}).get(
            "backend_query"
        ):
            raise ContractError("app does not declare telemetry backend query")
        phase2_result = phase2_result_from_json(phase2_json)
        old_summary_path = run_dir / "planes" / "telemetry" / "query-summary.json"
        try:
            old_summary = json.loads(old_summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old_summary = {}
        previous_refreshes = old_summary.get("refreshes") or []
        _, telemetry = collect_telemetry_record(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile.raw,
            phase2_result=phase2_result,
            timestamps=platform_result.get("timestamps") or {},
            cases_path=profile.cases_path(),
            data_trace_client=data_trace_client,
            sleep=sleep,
        )
        refreshed_at = _now()
        try:
            summary = json.loads(old_summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        summary["refreshes"] = [
            *[item for item in previous_refreshes if isinstance(item, dict)],
            {
                "actor": str(actor or "web")[:100],
                "refreshed_at": refreshed_at,
            },
        ]
        write_json(old_summary_path, summary)

        result = RunResultAssembler().refresh_telemetry(
            spec,
            phase2_result,
            platform_result,
            telemetry,
        )
        self.persist_run_result(run_dir, result)
        manifest_path = run_dir / "artifact-manifest.json"
        if manifest_path.exists():
            append_artifacts(
                run_dir,
                [
                    Artifact("platform-run-result.json", "report"),
                    Artifact("planes/telemetry/events.json", "telemetry_events"),
                    Artifact(
                        "planes/telemetry/raw.json",
                        "telemetry_raw",
                        sensitivity="sensitive",
                        redaction_status="raw",
                        retention="local_only",
                        share_policy="forbidden",
                    ),
                    Artifact(
                        "planes/telemetry/query-summary.json",
                        "telemetry_query_summary",
                    ),
                ],
            )
        view = load_telemetry_view(run_dir)
        view["refreshed_at"] = refreshed_at
        view["refreshed_by"] = str(actor or "web")[:100]
        return view

    def replay_api_call(
        self, run_id: str, idx: int, *, allow_raw: bool = False
    ) -> dict[str, Any]:
        """重放 run 抓取面里第 idx 条 API 调用（host 侧重发原始请求）。外发动作，由前端显式触发。"""
        from autophone.observation.api_capture import replay_call

        run_dir = self.find_run_dir(run_id)
        result = replay_call(run_dir, idx, allow_raw=allow_raw)
        result["run_id"] = run_id
        return result

    def verification_runs(self, *, draft_id: str | None = None) -> list[dict[str, Any]]:
        return self.run_catalog.promotion_runs(
            run_index.PromotionSubject(
                kind="verification",
                asset_id=draft_id,
            )
        )
