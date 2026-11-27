"""Pure Run result assembly over already collected evidence.

Callers supply the frozen package projection and execution evidence. This module
performs no filesystem access, device operations, collection, or persistence.
Both initial assembly and telemetry refresh use the same plane/status rules.
"""
from __future__ import annotations

import copy
from typing import Any

PLANES = ("ui", "function", "api", "telemetry")


def plane_mode(spec: dict[str, Any], plane: str) -> str:
    default = "required" if plane == "ui" else "disabled"
    return str(((spec.get("planes") or {}).get(plane) or {}).get("mode") or default)


def phase2_result_from_json(payload: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    outcomes = [
        SimpleNamespace(**item)
        for item in (payload.get("cases") or payload.get("outcomes") or [])
        if isinstance(item, dict)
    ]
    return SimpleNamespace(
        aborted=bool(payload.get("aborted")),
        abort_reason=str(payload.get("abort_reason") or ""),
        outcomes=outcomes,
        ax_backend=payload.get("ax_backend"),
    )


class RunResultAssembler:
    """Project execution evidence into a Run Result without mutating inputs."""

    def assemble(
        self,
        spec: dict[str, Any],
        phase2_result: Any,
        *,
        package: dict[str, Any],
        env: str | None = None,
        timestamps: dict[str, str],
        phase_status: dict[str, Any],
        telemetry: dict[str, Any] | None = None,
        case_meta: dict[str, dict[str, Any]] | None = None,
        ax_backend: str | None = None,
    ) -> dict[str, Any]:
        resolved_ax_backend = (
            ax_backend
            if ax_backend is not None
            else getattr(phase2_result, "ax_backend", None)
        )
        if resolved_ax_backend is None:
            resolved_ax_backend = spec.get("ax_backend")
        planes = self._top_planes(spec, phase2_result, ax_backend=resolved_ax_backend)
        self._apply_telemetry_plane(planes, telemetry)
        cases = [
            self._case_result(
                spec, outcome, case_meta,
                capture_skip_reason=getattr(phase2_result, "capture_skip_reason", None),
                ax_backend=resolved_ax_backend,
            )
            for outcome in phase2_result.outcomes
        ]
        self._apply_case_telemetry_planes(cases, spec, telemetry)
        result = {
            "schema_version": "v1",
            "run_id": spec["run_id"],
            "type": spec["type"],
            "app": spec["app"],
            "device": spec["device"],
            "package": package,
            "status": self._overall_status(phase2_result, planes),
            "planes": planes,
            "cases": cases,
            "artifacts": "artifact-manifest.json",
            "lock_phase": phase_status,
            "timestamps": timestamps,
        }
        if resolved_ax_backend is not None:
            result["ax_backend"] = resolved_ax_backend
        abort_reason = str(getattr(phase2_result, "abort_reason", "") or "")
        if phase2_result.aborted and abort_reason:
            # Run 级中止原因（BL-083）：此前只写引擎 result.json 与 ui plane detail。
            result["abort_reason"] = abort_reason
        if package.get("profile_build_label") is not None:
            result["build_label"] = package["profile_build_label"]
        if env is not None:
            result["env"] = env
        if spec.get("store_context") is not None:
            result["store_context"] = copy.deepcopy(spec["store_context"])
        snapshot = getattr(phase2_result, "rig_snapshot", None)
        if snapshot is not None:
            result["rig"] = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        from autophone.execution.contracts import failure_reasons
        reasons = failure_reasons(cases)
        if reasons:
            result["failure_reasons"] = reasons
        # 混跑分组（登录前置二分）：把组计划落顶层 + 逐 case 盖 group 标签，详情页据此按组分节
        # （同实时视图）。单遍跑（未拆组）phase2_result 无 groups → 不写，行为不变。
        groups = getattr(phase2_result, "groups", None)
        if groups:
            result["groups"] = groups
            gmap = getattr(phase2_result, "group_by_id", None) or {}
            for case in result["cases"]:
                label = gmap.get(str(case.get("id")))
                if label:
                    case["group"] = label
        return result

    def refresh_telemetry(
        self,
        spec: dict[str, Any],
        phase2_result: Any,
        previous_result: dict[str, Any],
        telemetry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Re-evaluate telemetry on a copy of a settled result without writing it."""
        result = copy.deepcopy(previous_result)
        planes = result.get("planes") or {}
        self._apply_telemetry_plane(planes, telemetry)
        cases = result.get("cases") or []
        self._apply_case_telemetry_planes(cases, spec, telemetry)
        result["planes"] = planes
        result["cases"] = cases
        result["status"] = self._overall_status(phase2_result, planes)
        from autophone.execution.contracts import failure_reasons

        reasons = failure_reasons(cases)
        if reasons:
            result["failure_reasons"] = reasons
        else:
            result.pop("failure_reasons", None)
        return result

    def _apply_telemetry_plane(self, planes: dict[str, Any], telemetry: dict[str, Any] | None) -> None:
        current = planes.get("telemetry")
        if not current or current.get("mode") == "disabled" or telemetry is None:
            return
        status = telemetry.get("status") or "unknown"
        count = telemetry.get("event_count") or 0
        assertion_counts = telemetry.get("assertion_counts") or {}
        if status == "captured":
            planes["telemetry"] = {
                "mode": current["mode"],
                "verdict": "captured",
                "detail": f"dataTrace captured {count} event(s)",
            }
            return
        if status in {"asserted", "assertion_failed"}:
            verdict = "fail" if assertion_counts.get("fail") else "pass"
            detail = f"telemetry assertions pass={assertion_counts.get('pass', 0)}, fail={assertion_counts.get('fail', 0)}"
            planes["telemetry"] = {"mode": current["mode"], "verdict": verdict, "detail": detail}
            return
        planes["telemetry"] = {
            "mode": current["mode"],
            "verdict": "pending",
            "detail": f"D1 record-only telemetry status={status}, events={count}",
        }


    def _apply_case_telemetry_planes(
        self,
        cases: list[dict[str, Any]],
        spec: dict[str, Any],
        telemetry: dict[str, Any] | None,
    ) -> None:
        mode = plane_mode(spec, "telemetry")
        if mode == "disabled" or telemetry is None:
            return
        by_case: dict[str, list[dict[str, Any]]] = {}
        for assertion in telemetry.get("assertions") or []:
            by_case.setdefault(str(assertion.get("case_id") or ""), []).append(assertion)
        for case in cases:
            assertions = by_case.get(str(case.get("id") or ""))
            if not assertions:
                continue
            if telemetry.get("status") not in {"asserted", "assertion_failed"}:
                case.setdefault("planes", {})["telemetry"] = {
                    "mode": mode,
                    "verdict": "pending",
                    "detail": "telemetry assertions are not yet proven",
                }
                continue
            fail_count = len([item for item in assertions if item.get("verdict") == "fail"])
            pass_count = len([item for item in assertions if item.get("verdict") == "pass"])
            case.setdefault("planes", {})["telemetry"] = {
                "mode": mode,
                "verdict": "fail" if fail_count else "pass",
                "detail": f"telemetry assertions pass={pass_count}, fail={fail_count}",
            }
            if mode == "required" and fail_count:
                case["status"] = "failed"


    def _empty_plane(self, mode: str) -> dict[str, str]:
        if mode == "disabled":
            return {"mode": mode, "verdict": "not_configured"}
        return {"mode": mode, "verdict": "pending", "detail": "collector not wired in Phase A"}


    def _top_planes(
        self,
        spec: dict[str, Any],
        phase2_result: Any,
        *,
        ax_backend: str | None = None,
    ) -> dict[str, Any]:
        planes = {plane: self._empty_plane(plane_mode(spec, plane)) for plane in PLANES}
        ui_mode = planes["ui"]["mode"]
        if phase2_result.aborted:
            planes["ui"] = {"mode": ui_mode, "verdict": "skipped", "detail": phase2_result.abort_reason}
            if ax_backend is not None:
                planes["ui"]["ax_backend"] = ax_backend
            return planes
        ui_statuses = [
            getattr(outcome, "ui_status", "") or outcome.status
            for outcome in (phase2_result.outcomes or [])
        ]
        if any(status in {"fail", "error"} for status in ui_statuses):
            planes["ui"] = {"mode": ui_mode, "verdict": "fail"}
        elif any(status in {"blocked", "skipped"} for status in ui_statuses):
            planes["ui"] = {"mode": ui_mode, "verdict": "skipped"}
        else:
            planes["ui"] = {"mode": ui_mode, "verdict": "pass"}
        if ax_backend is not None:
            planes["ui"]["ax_backend"] = ax_backend
        self._apply_api_plane(planes, spec, phase2_result.outcomes,
                              capture_skip_reason=getattr(phase2_result, "capture_skip_reason", None))
        return planes


    def _apply_api_plane(
        self, planes: dict[str, Any], spec: dict[str, Any], outcomes: list[Any],
        *, capture_skip_reason: str | None = None,
    ) -> None:
        """run 级 API 平面：聚合各用例挂在 outcome 上的 api_assertions / api_calls（capture 路径回填）。

        三态（advisory 抓取现为无条件常态）：
          · 有断言 → pass/fail（有 expectations 的用例）。
          · 无断言但抓到调用 → `captured`（advisory 旁路证据，不计 overall）。
          · 抓取面 fail-open 没采到 → pending，detail 优先用 capture_skip_reason（真实失败原因，
            见 _run_with_capture），拿不到才退回笼统猜测文案。
        `net_jsonl` 的有无即「finalize 跑没跑过」（默认 None，跑过即赋值）；不能用 api_calls 判，
        它 default_factory=list，没抓取也是 []，分不清「采到 0 条」与「根本没采」。"""
        mode = plane_mode(spec, "api")
        if mode == "disabled":
            return
        outcomes = outcomes or []
        missing_required = [
            str(getattr(outcome, "id", "") or "<unknown>")
            for outcome in outcomes
            if not (getattr(outcome, "api_assertions", None) or [])
        ]
        assertions = [a for o in outcomes for a in (getattr(o, "api_assertions", None) or [])]
        if assertions:
            if mode == "required" and missing_required:
                planes["api"] = {
                    "mode": mode,
                    "verdict": "pending",
                    "detail": (
                        "required API 无适用断言，Case: "
                        + ", ".join(missing_required)
                    ),
                }
                return
            fail = sum(1 for a in assertions if a.get("verdict") == "fail")
            planes["api"] = {
                "mode": mode,
                "verdict": "fail" if fail else "pass",
                "detail": f"api assertions pass={len(assertions) - fail}, fail={fail}",
            }
            return
        # net_jsonl 由 finalize_api_plane 给每个 outcome 赋值（默认 None）→ 它的有无即「抓取面跑没跑过」。
        # 用 api_calls 不行：它 default_factory=list，没抓取也是 []，分不清「采到 0 条」与「没采」。
        captured = any(getattr(o, "net_jsonl", None) for o in outcomes)
        if captured:
            total = sum(len(getattr(o, "api_calls", None) or []) for o in outcomes)
            if mode == "required":
                planes["api"] = {
                    "mode": mode,
                    "verdict": "pending",
                    "detail": f"已抓取 {total} 条归因接口调用，但无适用断言；required 未满足",
                }
            else:
                planes["api"] = {
                    "mode": mode,
                    "verdict": "captured",
                    "detail": f"已抓取 {total} 条归因接口调用（advisory · 无断言）",
                }
        else:
            reason = capture_skip_reason or "抓取面未就位（如 frida-server 隧道未起 127.0.0.1:27042）"
            missing_api_jv = any(
                isinstance(getattr(o, "joint_verdict", None), dict)
                and (
                    getattr(o, "joint_verdict", {}).get("fail_kind") == "api_evidence_missing"
                    or (getattr(o, "joint_verdict", {}).get("planes") or {}).get("api") == "missing"
                )
                for o in outcomes
            )
            planes["api"] = {
                "mode": mode,
                "verdict": "fail" if missing_api_jv else "pending",
                "detail": (
                    f"api_evidence_missing：{reason}"
                    if missing_api_jv
                    else f"未采到接口数据：{reason}"
                ),
            }


    def _case_result(
        self,
        spec: dict[str, Any],
        outcome: Any,
        case_meta: dict[str, dict[str, Any]] | None = None,
        *,
        capture_skip_reason: str | None = None,
        ax_backend: str | None = None,
    ) -> dict[str, Any]:
        planes = {plane: self._empty_plane(plane_mode(spec, plane)) for plane in PLANES}
        ui_mode = planes["ui"]["mode"]
        ui_status = getattr(outcome, "ui_status", "") or outcome.status
        verdict = {
            "pass": "pass",
            "fail": "fail",
            "error": "fail",
            "blocked": "skipped",
            "skipped": "skipped",
        }.get(ui_status, "fail")
        joint_verdict = getattr(outcome, "joint_verdict", None)
        ui_detail = outcome.detail or ""
        if ui_status == "pass" and outcome.status != "pass" and isinstance(joint_verdict, dict):
            ui_detail = ""
        planes["ui"] = {
            "mode": ui_mode,
            "verdict": verdict,
            **({"detail": ui_detail} if ui_detail else {}),
            **({"ax_backend": ax_backend} if ax_backend is not None else {}),
        }
        api_mode = plane_mode(spec, "api")
        asserts = getattr(outcome, "api_assertions", None) or []
        if api_mode != "disabled":
            if asserts:
                api_fail = sum(1 for a in asserts if a.get("verdict") == "fail")
                planes["api"] = {
                    "mode": api_mode,
                    "verdict": "fail" if api_fail else "pass",
                    "detail": f"api assertions pass={len(asserts) - api_fail}, fail={api_fail}",
                }
            elif getattr(outcome, "net_jsonl", None):
                # net_jsonl 有值即 finalize 跑过（抓到了）；api_calls 默认 [] 分不清「采到 0」与「没采」。
                calls = getattr(outcome, "api_calls", None) or []
                if api_mode == "required":
                    planes["api"] = {
                        "mode": api_mode,
                        "verdict": "pending",
                        "detail": (
                            f"已抓取 {len(calls)} 条接口调用，但无适用断言；"
                            "required 未满足"
                        ),
                    }
                else:
                    planes["api"] = {
                        "mode": api_mode,
                        "verdict": "captured",
                        "detail": (f"已抓取 {len(calls)} 条接口调用（advisory · 无断言）"
                                   if calls else "本用例无接口调用（已抓取 · advisory）"),
                    }
            else:
                # finalize 没跑过（抓取面 fail-open 退回）→ 诚实标「没采到」，detail 优先用真实
                # 失败原因（capture_skip_reason），拿不到才退回笼统猜测文案。
                reason = capture_skip_reason or "抓取面未就位（如 frida-server 隧道未起 127.0.0.1:27042）"
                jv_api_missing = (
                    isinstance(joint_verdict, dict)
                    and (
                        joint_verdict.get("fail_kind") == "api_evidence_missing"
                        or (joint_verdict.get("planes") or {}).get("api") == "missing"
                    )
                )
                planes["api"] = {
                    "mode": api_mode,
                    "verdict": "fail" if jv_api_missing else "pending",
                    "detail": (
                        f"api_evidence_missing：{reason}"
                        if jv_api_missing
                        else f"未采到接口数据：{reason}"
                    ),
                }
        entity_readback = getattr(outcome, "entity_readback", None)
        if isinstance(entity_readback, dict):
            rb_status = str(entity_readback.get("status") or "")
            rb_verdict = {
                "pass": "pass",
                "fail": "fail",
                "not_executed": "skipped",
            }.get(rb_status, "fail")
            rb_detail = str(entity_readback.get("detail") or "")
            planes["readback"] = {
                "mode": "required",
                "verdict": rb_verdict,
                **({"detail": rb_detail} if rb_detail else {}),
            }
        elif isinstance(joint_verdict, dict) and (joint_verdict.get("planes") or {}).get("readback") == "missing":
            planes["readback"] = {
                "mode": "required",
                "verdict": "fail",
                "detail": str(joint_verdict.get("detail") or "entity readback missing"),
            }
        result = {
            "id": outcome.id,
            "status": {
                "pass": "passed",
                "fail": "failed",
                "error": "error",
                "blocked": "blocked",
                "skipped": "skipped",
            }.get(outcome.status, "error"),
            "planes": planes,
        }
        if outcome.status in {"fail", "error", "blocked"} and getattr(outcome, "failure_stage", None):
            from autophone.execution.failure_context import failure_fields
            result.update(failure_fields(
                getattr(outcome, "failure_stage", None) or "not_recorded",
                getattr(outcome, "failed_definition", None),
            ))
        blocker = getattr(outcome, "blocker", None)
        if blocker:
            result["blocker"] = dict(blocker)
        # 用例名 / 模块 / 耗时 / 执行路径上提到 case 结果：前端实时视图左栏与摘要面需要这些
        # 才能显示「R63 我的业绩验证 · 3.3s · recipe」而非光秃秃的 ID。
        item = getattr(outcome, "item", "") or ""
        module = getattr(outcome, "module", "") or ""
        if item:
            result["item"] = item
        if module:
            result["module"] = module
        dur = getattr(outcome, "duration_ms", None)
        if isinstance(dur, (int, float)):
            result["duration_ms"] = dur
        exec_via = getattr(outcome, "exec_via", "") or ""
        if exec_via:
            result["exec_via"] = exec_via
        meta = (case_meta or {}).get(str(outcome.id))
        if meta and isinstance(meta.get("revision"), int):
            result["revision"] = meta["revision"]
        draft_recipe_identity = getattr(outcome, "draft_recipe_identity", None)
        if isinstance(draft_recipe_identity, dict):
            result["draft_recipe_identity"] = dict(draft_recipe_identity)
        recipe_identity = getattr(outcome, "recipe_identity", None)
        if isinstance(recipe_identity, dict):
            result["recipe_identity"] = dict(recipe_identity)
        binding_sha256 = getattr(outcome, "binding_sha256", None)
        if binding_sha256 is None and isinstance(recipe_identity, dict):
            binding_sha256 = recipe_identity.get("binding_sha256")
        if binding_sha256 is not None:
            result["binding_sha256"] = str(binding_sha256)
        operation_witnesses = getattr(outcome, "operation_witnesses", None)
        if operation_witnesses:
            result["operation_witnesses"] = copy.deepcopy(operation_witnesses)
        negative_control = getattr(outcome, "negative_control", None)
        if isinstance(negative_control, dict):
            result["negative_control"] = copy.deepcopy(negative_control)
        if isinstance(joint_verdict, dict):
            result["joint_verdict"] = copy.deepcopy(joint_verdict)
        return result

    def _overall_status(self, phase2_result: Any, planes: dict[str, Any]) -> str:
        from autophone.execution.contracts import reduce_regression_status

        outcomes = [
            {
                "status": {"pass": "passed", "fail": "failed"}.get(outcome.status, outcome.status),
                "blocker": getattr(outcome, "blocker", None),
            }
            for outcome in phase2_result.outcomes
        ]
        required = [plane for plane in planes.values() if plane["mode"] == "required"]
        return reduce_regression_status(
            base_rig_blocked=bool(phase2_result.aborted),
            orchestration_error=False,
            required_planes=required,
            cases=outcomes,
        )
