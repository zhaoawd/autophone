"""Project child captures, verified response/OCR comparisons and advisory pixels.

Legacy captures still provide endpoint presence. Stronger conclusions require
frozen baseline sources verified by ``impact.comparison``.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from autophone.observation.api_capture import CAPTURE_REL
from autophone.observation.impact import comparison
from autophone.observation.impact import policy
from autophone.observation.impact import endpoints as endpoint_mod
from autophone.observation.impact import execution as execution_mod
from autophone.assets.screens import load_screen_asset, screen_by_id
from autophone.observation import visual_diff


RunDirResolver = Callable[[str], Path | None]


def _visual_target_id(target_id: str) -> str:
    import re
    return target_id if re.fullmatch(r'[A-Za-z0-9_-]+', target_id) else comparison.digest(target_id.encode())[:16]


def _reject_policy_visual(item: dict[str, Any], target_id: str, outdir: Path | None,
                          reason: str) -> None:
    """撤销本目标的分享图像和引用，保留明确的不可比较状态。"""
    item.pop('frames', None)
    item.pop('visual_score', None)
    item['visual_changed'] = None
    item['api_changed'] = None
    item['semantic_changed'] = None
    for kind in ('api', 'semantic'):
        item[f'{kind}_comparison'] = {'status': 'unavailable', 'reason': reason}
    if outdir is not None:
        frames = Path(outdir) / 'impact/frames'
        prefix = _visual_target_id(target_id)
        for path in frames.glob(f'{prefix}-*'):
            # 仅删除本模块生成的目标图像；不跟随目录或符号链接。
            if path.name in {f'{prefix}-current.jpg', f'{prefix}-diff.png'} or path.name.startswith(f'{prefix}-baseline.'):
                if path.is_file() or path.is_symlink():
                    path.unlink()


def _capture_records(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / CAPTURE_REL
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _record_endpoint(record: Mapping[str, Any]) -> str:
    try:
        return endpoint_mod.endpoint_key(
            str(record.get("method") or ""),
            str(record.get("url") or ""),
            host=str(record.get("host") or "") or None,
        )
    except endpoint_mod.EndpointNormalizeError:
        return ""


def _record_paths(record: Mapping[str, Any]) -> set[str]:
    text = record.get("resp_body_text")
    if not isinstance(text, str) or not text:
        return set()
    try:
        body = json.loads(text)
    except ValueError:
        return set()
    return {
        endpoint_mod.normalize_response_path(path)
        for path in endpoint_mod.response_paths(body)
    }


def _safe_checkpoint_frame(run_dir: Path, screen_id: str) -> Path | None:
    result_path = Path(run_dir) / "operation-result.json"
    try:
        doc = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    root = Path(run_dir).resolve()
    for step in reversed(doc.get("steps") or []):
        if not isinstance(step, dict):
            continue
        checkpoint = step.get("screen_checkpoint")
        if not isinstance(checkpoint, dict):
            continue
        if (
            checkpoint.get("status") != "passed"
            or str(checkpoint.get("matched") or "") != screen_id
            # 只出遥测的 draft checkpoint（BL-060）不作目标页证据。
            or checkpoint.get("enforcement") == "telemetry_only"
        ):
            continue
        ref = Path(str(checkpoint.get("shot") or ""))
        if (
            not ref.parts
            or ref.is_absolute()
            or ".." in ref.parts
            or ref.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
        ):
            continue
        candidate = (root / ref).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    return None


def _collect_visual(
    *,
    item: dict[str, Any],
    target_id: str,
    target: Mapping[str, Any],
    plan: Mapping[str, Any],
    run_dir_resolver: RunDirResolver,
    impact_run_dir: Path,
    screen_asset_path: Path,
    baseline_manifest_path: Path | None,
) -> None:
    screen_id = str(target.get("screen_id") or "")
    if not screen_id:
        return
    child_frame = None
    for child_run_id in reversed(item.get("evidence_runs") or []):
        try:
            child_dir = run_dir_resolver(str(child_run_id))
        except Exception:  # noqa: BLE001 evidence lookup is advisory
            child_dir = None
        if child_dir and plan.get("app") and plan.get("build"):
            try:
                comparison.operation_source(Path(child_dir), str(child_run_id), str(plan["app"]), str(plan["build"]))
            except (OSError, ValueError):
                child_dir = None
        if child_dir:
            child_frame = _safe_checkpoint_frame(Path(child_dir), screen_id)
        if child_frame:
            break
    if child_frame is None:
        item["ui_note"] = str(item.get("ui_note") or "") + "；child run 没有目标 Screen checkpoint 帧"
        return
    try:
        screen_asset = load_screen_asset(screen_asset_path)
        screen = screen_by_id(screen_asset, screen_id)
        if any(plan.get(k) and str(plan[k]) != str(screen_asset.get("meta", {}).get(k) or "") for k in ("app", "build")):
            raise comparison.EvidenceError("screen_asset_identity_mismatch")
    except Exception:  # noqa: BLE001 missing/stale visual assets stay advisory
        item["ui_note"] = str(item.get("ui_note") or "") + "；当前 build 的 Screen 资产不可用"
        return

    target_id = _visual_target_id(target_id)
    frame_dir = Path(impact_run_dir) / "impact" / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    current_rel = Path("impact/frames") / f"{target_id}-current.jpg"
    current_path = Path(impact_run_dir) / current_rel
    try:
        visual_diff.mask_sensitive_frame(child_frame, current_path, screen=screen)
    except Exception:  # noqa: BLE001 raw frame must not be copied if masking fails
        item["ui_note"] = str(item.get("ui_note") or "") + "；当前帧敏感区域遮罩失败，未进入报告"
        return
    item["frames"] = {"baseline": None, "current": current_rel.as_posix(), "diff": None}

    if baseline_manifest_path is None or not Path(baseline_manifest_path).is_file():
        item["ui_note"] = str(item.get("ui_note") or "") + "；当前 build 没有 baseline manifest"
        return
    try:
        baseline_manifest = visual_diff.load_manifest(Path(baseline_manifest_path))
        baseline_entry = visual_diff.frame_by_screen(baseline_manifest, screen_id)
        if baseline_entry is None:
            item["ui_note"] = str(item.get("ui_note") or "") + f"；screen={screen_id} 没有 baseline"
            return
        baseline_source = comparison.safe_file(Path(baseline_manifest_path).parent, str(baseline_entry["file"]))
        comparison.read(Path(baseline_manifest_path).parent, str(baseline_entry["file"]), baseline_entry["sha256"])
        if any(str(baseline_manifest.get(k) or "") != str(screen_asset.get("meta", {}).get(k) or "") for k in ("app", "build")):
            raise comparison.EvidenceError("visual_baseline_identity_mismatch")
        if not baseline_source.is_file():
            item["ui_note"] = str(item.get("ui_note") or "") + "；baseline frame 文件缺失"
            return
        baseline_rel = Path("impact/frames") / f"{target_id}-baseline{baseline_source.suffix}"
        baseline_copy = Path(impact_run_dir) / baseline_rel
        visual_diff.mask_sensitive_frame(baseline_source, baseline_copy, screen=screen)
        current_manifest = {
            "schema_version": "v1",
            "app": str(baseline_manifest.get("app") or ""),
            "build": str(baseline_manifest.get("build") or ""),
            "preview_size": list(baseline_manifest.get("preview_size") or [430, 932]),
            "frames": [{
                "screen_id": screen_id,
                "file": current_path.name,
                "sha256": visual_diff.sha256_file(current_path),
                "source_run_dir": "impact-child-operation",
                "captured_at": "runtime",
                "volatile_regions": list(baseline_entry.get("volatile_regions") or []),
            }],
        }
        diff_rel = Path("impact/frames") / f"{target_id}-diff.png"
        diff_path = Path(impact_run_dir) / diff_rel
        compared = visual_diff.compare_frame(
            screen_id=screen_id,
            screen_asset=screen_asset,
            baseline_manifest=baseline_manifest,
            current_manifest=current_manifest,
            baseline_dir=Path(baseline_manifest_path).parent,
            current_dir=current_path.parent,
            threshold=float(baseline_manifest.get("threshold") or visual_diff.DEFAULT_THRESHOLD),
            diff_path=diff_path,
        )
    except Exception:  # noqa: BLE001 visual evidence remains advisory
        item["ui_note"] = str(item.get("ui_note") or "") + "；visual diff 计算失败"
        return
    item["frames"]["baseline"] = baseline_rel.as_posix()
    if diff_path.is_file():
        item["frames"]["diff"] = diff_rel.as_posix()
    if compared.get("status") in {"changed", "no_change"}:
        item["visual_changed"] = compared.get("status") == "changed"
        grid = dict(compared.get("grid") or {})
        cells = int(grid.get("cols") or 0) * int(grid.get("rows") or 0)
        if cells:
            item["visual_score"] = float(compared.get("changed_blocks") or 0) / cells
        item["masked_regions"] = list(
            visual_diff.volatile_region_names(screen, baseline_entry)
        )


def collect(
    *,
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    run_dir_resolver: RunDirResolver,
    impact_run_dir: Path | None = None,
    screen_asset_path: Path | None = None,
    baseline_manifest_path: Path | None = None,
    ocr_runner: Callable | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge endpoint presence from child runs into conservative target evidence."""
    evidence = execution_mod.evidence(execution)
    plan_targets = {
        str(target.get("target_id") or ""): dict(target)
        for target in plan.get("targets") or []
    }
    cache: dict[str, list[dict[str, Any]]] = {}
    policy_failures: set[str] = set()

    for target_id, item in evidence.items():
        if not item.get("reached"):
            continue
        target = plan_targets.get(target_id) or {}
        try:
            _settings, frozen_screen = policy.verified(target, impact_run_dir)
        except (OSError, ValueError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, policy.PolicyError) else 'comparison_policy_unavailable'
            _reject_policy_visual(item, target_id, impact_run_dir, reason)
            policy_failures.add(target_id)
            continue
        target_screen_path = frozen_screen if frozen_screen is not None else screen_asset_path
        if impact_run_dir is not None and target_screen_path is not None and not plan.get("api_scenario"):
            _collect_visual(
                item=item,
                target_id=target_id,
                target=target,
                plan=plan,
                run_dir_resolver=run_dir_resolver,
                impact_run_dir=impact_run_dir,
                screen_asset_path=target_screen_path,
                baseline_manifest_path=baseline_manifest_path,
            )
        expected_endpoint = str(target.get("endpoint_key") or "")
        if not expected_endpoint:
            item["api_note"] = "计划目标没有 endpoint identity，无法从 child run 归集 API 证据"
            continue
        expected_paths = {
            endpoint_mod.normalize_response_path(path)
            for path in target.get("response_paths") or []
            if endpoint_mod.normalize_response_path(path)
        }
        matches: list[dict[str, Any]] = []
        observed_paths: set[str] = set()
        capture_runs = 0
        for child_run_id in item.get("evidence_runs") or []:
            child_run_id = str(child_run_id)
            if child_run_id not in cache:
                try:
                    child_dir = run_dir_resolver(child_run_id)
                except Exception:  # noqa: BLE001 evidence lookup is advisory
                    child_dir = None
                cache[child_run_id] = _capture_records(child_dir) if child_dir else []
            rows = cache[child_run_id]
            if rows:
                capture_runs += 1
            for record in rows:
                if record.get("noise") or _record_endpoint(record) != expected_endpoint:
                    continue
                matches.append(record)
                observed_paths.update(_record_paths(record))

        if not matches:
            item["api_note"] = (
                f"{capture_runs}/{len(item.get('evidence_runs') or [])} 个 child run 有 capture，"
                f"未命中目标 endpoint={expected_endpoint}"
            )
            continue
        item["api_observed"] = True
        statuses = [record.get("status") for record in matches if isinstance(record.get("status"), int)]
        if statuses:
            item["status"] = statuses[-1]
        if expected_paths:
            hit_paths = sorted(expected_paths & observed_paths)
            missing_paths = sorted(expected_paths - observed_paths)
            item["api_note"] = (
                f"Operation 窗口命中 endpoint {len(matches)} 次；"
                f"目标响应路径命中 {len(hit_paths)}/{len(expected_paths)}；"
                "当前响应只证明出现事实；before/after 结论见 api_comparison"
                + (f"；未观测路径={missing_paths}" if missing_paths else "")
            )
        else:
            item["api_note"] = (
                f"Operation 窗口命中 endpoint {len(matches)} 次；"
                "计划未限定响应路径，且当前响应只证明出现事实；before/after 结论见 api_comparison"
            )
    for target_id, item in evidence.items():
        if item.get("reached") and target_id not in policy_failures:
            comparison.collect(
                plan=plan, target=plan_targets.get(target_id) or {}, item=item,
                execution=execution, run_dir_resolver=run_dir_resolver,
                baseline_manifest_path=baseline_manifest_path,
                screen_asset_path=screen_asset_path, impact_run_dir=impact_run_dir,
                ocr_runner=ocr_runner,
            )
            # 比较读取期间冻结资产若发生变化，不保留先前生成的分享图像。
            try:
                policy.verified(plan_targets.get(target_id) or {}, impact_run_dir)
            except (OSError, ValueError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, policy.PolicyError) else 'comparison_policy_unavailable'
                _reject_policy_visual(item, target_id, impact_run_dir, reason)
    return evidence
