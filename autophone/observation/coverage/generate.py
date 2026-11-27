"""Generate Phase 5 coverage.json from run artifacts.

The report is generated, not maintained. It deliberately distinguishes full
coverage from partial evidence: a lifecycle run with UI/verifier but no API
assertions remains `partial` with an `api_missing` gap.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autophone.execution.engine import cases_loader
from autophone.observation.coverage.html import write_coverage_html
from autophone.execution.fixtures.contract import FixtureContext, write_json
from autophone.execution.fixtures.contract import load_fixture_context
from autophone.execution.lifecycle.scenarios import SCENARIOS
from autophone.observation.verifier.verifier import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    VerifierResult,
    verifier_passed,
)

COVERAGE_SCHEMA = "autophone.coverage/1"


@dataclass
class CoverageCase:
    case_id: str
    automation: str
    module: str = ""
    item: str = ""
    order_type: str | None = None
    row: int | None = None
    status: str = "uncovered"       # pass | partial | fail | error | skipped | uncovered
    fixture_id: str | None = None
    ui_steps: int = 0
    api_calls: int = 0
    api_assertions: dict[str, int] = field(default_factory=dict)
    verifier: dict[str, int] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_case_metadata(path: Path) -> list[dict[str, Any]]:
    # 与 phase2 runner 共用同一份加载器（含 PyYAML 缺失的降级解析 + 警告），
    # 避免 fallback 字段集在两处各自演化。晚绑定取模块属性：本模块常被 run 中途首次
    # import，from-import 会把此刻（可能被测试替换过的）函数对象永久冻进本命名空间。
    return cases_loader.load_cases(path)


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be an object")
    return data


def _capture_line_count(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _base_gap(case: dict[str, Any]) -> str:
    auto = str(case.get("automation") or "")
    if auto == "needs_fixture":
        return "fixture_missing"
    if auto == "blocked_by_infra":
        reason = str(case.get("automation_reason") or "")
        if "实名" in reason or "活体" in reason:
            return "blocked_by_face_scan"
        return "blocked_by_infra"
    return "run_missing"


def _new_coverage_case(case: dict[str, Any], cases_path: Path) -> CoverageCase:
    cid = str(case.get("id") or "")
    gap = _base_gap(case)
    return CoverageCase(
        case_id=cid,
        automation=str(case.get("automation") or ""),
        module=str(case.get("module") or ""),
        item=str(case.get("item") or ""),
        order_type=case.get("order_type"),
        row=case.get("row"),
        gaps=[gap],
        source_refs=[f"{cases_path}:{cid}"],
    )


def _summarize_api_assertions(assertions: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for assertion in assertions:
        verdict = str(assertion.get("verdict") or assertion.get("status") or "unknown")
        counts[verdict] += 1
    return dict(counts)


def _apply_phase2_result(cases: dict[str, CoverageCase], phase2_result: dict[str, Any]) -> None:
    for outcome in phase2_result.get("cases", []) or []:
        if not isinstance(outcome, dict):
            continue
        cid = str(outcome.get("id") or "")
        cov = cases.get(cid)
        if cov is None:
            continue
        cov.status = str(outcome.get("status") or "unknown")
        cov.ui_steps = len(outcome.get("actions", []) or [])
        cov.api_calls = len(outcome.get("api_calls", []) or [])
        cov.api_assertions = _summarize_api_assertions(outcome.get("api_assertions", []) or [])
        cov.evidence_refs.extend(str(x) for x in outcome.get("shots", []) or [])
        if outcome.get("net_jsonl"):
            cov.evidence_refs.append(str(outcome["net_jsonl"]))
        cov.gaps = []
        if cov.status == "fail":
            cov.gaps.append("ui_action_fail")
        elif cov.status == "error":
            cov.gaps.append("ui_error")
        elif cov.status == "skipped":
            cov.gaps.append("script_missing")


def _verifier_by_state(lifecycle_result: dict[str, Any]) -> dict[str, VerifierResult]:
    out: dict[str, VerifierResult] = {}
    for item in lifecycle_result.get("verifier_results", []) or []:
        if not isinstance(item, dict):
            continue
        result = VerifierResult.from_dict(item)
        out[result.state] = result
    return out


def _verifier_summary(results: list[VerifierResult]) -> dict[str, int]:
    passed = sum(1 for result in results if verifier_passed(result))
    low = sum(1 for result in results if result.progressed and not verifier_passed(result))
    failed = sum(1 for result in results if not result.progressed)
    return {"pass": passed, "low_confidence": low, "fail": failed}


def _apply_fixture_context(cases: dict[str, CoverageCase], fx: FixtureContext) -> None:
    cov = cases.get(fx.case_id)
    if cov is None:
        return
    cov.fixture_id = fx.fixture_id
    cov.evidence_refs.extend(e.ref for e in fx.evidence if e.ref)
    if fx.status != "ready":
        cov.status = "fail" if fx.status == "fixture_fail" else "partial"
        cov.gaps = list(dict.fromkeys(cov.gaps + ["fixture_not_ready"] + list(fx.gaps or [])))


def _apply_lifecycle_result(
    cases: dict[str, CoverageCase],
    lifecycle_result: dict[str, Any],
    fixture: FixtureContext | None,
    capture_jsonl: Path | None,
) -> None:
    scenario_id = str(lifecycle_result.get("scenario_id") or "")
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        return
    verifier_by_state = _verifier_by_state(lifecycle_result)
    api_calls = _capture_line_count(capture_jsonl)
    lifecycle_api_assertions = _summarize_api_assertions(
        lifecycle_result.get("api_assertions", []) or []
    )
    lifecycle_status = str(lifecycle_result.get("status") or "unknown")
    fixture_id = str(lifecycle_result.get("fixture_id") or (fixture.fixture_id if fixture else ""))
    step_count = len(lifecycle_result.get("steps", []) or [])

    for cid, required_states in scenario.case_state_map.items():
        cov = cases.get(cid)
        if cov is None:
            continue
        required_results = [verifier_by_state[state] for state in required_states
                            if state in verifier_by_state]
        missing_states = [state for state in required_states if state not in verifier_by_state]
        cov.fixture_id = fixture_id or cov.fixture_id
        cov.ui_steps = step_count
        cov.api_calls = api_calls
        if lifecycle_api_assertions:
            cov.api_assertions = lifecycle_api_assertions
        cov.verifier = _verifier_summary(required_results)
        cov.evidence_refs.extend(
            ref for result in required_results for ref in result.evidence_refs if ref
        )
        if fixture:
            cov.evidence_refs.extend(e.ref for e in fixture.evidence if e.ref)

        gaps: list[str] = []
        if fixture is None:
            gaps.append("fixture_missing")
        elif fixture.status != "ready":
            gaps.append("fixture_not_ready")
        if lifecycle_status != "pass":
            gaps.append("ui_action_fail")
        if missing_states:
            gaps.append("verifier_missing")
        if any(not verifier_passed(result) for result in required_results):
            gaps.append("verifier_low_confidence")
        if not cov.api_assertions:
            gaps.append("api_missing")
        elif cov.api_assertions.get("fail", 0) > 0:
            gaps.append("api_fail")

        cov.gaps = list(dict.fromkeys(gaps))
        cov.status = "pass" if not cov.gaps else (
            "fail" if "ui_action_fail" in cov.gaps or "fixture_not_ready" in cov.gaps else "partial"
        )


def _aggregate_gaps(cases: list[CoverageCase]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for cov in cases:
        for gap in cov.gaps:
            grouped[gap].append(cov.case_id)
    return [
        {"reason": reason, "count": len(case_ids), "case_ids": sorted(case_ids)}
        for reason, case_ids in sorted(grouped.items())
    ]


def generate_coverage(
    *,
    cases_path: Path,
    run_id: str = "manual",
    fixture_context_path: Path | None = None,
    lifecycle_result_path: Path | None = None,
    phase2_result_path: Path | None = None,
    capture_jsonl: Path | None = None,
    case_docs: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    if case_docs is None:
        case_docs = load_case_metadata(cases_path)
    cases = {
        str(case.get("id") or ""): _new_coverage_case(case, cases_path)
        for case in case_docs
        if case.get("id")
    }

    phase2_result = _load_json(phase2_result_path)
    if phase2_result:
        _apply_phase2_result(cases, phase2_result)

    fixture = load_fixture_context(fixture_context_path) if fixture_context_path else None
    if fixture:
        _apply_fixture_context(cases, fixture)

    lifecycle_result = _load_json(lifecycle_result_path)
    if lifecycle_result:
        _apply_lifecycle_result(cases, lifecycle_result, fixture, capture_jsonl)

    case_list = [cases[cid] for cid in sorted(cases)]
    counts = Counter(cov.status for cov in case_list)
    return {
        "schema": COVERAGE_SCHEMA,
        "run_id": run_id,
        "cases_yaml": str(cases_path),
        "counts": dict(counts),
        "cases": [cov.to_dict() for cov in case_list],
        "gaps": _aggregate_gaps(case_list),
        "artifacts": {
            "fixture_context": str(fixture_context_path) if fixture_context_path else None,
            "lifecycle_result": str(lifecycle_result_path) if lifecycle_result_path else None,
            "phase2_result": str(phase2_result_path) if phase2_result_path else None,
            "capture_jsonl": str(capture_jsonl) if capture_jsonl else None,
        },
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate Phase 5 coverage.json")
    ap.add_argument("--cases", type=Path, required=True, help="case library YAML")
    ap.add_argument("--run-id", default="manual")
    ap.add_argument("--fixture-context", type=Path, default=None)
    ap.add_argument("--lifecycle-result", type=Path, default=None)
    ap.add_argument("--phase2-result", type=Path, default=None)
    ap.add_argument("--capture-jsonl", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("coverage.json"))
    ap.add_argument("--html", action="store_true", help="also write coverage.html next to coverage.json")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    coverage = generate_coverage(
        cases_path=args.cases,
        run_id=args.run_id,
        fixture_context_path=args.fixture_context,
        lifecycle_result_path=args.lifecycle_result,
        phase2_result_path=args.phase2_result,
        capture_jsonl=args.capture_jsonl,
    )
    out = args.out / "coverage.json" if args.out.exists() and args.out.is_dir() else args.out
    write_json(out, coverage)
    html_out = None
    if args.html:
        html_out = out.with_suffix(".html")
        write_coverage_html(coverage, html_out)
    print(json.dumps({
        "coverage": str(out),
        "html": str(html_out) if html_out else None,
        "counts": coverage["counts"],
        "gap_count": len(coverage["gaps"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
