"""Codex 分析进程的无工具配置与事件核对。仅接收调用方已净化的内联输入。"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from autophone.contracts import write_json

POLICY = "codex-analysis-toolless/v1"
SUPPORTED_TOOLLESS_CLI_VERSIONS = frozenset({"codex-cli 0.154.0"})


class CallLimitExceeded(ValueError):
    code = "ai_call_limit_exceeded"


DISABLED_FEATURES = (
    "shell_tool", "view_image", "multi_agent", "multi_agent_v2", "goals",
    "plugins", "apps", "code_mode", "code_mode_host", "code_mode_only",
    "browser_use", "browser_use_external", "computer_use", "image_generation",
    "shell_snapshot", "hooks", "memories", "skill_search", "in_app_browser",
    "request_permissions_tool", "sleep_tool", "tool_suggest", "workspace_dependencies", "artifact",
)


def toolless_flags() -> list[str]:
    """共享的 Codex 读取面限制；事件核对不替代工具执行前的限制。"""
    flags = [arg for feature in DISABLED_FEATURES for arg in ("--disable", feature)]
    return flags + ["--strict-config", "-c", "tools.experimental_request_user_input.enabled=false",
                    "-c", "tools.update_plan.enabled=false", "--enable", "skip_host_skill_discovery", "-c", 'approval_policy="never"',
                    "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                    "-c", "mcp_servers={}"]


def write_toolless_catalog(argv: list[str], runner, *, env: dict, directory: Path, timeout_s: float = 10.0) -> Path:
    """读取 CLI 离线目录并移除元数据派生工具；失败时不启动推理。"""
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(argv, timeout_s)
    probe_env = dict(env, CODEX_HOME=str(directory))
    result = runner([argv[0], "debug", "models", "--bundled"], timeout=min(10.0, timeout_s),
                    env=probe_env, cwd=directory)
    if result.returncode != 0:
        raise ValueError("codex toolless catalog discovery failed")
    try:
        catalog = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("codex toolless catalog discovery returned invalid JSON") from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models:
        raise ValueError("codex toolless catalog must contain models")
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str) or not model["slug"]:
            raise ValueError("codex toolless catalog has invalid model identity")
        model["apply_patch_tool_type"] = None
        model["experimental_supported_tools"] = []
    path = directory / "models.json"
    write_json(path, catalog)
    return path


def unexpected_tool_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") not in {
                "agent_message", "reasoning", "plan", "todo_list", "error"}:
            events.append({"event": event.get("type"), "item_type": item.get("type")})
    return events


@dataclass(frozen=True)
class CallLimit:
    path: Path
    maximum: int

    def reserve(self, *, ref: str, model: str | None) -> int:
        if isinstance(self.maximum, bool) or self.maximum < 1:
            raise ValueError("invalid analysis call limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            doc = json.loads(self.path.read_text()) if self.path.exists() else {
                "schema_version": "codex-analysis-call-limit/v1", "maximum": self.maximum, "calls": []}
            if doc["maximum"] != self.maximum:
                raise ValueError("analysis call limit drift")
            if len(doc["calls"]) >= self.maximum:
                raise CallLimitExceeded("ai_call_limit_exceeded")
            doc["calls"].append({"ref": ref, "model": model})
            write_json(self.path, doc)
            return len(doc["calls"])


def isolated_runner(base, *, raw_path: Path):
    """固定 CLI 工具面和空工作目录；事件流出现未声明的 item 时拒绝结果。"""
    def run(argv, **kwargs):
        if "--version" in argv or argv[1:4] == ["debug", "models", "--bundled"]:
            return base(argv, **kwargs)
        if "exec" not in argv or "--ignore-user-config" not in argv or "--json" not in argv:
            raise ValueError("codex_read_isolation requires an isolated JSON exec")
        command = [arg for arg in argv if arg != "--strict-config"]
        command += toolless_flags()
        requirement = next((p for p in raw_path.resolve().parents
                            if p.name.startswith("REQ-") and p.parent.name == "requirements"), None)
        counter = requirement / "codex-analysis-call-limit.json" if requirement is not None else None
        limit = os.environ.get("AUTOPHONE_CODEX_MAX_ANALYSIS_CALLS")
        if limit is None and counter is not None and counter.exists():
            limit = json.loads(counter.read_text())["maximum"]
        maximum, ordinal = None, None
        requested_model = argv[argv.index("-m") + 1] if "-m" in argv else None
        if limit is not None:
            maximum = int(limit)
            # 每张需求单共享一个计数文件；同一需求另开工作流仍不能重置次数。
            if requirement is None:
                raise ValueError("analysis call limit requires a requirement evidence path")
            ordinal = CallLimit(counter, maximum).reserve(
                ref=str(raw_path), model=requested_model)
        with tempfile.TemporaryDirectory(prefix="autophone-codex-analysis-") as directory:
            kwargs["cwd"] = Path(directory)
            proof = {"policy": POLICY, "cwd": directory, "disabled_features": list(DISABLED_FEATURES),
                     "user_config": False, "project_doc_max_bytes": 0, "mcp_servers": {},
                     "tool_events": [], "status": "started"}
            proof.update(maximum_calls=maximum, call_ordinal=ordinal,
                         requested_model=requested_model)
            proof_path = Path(raw_path).with_name("codex-read-isolation.json")
            write_json(proof_path, proof)
            try:
                completed = base(command, **kwargs)
            except subprocess.TimeoutExpired as exc:
                def decoded(value):
                    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                stdout, stderr = decoded(exc.output), decoded(exc.stderr)
                Path(raw_path).with_name("codex-events.jsonl").write_text(stdout, encoding="utf-8")
                Path(raw_path).with_name("codex-stderr.log").write_text(stderr, encoding="utf-8")
                proof.update(status="timeout", stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest())
                proof["tool_events"] = unexpected_tool_events(stdout)
                proof["isolation_violation"] = bool(proof["tool_events"])
                for line in stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                        proof["usage"] = event["usage"]
                write_json(proof_path, proof)
                raise
            Path(raw_path).with_name("codex-events.jsonl").write_text(completed.stdout, encoding="utf-8")
            Path(raw_path).with_name("codex-stderr.log").write_text(completed.stderr, encoding="utf-8")
            proof["tool_events"] = unexpected_tool_events(completed.stdout)
            proof["stdout_sha256"] = hashlib.sha256(completed.stdout.encode()).hexdigest()
            proof["status"] = "rejected" if proof["tool_events"] else "completed"
            write_json(proof_path, proof)
            if proof["tool_events"]:
                raise ValueError("codex_read_isolation: unexpected tool events")
            return completed
    return run
