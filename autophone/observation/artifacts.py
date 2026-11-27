from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autophone.contracts import ContractError, validate_or_raise, write_json


class GovernanceError(ValueError):
    pass


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_or_raise("artifact-manifest.v2.json", payload)
    return payload


# AI 使用策略：独立于 share_policy 的治理维度。
#   allowed                  —— 可直接进 AI 净化包（build_ai_context 默认复制）。
#   allowed_after_redaction  —— 只有脱敏形态可进包：redaction_status 为 redacted / summary_only 时复制；
#                               raw 本体不进包，记入 needs_redaction。
#   forbidden                —— 不进包；只有 --allow-raw 显式放行并留痕（raw_included）。
AI_POLICIES: tuple[str, ...] = ("allowed", "allowed_after_redaction", "forbidden")


def default_ai_policy(redaction_status: str, share_policy: str) -> str:
    """未显式声明 ai_policy 时的推导值。

    raw 或 share_policy 为 forbidden → forbidden；其余 → allowed。
    要让某类产物（如截图）单独进包，必须显式声明 ai_policy，而不是放宽这里。
    """
    if redaction_status == "raw" or share_policy == "forbidden":
        return "forbidden"
    return "allowed"


@dataclass(frozen=True)
class Artifact:
    path: str
    kind: str
    sensitivity: str = "internal"
    redaction_status: str = "summary_only"
    retention: str = "shareable"
    share_policy: str = "summary_only"
    # 空串＝未声明，__post_init__ 按 default_ai_policy 推导；显式传值须落在 AI_POLICIES 内。
    ai_policy: str = ""

    def __post_init__(self) -> None:
        policy = self.ai_policy or default_ai_policy(self.redaction_status, self.share_policy)
        if policy not in AI_POLICIES:
            raise ValueError(f"ai_policy must be one of {AI_POLICIES}, got {policy!r} for {self.path}")
        object.__setattr__(self, "ai_policy", policy)

    def to_manifest_item(self, run_dir: Path) -> dict[str, Any]:
        item: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "sensitivity": self.sensitivity,
            "redaction_status": self.redaction_status,
            "retention": self.retention,
            "share_policy": self.share_policy,
            "ai_policy": self.ai_policy,
        }
        target = run_dir / self.path
        if target.exists() and target.is_file():
            data = target.read_bytes()
            item["sha256"] = hashlib.sha256(data).hexdigest()
            item["bytes"] = len(data)
        return item


def _private_candidate_evidence(run_dir: Path):
    """Discover only the candidate and product RNU evidence namespaces."""
    root = run_dir.resolve()

    def checked(path: Path):
        if path.is_symlink():
            raise ContractError(f"candidate/RNU evidence path is a symlink: {path}")
        if not path.resolve().is_relative_to(root):
            raise ContractError(f"candidate/RNU evidence path escapes Run: {path}")

    def files(path: Path):
        checked(path)
        if path.is_file():
            yield path
        elif path.is_dir():
            for member in sorted(path.rglob("*")):
                checked(member)
                if member.is_file():
                    yield member

    for name in ("candidate-activation.json", "candidate-attempts", "candidate-activations", "candidate-identities"):
        for path in files(run_dir / name):
            yield path, path.suffix != ".json"
    for name in ("rnu-port-observations", "rnu-port-protocol"):
        for path in files(run_dir / name):
            yield path, True
    shots = run_dir / "shots"
    checked(shots)
    for directory in sorted(shots.glob("rnu-*")):
        for path in files(directory):
            yield path, True
    for certificate in sorted(run_dir.glob("native-shell-certificate-*.pem")):
        for path in files(certificate):
            yield path, True


def write_artifact_manifest(run_dir: Path, run_id: str, artifacts: list[Artifact]) -> Path:
    # Product RNU collections include bundles, assets and SQLite/WAL bytes.
    # Their private policy overrides generic screenshot declarations as well.
    declared = {artifact.path: artifact for artifact in artifacts}
    for path, raw in _private_candidate_evidence(run_dir):
        relative = path.relative_to(run_dir).as_posix()
        declared[relative] = Artifact(relative, "other", retention="local_only",
                                      redaction_status="raw" if raw else "summary_only",
                                      share_policy="forbidden", ai_policy="forbidden")
    artifacts = list(declared.values())
    payload = {
        "schema_version": "v2",
        "run_id": run_id,
        "artifacts": [artifact.to_manifest_item(run_dir) for artifact in artifacts],
    }
    validate_or_raise("artifact-manifest.v2.json", payload)
    path = run_dir / "artifact-manifest.json"
    return write_json(path, payload)


def append_artifacts(run_dir: Path, artifacts: list[Artifact]) -> Path:
    """把新产物登记进已存在的 artifact-manifest.json（按 path 幂等替换），re-validate 后写回。

    用于 run 收尾后追加的产物（如 AI 诊断），不重算既有条目的 sha256——只动自己这几条。
    """
    path = run_dir / "artifact-manifest.json"
    manifest = load_manifest(path)
    new_paths = {a.path for a in artifacts}
    kept = [item for item in manifest.get("artifacts", []) if item.get("path") not in new_paths]
    for artifact in artifacts:
        if (run_dir / artifact.path).exists():
            kept.append(artifact.to_manifest_item(run_dir))
    manifest["artifacts"] = kept
    validate_or_raise("artifact-manifest.v2.json", manifest)
    return write_json(path, manifest)


def entity_readback_artifact() -> Artifact:
    """Redacted post-action instance-level Entity Readback evidence artifact."""
    return Artifact(
        "entity-readback.json",
        "other",
        redaction_status="redacted",
        share_policy="summary_only",
        ai_policy="allowed",
    )


def default_phase2_artifacts(run_dir: Path) -> list[Artifact]:
    has_images = any(
        path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        for path in (run_dir / "shots").glob("**/*")
    ) if (run_dir / "shots").exists() else False
    html_report = Artifact("report.html", "report", share_policy="attach_allowed")
    if has_images:
        html_report = Artifact(
            "report.html",
            "report",
            sensitivity="sensitive",
            redaction_status="raw",
            retention="local_only",
            share_policy="forbidden",
        )
    # ai_policy 未显式声明的条目按 default_ai_policy 推导：
    #   · 摘要/脱敏且 share_policy 非 forbidden 的报告、覆盖率、events.json、query-summary、impact/* → allowed；
    #   · run-spec / run-submission / build-identity（share_policy=forbidden）→ forbidden，含 rig 拓扑与内部编号；
    #   · run.log / events.jsonl / run-failure.json 显式声明 allowed_after_redaction；
    #   · report.html 含截图时为 raw → forbidden（截图走 shots/* 单独放行，不经报告整页）。
    artifacts = [
        # run-spec 会暴露内部用例编号与 rig 拓扑，所以禁止外发；但它是刻意跨机器同步的
        # 关键元数据，因此 retention 保持默认 shareable；运行态 /runs/ 整棵忽略，
        # 显式归档后才可进入仓库。share_policy 与 Git 留存独立。
        Artifact("run-spec.json", "other", share_policy="forbidden"),
        Artifact("run-submission.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("closure-intent.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("world-identity.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("reset-receipt.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("closure-intent.quarantine.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("build-identity.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("platform-run-result.json", "report"),
        Artifact("result.json", "report"),
        Artifact("report.md", "report"),
        html_report,
        Artifact("coverage.json", "coverage"),
        Artifact("coverage.html", "coverage", share_policy="attach_allowed"),
        Artifact("rig-snapshot.json", "other"),
        # BL-083 第八轮：Run 前被测 App Frida 残留检测（Tag 255 区域）与 Run 后终止的 pid、时点与结果。
        Artifact("app-frida-hygiene.json", "other", retention="local_only", share_policy="forbidden"),
        # Draft Trial 的 Fixture Readback 证据（摘要绑定声明、真实标识与 result.json 动作引用）。
        # context 与被绑定参数写出前已按 governance 敏感键 / 手机号身份证规则脱敏（订单号等
        # 业务标识不在敏感键内，原样保留，与 result.json 动作明细同级）。与 result.json 同级
        # 留存，跨机器归档后准备卡仍可复核 runtime_verified。
        Artifact(
            "fixture-readback.json",
            "other",
            redaction_status="redacted",
            share_policy="summary_only",
            ai_policy="allowed",
        ),
        # BL-083 第八轮 Cleanup Readback 证据（清理 Operation 与独立只读 Readback 的动作引用、
        # 参数摘要与身份变化判定）；值写出前已按 governance 规则脱敏，与 fixture-readback.json 同级。
        Artifact(
            "cleanup-readback.json",
            "other",
            redaction_status="redacted",
            share_policy="summary_only",
            ai_policy="allowed",
        ),
        # 专项二：动作后实例级业务实体 Readback 证据（脱敏 + 摘要绑定，与 fixture/cleanup readback 同级）。
        entity_readback_artifact(),
        # Text logs are redacted, but still contain execution topology and
        # exception context. Keep them local with the rest of run evidence.
        # AI 维度 allowed_after_redaction（2026-09-07 裁定）：三者在写出时已经过 autophone.logging.redact_text
        # （run.log 由 formatter、events.jsonl 由 _event_from_record、run-failure.json 由 service 直接调用），
        # 进净化包时 build_ai_context 再以同一函数做一次幂等投影；它们是失败归因最直接的文本证据。
        # retention / share_policy 不变：仍只本机、禁止外发。
        Artifact(
            "run.log",
            "log",
            sensitivity="sensitive",
            redaction_status="redacted",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="allowed_after_redaction",
        ),
        Artifact(
            "events.jsonl",
            "log",
            sensitivity="sensitive",
            redaction_status="redacted",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="allowed_after_redaction",
        ),
        Artifact(
            "run-failure.json",
            "log",
            sensitivity="sensitive",
            redaction_status="redacted",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="allowed_after_redaction",
        ),
        Artifact("planes/telemetry/events.json", "telemetry_events"),
        # 埋点原始行含结构化 PII（人审方案 §6.2）：AI 只读 events.json；需要原文时走脱敏投影，
        # raw 本体不进包 → allowed_after_redaction（有脱敏投影前效果等同 forbidden，且留 needs_redaction 痕）。
        Artifact(
            "planes/telemetry/raw.json",
            "telemetry_raw",
            sensitivity="sensitive",
            redaction_status="raw",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="allowed_after_redaction",
        ),
        Artifact(
            "planes/telemetry/query-summary.json",
            "telemetry_query_summary",
        ),
        # API 抓取原始合并 JSONL：含真实 body/headers（PII/凭据）→ 只本机、禁止外发；
        # 看板经 api_capture.load_capture 脱敏后才渲染。
        # AI 维度 forbidden：headers 含凭据、body 含 PII，看板用的脱敏投影（api_capture.load_capture）
        # 只为渲染设计、未作为 AI 输入验证过；截图单独放行不得连带放行它（§6.1）。
        Artifact(
            "planes/api/capture.jsonl",
            "api_capture",
            sensitivity="sensitive",
            redaction_status="raw",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="forbidden",
        ),
        # run 期录屏：设备画面（同截图，可能含登录态/PII）→ 只本机、禁止外发。
        # AI 维度 forbidden：视频不能内联进 prompt、体积大、无逐帧脱敏手段；需要画面用截图。
        Artifact(
            "recording.mp4",
            "screen_recording",
            sensitivity="sensitive",
            redaction_status="raw",
            retention="local_only",
            share_policy="forbidden",
            ai_policy="forbidden",
        ),
        # 影响图谱的三类迁移事实均在写入前完成归一与脱敏，并由 Git 跨机器同步。
        # 它们仍只允许进入摘要，不作为外部附件直接发送。
        Artifact(
            "impact/observations.jsonl",
            "other",
            redaction_status="redacted",
        ),
        Artifact(
            "impact/action-marks.jsonl",
            "other",
            redaction_status="redacted",
        ),
        Artifact(
            "impact/consumer-context.json",
            "other",
            redaction_status="redacted",
        ),
    ]
    for private in sorted([*(run_dir / "impact/private").rglob("*"),
                           *(run_dir / "planes/ax/private").rglob("*")]):
        if private.is_file():
            artifacts.append(Artifact(
                private.relative_to(run_dir).as_posix(), "other",
                sensitivity="sensitive", redaction_status="raw", retention="local_only",
                share_policy="forbidden", ai_policy="forbidden",
            ))
    api_private = run_dir / "planes/api/private"
    for private in [api_private, *sorted(api_private.rglob("*"))]:
        if private.is_symlink() or not private.resolve().is_relative_to(run_dir.resolve()):
            raise ContractError(f"API evidence escapes Run: {private}")
        if private.is_file():
            artifacts.append(Artifact(
                private.relative_to(run_dir).as_posix(), "other",
                sensitivity="sensitive", redaction_status="raw", retention="local_only",
                share_policy="forbidden", ai_policy="forbidden",
            ))
    # Operation helpers can write nested API captures and Fixture identities.
    # Register every original before sealing, without allowing symlink escape.
    operations = run_dir / "operations"
    for original in [operations, *sorted(operations.rglob("*"))]:
        if original.is_symlink() or not original.resolve().is_relative_to(run_dir.resolve()):
            raise ContractError(f"operation evidence escapes Run: {original}")
        if original.is_file():
            artifacts.append(Artifact(
                original.relative_to(run_dir).as_posix(), "other",
                sensitivity="sensitive", redaction_status="raw", retention="local_only",
                share_policy="forbidden", ai_policy="forbidden",
            ))
    shots_dir = run_dir / "shots"
    if shots_dir.exists():
        for shot in sorted(shots_dir.glob("**/*")):
            if not shot.is_file():
                continue
            rel = shot.relative_to(run_dir).as_posix()
            if shot.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                # 截图是失败归因的主要视觉证据（§7.3）。AI 维度 allowed：可不经 --allow-raw 单独进包，
                # 而 telemetry raw / api capture raw 仍被各自的 ai_policy 挡住（§6.1 的独立放行）。
                # retention / share_policy 不变：仍只本机、禁止外发。
                artifacts.append(Artifact(
                    rel,
                    "screenshot",
                    sensitivity="sensitive",
                    redaction_status="raw",
                    retention="local_only",
                    share_policy="forbidden",
                    ai_policy="allowed",
                ))
            else:
                artifacts.append(Artifact(rel, "other", retention="local_only", share_policy="forbidden"))
    return [artifact for artifact in artifacts if (run_dir / artifact.path).exists()]
