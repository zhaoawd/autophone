"""ai_diagnose.py — AI-1 失败归因（platform-ai-capability §3.1）。

喂 run 的**净化包**（§5.2，raw/PII 默认不进）给本地 agent 做纯推理诊断，产出结构化
ai-diagnosis.json（贴合 autophone/_data/schemas/ai-diagnosis.v1.json）+ 独立 ai-diagnosis.html 面板，
并把这两件按治理登记进 manifest。

边界（roadmap §2.2/§五）：纯推理、tools=none、不碰真机、不进执行链；AI 产物是「带证据的假设」，
evidence_refs 必须引用净化包内真实存在的相对路径，provenance 由**我们**盖章（provider/model），
不让模型自报来源。
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, build_ai_context, invoke_and_persist
from autophone.application.ai_output_schema import drop_optional_nulls, strict_transport_schema
from autophone.observation.artifacts import Artifact, append_artifacts
from autophone.contracts import validate_or_raise, write_json
from autophone.util import read_json

# 喂给模型的「内容 schema」——只让它专注诊断字段；schema_version/run_id/provenance 由我们盖章后
# 再贴 strict ai-diagnosis.v1 校验。additionalProperties:false 防止模型塞出契约外的键。
_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # evidence_refs / confidence 必填（platform-skill-boundary.md §8.0）：advisory 产物要么带
    # 可核对的引用与把握度，要么不落盘。宿主随后再校验每条 ref ∈ 本次内联产物集合。
    "required": ["likely_root_cause", "evidence_refs", "confidence"],
    "properties": {
        "failed_case": {"type": ["string", "null"]},
        "failed_action": {"type": ["string", "null"]},
        "likely_root_cause": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "confidence": {"enum": ["low", "medium", "high"]},
    },
}
# 传输 schema：可选字段改为必填可空，满足 provider 结构化输出约束；回包删去 null 后再盖章并贴契约校验。
_MODEL_TRANSPORT_SCHEMA: dict[str, Any] = strict_transport_schema(_MODEL_SCHEMA)

_SYSTEM = (
    "你是 autophone 的失败归因助手。你**没有任何工具**，不要尝试运行命令或读文件——"
    "所有证据已内联在用户消息里，只基于这些文本推理，一轮直接输出贴合 schema 的 JSON。"
    "evidence_refs 必须是证据里出现过的相对路径，不臆造。"
    "证据不足就把 confidence 标 low 并在 likely_root_cause 里说明缺什么。"
)


def _failed_cases(run_result: dict[str, Any]) -> list[str]:
    cases = run_result.get("cases")
    if not isinstance(cases, list):
        return []
    return [str(c.get("id")) for c in cases if isinstance(c, dict) and c.get("status") not in (None, "passed")]


# 内联文本证据的尺寸闸：单文件 / 总量上限，避免把大 jsonl 塞爆 prompt。
_TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log", ".csv", ".yaml", ".yml"}
_PER_FILE_CAP = 20_000
_TOTAL_CAP = 100_000
# 随 prompt 附带的图片（净化包里 ai_policy 放行的截图）：优先未通过用例的截图，总数封顶。
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_IMAGES = 8


def _select_images(bundle_dir: Path, included: list[str], failed_cases: list[str]) -> list[str]:
    """从净化包已收的产物里挑图片（相对路径）。失败用例命中的截图排前，超过 _MAX_IMAGES 截断。

    只看 bundle 里实际存在的文件：治理（ai_policy）已在 build_ai_context 决定谁能进包，这里不再放宽。
    """
    images = [
        rel for rel in included
        if Path(rel).suffix.lower() in _IMAGE_SUFFIXES and (bundle_dir / rel).is_file()
    ]

    def rank(rel: str) -> tuple[int, str]:
        hit = any(cid and cid in Path(rel).name for cid in failed_cases)
        return (0 if hit else 1, rel)

    return sorted(images, key=rank)[:_MAX_IMAGES]


def _inline_context(bundle_dir: Path, included: list[str]) -> tuple[str, list[str], list[str]]:
    """把净化包里允许的**文本**证据内联成 prompt 片段。返回 (文本, 已内联, 未内联)。

    tools=none 是一轮纯推理、不给工具，所以证据必须内联——不能靠 --add-dir 让 agent 翻目录
    （实测会触发 agentic 循环拖到超时）。图片等二进制不内联（raw 图本就被治理挡在包外）。
    """
    inlined: list[str] = []
    omitted: list[str] = []
    chunks: list[str] = []
    total = 0
    for rel in included:
        if rel == "ai-context-manifest.json":
            continue
        src = bundle_dir / rel
        if src.suffix.lower() not in _TEXT_SUFFIXES or not src.is_file():
            omitted.append(rel)
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        if len(text) > _PER_FILE_CAP:
            text = text[:_PER_FILE_CAP] + "\n…(truncated)"
        if total + len(text) > _TOTAL_CAP:
            omitted.append(rel)
            continue
        total += len(text)
        inlined.append(rel)
        chunks.append(f"----- {rel} -----\n{text}")
    return "\n\n".join(chunks), inlined, omitted


def _build_prompt(
    run_result: dict[str, Any],
    context_text: str,
    omitted: list[str],
    knowledge_context: str = "",
    images: list[str] | None = None,
) -> str:
    failed = _failed_cases(run_result)
    lines = [
        "诊断这个 autophone run 为什么失败，定位到 case/action，给出最可能根因与下一步。",
        f"run 总状态：{run_result.get('status') or 'unknown'}；类型：{run_result.get('type') or '-'}；"
        f"app/device：{run_result.get('app') or '-'}/{run_result.get('device') or '-'}。",
    ]
    if failed:
        lines.append(f"未通过的用例：{', '.join(failed)}。")
    if images:
        lines.append("随消息附带的截图（按附带顺序，可在 evidence_refs 引用其相对路径）："
                     + ", ".join(images) + "。")
    if omitted:
        lines.append("注意：以下证据因脱敏治理或体积未纳入，缺它们就基于已有证据推理并在 confidence 反映："
                     + ", ".join(omitted) + "。")
    lines.append("evidence_refs 只能引用下面出现过的相对路径。")
    if knowledge_context:
        lines.append("同 Build、同 Selector 基线查询：\n" + knowledge_context)
    lines.append("\n===== 证据 =====\n" + (context_text or "(无可内联的文本证据)"))
    return "\n".join(lines)


def diagnose_run(
    run_dir: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    allow_raw: bool = False,
    include_images: bool = True,
    runner: CommandRunner = _subprocess_runner,
) -> dict[str, Any]:
    """对一个 run 做失败归因。返回 {ok, ...}；ok=False 时不落 ai-diagnosis.json（fail-closed）。

    图片：净化包按 ai_policy 放行的截图（默认只有 shots/* 声明 allowed）随 AiRequest.images 附给模型；
    include_images=False 关闭附图（治理判定不变，只是不附）。telemetry raw / api capture raw 仍由各自
    的 ai_policy 挡住，不随截图连带放行（人审方案 §6.1 / §7.3）。
    """
    run_result = read_json(run_dir / "platform-run-result.json") or {}
    run_id = str(run_result.get("run_id") or run_dir.name)

    bundle = build_ai_context(run_dir, allow_raw=allow_raw)
    context_text, inlined, omitted = _inline_context(bundle.bundle_dir, bundle.included)
    images = _select_images(bundle.bundle_dir, bundle.included, _failed_cases(run_result)) if include_images else []
    # 已附带的图片不算「未纳入」；未被选中的图片仍留在 omitted 里让模型知道证据不全。
    omitted = [rel for rel in omitted if rel not in images]
    try:
        import json

        from autophone.knowledge.query import KnowledgeQueryEngine

        app = str(run_result.get("app") or "")
        knowledge = KnowledgeQueryEngine(
            app,
            run_root=run_dir.parent.parent,
        ).execute(6, run_id=run_id)
        knowledge_context = json.dumps(knowledge, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - diagnosis remains available without a catalog
        knowledge_context = ""
    req = AiRequest(
        prompt=_build_prompt(run_result, context_text, omitted, knowledge_context, images),
        system=_SYSTEM,
        tools="none",   # 一轮纯推理，证据已内联，不给工具（不走 --add-dir）
        json_schema=_MODEL_TRANSPORT_SCHEMA,
        provider=provider,
        model=model,
        images=[bundle.bundle_dir / rel for rel in images],
        allow_raw=allow_raw,
    )
    ai_result = invoke_and_persist(req, run_dir / "ai" / "diagnose-raw.json", runner=runner)
    if not ai_result.ok or not isinstance(ai_result.json, dict):
        return {
            "ok": False,
            "run_id": run_id,
            "error": ai_result.error or "diagnosis returned no structured json",
            "provider": ai_result.provider,
            "raw_path": ai_result.raw_path,
            "context_skipped": bundle.skipped,
            "context_images": images,
        }

    # §8.0：evidence_refs 必须落在本次实际喂进 prompt 的产物集合里（内联文本 + 附带图片）。
    # 「不臆造」此前只写在 _SYSTEM 提示词里，是请求不是约束；这里把它变成宿主侧的 fail-closed。
    allowed_refs = set(inlined) | set(images)
    unknown_refs = [str(r) for r in (ai_result.json.get("evidence_refs") or []) if str(r) not in allowed_refs]
    if unknown_refs:
        return {
            "ok": False,
            "run_id": run_id,
            "error": "evidence_refs not present in the inlined evidence set: " + ", ".join(unknown_refs),
            "provider": ai_result.provider,
            "model": ai_result.model,
            "raw_path": ai_result.raw_path,
            "context_inlined": inlined,
            "context_images": images,
            "context_skipped": bundle.skipped,
        }

    # 我们盖 envelope/provenance，再贴 strict 契约校验。
    diagnosis = drop_optional_nulls(_MODEL_SCHEMA, dict(ai_result.json))
    diagnosis["schema_version"] = "v1"
    diagnosis["run_id"] = run_id
    diagnosis["provenance"] = {"provider": ai_result.provider, "model": ai_result.model}
    validate_or_raise("ai-diagnosis.v1.json", diagnosis)
    write_json(run_dir / "ai-diagnosis.json", diagnosis)
    (run_dir / "ai-diagnosis.html").write_text(_render_html(diagnosis, ai_result), encoding="utf-8")

    # 诊断产物登记进 manifest：json/html 是平台自身脱敏输出（可分享摘要）；raw 推理留本地禁外发。
    append_artifacts(run_dir, [
        Artifact("ai-diagnosis.json", "report"),
        Artifact("ai-diagnosis.html", "report", share_policy="attach_allowed"),
        Artifact("ai/diagnose-raw.json", "other", retention="local_only", share_policy="forbidden"),
    ])
    return {
        "ok": True,
        "run_id": run_id,
        "diagnosis": diagnosis,
        "provider": ai_result.provider,
        "model": ai_result.model,
        "cost_usd": ai_result.cost_usd,
        "raw_path": ai_result.raw_path,
        "context_inlined": inlined,
        "context_images": images,
        "context_included": bundle.included,
        "context_skipped": bundle.skipped,
        "context_raw_included": bundle.raw_included,
        "context_needs_redaction": bundle.needs_redaction,
        "context_redacted_projection": bundle.redacted_projection,
    }


def _render_html(diagnosis: dict[str, Any], ai_result: Any) -> str:
    def esc(v: Any) -> str:
        return html.escape(str(v))

    evidence = "".join(f"<li>{esc(e)}</li>" for e in diagnosis.get("evidence_refs") or []) or "<li>-</li>"
    steps = "".join(f"<li>{esc(s)}</li>" for s in diagnosis.get("next_steps") or []) or "<li>-</li>"
    prov = diagnosis.get("provenance") or {}
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>AI 诊断 {esc(diagnosis.get('run_id'))}</title>"
        "<style>body{font-family:-apple-system,sans-serif;margin:2rem;color:#16202a}"
        ".tag{display:inline-block;padding:2px 8px;border-radius:4px;background:#eef2f6;font-size:12px}"
        "h1{font-size:18px}li{margin:2px 0}</style>"
        f"<h1>AI 失败归因 <small>{esc(diagnosis.get('run_id'))}</small></h1>"
        f"<p><span class=tag>provider {esc(prov.get('provider'))}</span> "
        f"<span class=tag>model {esc(prov.get('model') or '-')}</span> "
        f"<span class=tag>confidence {esc(diagnosis.get('confidence') or '-')}</span> "
        f"<span class=tag>cost ${esc(ai_result.cost_usd if ai_result.cost_usd is not None else '-')}</span></p>"
        f"<h2>定位</h2><p>case：{esc(diagnosis.get('failed_case') or '-')} / "
        f"action：{esc(diagnosis.get('failed_action') or '-')}</p>"
        f"<h2>最可能根因</h2><p>{esc(diagnosis.get('likely_root_cause'))}</p>"
        f"<h2>证据</h2><ul>{evidence}</ul>"
        f"<h2>下一步</h2><ul>{steps}</ul>"
        "<hr><p style=color:#818181;font-size:12px>AI 产物为带证据的假设，非结论；请人复核后再据以修复。</p>"
    )
