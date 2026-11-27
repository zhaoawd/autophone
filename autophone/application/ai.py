"""ai.py — 调用本地 agent CLI（Claude Code / Codex）的能力层（AI-0 桥）。

定位见 docs/platform-ai-capability.md：平台只在非确定性环节（失败归因 / 验证单 / 探索 /
操作起草）调本地 agent，**运行期回归链严禁触达本模块**。本文件落地 AI-0 三件：

1. build_ai_context —— §5.2 输入侧治理：把 manifest 允许的脱敏产物**物理复制**进一个净化包，
   再交给 --add-dir，而不是让 agent 直接读裸 run_dir（raw/forbidden 默认不进包，allow_raw 留痕放行）。
2. AiProvider 双后端（claude/codex）+ invoke —— 把 AiRequest 翻译成 CLI 命令，fail-closed 收口，
   resolved provider/model/cli_version 回写防漂移。
3. ai_doctor —— CLI capability detection（在不在/版本/默认 provider，不打印密钥）。

subprocess 层是可注入 seam（runner 参数），宿主侧逻辑无需真模型即可单测——与 shell 适配层 /
verification 执行器同款约定。
"""
from __future__ import annotations

import json
import math
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable

from autophone.logging import redact_text
from autophone.paths import CODE_ROOT, STATE_ROOT
from autophone.contracts import validate_or_raise, write_json
from autophone.observation.artifacts import GovernanceError, default_ai_policy, load_manifest

PROVIDERS = ("claude", "codex")
DEFAULT_PROVIDER = "codex"
DEFAULT_TIMEOUT_S = 300.0
CHATGPT_APP_CODEX_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex"


class AiError(RuntimeError):
    pass


# ──────────────── 调用契约（贴合 autophone/_data/schemas/ai-request|result.v1.json）────────────


@dataclass
class AiRequest:
    prompt: str
    system: str | None = None
    context_dirs: list[Path] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    tools: str = "none"            # none | device | full
    provider: str | None = None    # claude | codex | auto；None=用 env/默认
    model: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_budget_usd: float | None = None
    allow_raw: bool = False
    # CLI 停止阈值（BL-067 控费）：给出时 claude `--max-budget-usd` 取它，`max_budget_usd` 只作授权上限，
    # 由调用后的 `enforce_call_budget` 判定。缺省 None 时 CLI 阈值等于授权上限（原行为）。
    stop_budget_usd: float | None = None

    def cli_budget_usd(self) -> float | None:
        """传给 CLI 的停止阈值：显式停止阈值优先，否则为授权上限。"""
        return self.stop_budget_usd if self.stop_budget_usd is not None else self.max_budget_usd

    def to_contract(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema_version": "v1", "prompt": self.prompt, "tools": self.tools}
        if self.system:
            out["system"] = self.system
        if self.context_dirs:
            out["context_dirs"] = [str(p) for p in self.context_dirs]
        if self.images:
            out["images"] = [str(p) for p in self.images]
        if self.json_schema is not None:
            out["json_schema"] = self.json_schema
        if self.provider:
            out["provider"] = self.provider
        if self.model:
            out["model"] = self.model
        out["timeout_s"] = self.timeout_s
        if self.max_budget_usd is not None:
            out["max_budget_usd"] = self.max_budget_usd
        if self.stop_budget_usd is not None:
            out["stop_budget_usd"] = self.stop_budget_usd
        out["allow_raw"] = self.allow_raw
        validate_or_raise("ai-request.v1.json", out)
        return out


@dataclass
class AiResult:
    ok: bool
    text: str
    provider: str
    json: dict[str, Any] | None = None
    model: str | None = None
    cli_version: str | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    raw_path: str | None = None
    error: str | None = None
    # 只做加法的计量留痕：CLI 结果元数据里的 token 用量与 CLI 实际回报的模型标识。
    # `model` 保持请求/解析口径不变；两项缺省为 None 时契约输出与原先逐键相同。
    usage: dict[str, Any] | None = None
    reported_models: list[str] | None = None
    # 只做加法的预算留痕（BL-036 G4）：调用方声明的单次上限与 fail-closed 拒绝码。两项缺省为
    # None 时契约输出与原先逐键相同。
    max_budget_usd: float | None = None
    error_code: str | None = None
    # 只做加法的设备工具留痕（BL-067）：device 通道从事件流统计的 vphone MCP 调用（attempted /
    # completed / failed / denied 与失败原文摘要）。codex 读 `--json`，claude 读 `stream-json`。
    # 其余通道缺省 None。
    device_tool_trace: dict[str, Any] | None = None
    # 只做加法的控费留痕（BL-067）：CLI 停止阈值，与超出授权上限 `max_budget_usd` 的金额。
    stop_budget_usd: float | None = None
    budget_overrun_usd: float | None = None

    def to_contract(self) -> dict[str, Any]:
        out = {
            "schema_version": "v1",
            "ok": self.ok,
            "text": self.text,
            "provider": self.provider,
            "json": self.json,
            "model": self.model,
            "cli_version": self.cli_version,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "raw_path": self.raw_path,
            "error": self.error,
        }
        if self.usage is not None:
            out["usage"] = self.usage
        if self.reported_models is not None:
            out["reported_models"] = self.reported_models
        if self.max_budget_usd is not None:
            out["max_budget_usd"] = self.max_budget_usd
        if self.error_code is not None:
            out["error_code"] = self.error_code
        if self.device_tool_trace is not None:
            out["device_tool_trace"] = self.device_tool_trace
        if self.stop_budget_usd is not None:
            out["stop_budget_usd"] = self.stop_budget_usd
        if self.budget_overrun_usd is not None:
            out["budget_overrun_usd"] = self.budget_overrun_usd
        validate_or_raise("ai-result.v1.json", out)
        return out


@dataclass(frozen=True)
class ProviderCapabilities:
    text: bool
    images: bool
    structured_output: bool
    device_tools: bool
    streaming: bool
    budget_limit: bool
    cost_reporting: bool

    def to_dict(self) -> dict[str, bool]:
        return dict(self.__dict__)


_PROVIDER_CAPABILITIES = {
    "claude": ProviderCapabilities(
        text=True,
        images=True,
        structured_output=True,
        device_tools=True,
        streaming=True,
        budget_limit=True,
        cost_reporting=True,
    ),
    # BL-067：Codex 适配器已在 tools=device 时注入 vphone MCP 并放行其工具审批，
    # 但真实 `codex exec` 下的 MCP 放行尚无活机证据。看板准入在活机验证前仍不宣告
    # device_tools，避免把未验证的实现当成授权。
    "codex": ProviderCapabilities(
        text=True,
        images=True,
        structured_output=True,
        device_tools=False,
        streaming=False,
        budget_limit=False,
        cost_reporting=False,
    ),
}


def provider_capabilities(provider: str | None) -> tuple[str, ProviderCapabilities]:
    resolved = resolve_provider(provider)
    return resolved, _PROVIDER_CAPABILITIES[resolved]


# ─────────────────────────── §5.2 ai-context 输入净化包 ───────────────────────────

# 净化包的身份文件：build_ai_context 写它，loader 门禁 3 用它判断一个 context_dir
# 是不是受治理的 bundle（platform-skill-boundary.md §八 门禁 3）。
AI_CONTEXT_MANIFEST = "ai-context-manifest.json"


@dataclass
class AiContextBundle:
    bundle_dir: Path
    included: list[str]
    skipped: list[str]
    raw_included: list[str]   # 经 allow_raw 显式放行的 raw/forbidden 产物（留痕证据）
    # ai_policy=allowed_after_redaction 但本体仍是 raw、尚无脱敏投影可用的产物（未进包）。
    needs_redaction: list[str] = field(default_factory=list)
    # ai_policy=allowed_after_redaction 的文本产物：进包副本是 redact_text 投影而非原样拷贝（留痕）。
    redacted_projection: list[str] = field(default_factory=list)


def _is_safe_relpath(rel: str) -> bool:
    """manifest 的 path 必须是「干净的相对路径」：非绝对、无 `..` 段、非空、不带盘符。"""
    if not rel or rel.strip() != rel:
        return False
    p = Path(rel)
    if p.is_absolute() or p.drive or p.anchor:
        return False
    return not any(part == ".." for part in p.parts)


def _within(path: Path, root: Path) -> bool:
    """resolve 后 path 是否落在 root（含 root 自身）内——兜住 symlink 逃逸。"""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def artifact_ai_policy(item: dict[str, Any]) -> str:
    """manifest 条目的 AI 使用策略；缺字段（旧 manifest）按 default_ai_policy 推导。"""
    policy = item.get("ai_policy")
    if policy in ("allowed", "allowed_after_redaction", "forbidden"):
        return str(policy)
    return default_ai_policy(str(item.get("redaction_status") or ""), str(item.get("share_policy") or ""))


def artifact_ai_blocked(item: dict[str, Any]) -> str | None:
    """净化包为何默认不收这条产物：None＝可进包；"forbidden"；"needs_redaction"（allowed_after_redaction 但仍是 raw）。"""
    policy = artifact_ai_policy(item)
    if policy == "allowed":
        return None
    if policy == "forbidden":
        return "forbidden"
    return "needs_redaction" if item.get("redaction_status") == "raw" else None


# allowed_after_redaction 文本产物的最小脱敏投影：与写出侧同一个 autophone.logging.redact_text
# （手机号打码、Authorization / Cookie / token / password 抹除），幂等；只对这些后缀做投影，
# 其余（图片、视频、二进制）不投影。raw 本体仍走 needs_redaction，投影不放宽它。
_REDACTION_PROJECTION_SUFFIXES = {".log", ".jsonl", ".json", ".txt", ".md", ".csv", ".yaml", ".yml"}


def _write_redacted_projection(src: Path, target: Path) -> bool:
    """把 src 经 redact_text 逐行投影写到 target；非文本后缀返回 False（调用方原样拷贝）。"""
    if src.suffix.lower() not in _REDACTION_PROJECTION_SUFFIXES:
        return False
    text = src.read_text(encoding="utf-8", errors="replace")
    target.write_text(redact_text(text), encoding="utf-8")
    return True


def build_ai_context(
    run_dir: Path,
    *,
    dest: Path | None = None,
    allow_raw: bool = False,
) -> AiContextBundle:
    """把 manifest 允许的产物复制进净化包，供 --add-dir。fail-closed：无 manifest 不能净化即抛。

    规则（docs §5.2 / roadmap §2.4；AI 维度见 platform-human-review-two-stage.md §6.1，BL-071）：
    - 按每条的 `ai_policy` 筛选；旧 manifest 缺该字段时按 `default_ai_policy`（raw 或 share_policy
      forbidden → forbidden，其余 allowed）推导。
    - allowed → 复制；allowed_after_redaction → 只有 redaction_status 为 redacted/summary_only 才进包，
      且文本产物进包的是 redact_text 投影（记 redacted_projection），raw 本体跳过并记 needs_redaction；
      forbidden → 跳过。
    - allow_raw=True 才把 forbidden 与 needs_redaction 的产物也复制进包，并记 raw_included（留痕）。
    - 截图与 telemetry raw / api capture raw 因此各按自己的 ai_policy 决定，不再被同一个开关捆绑。
    - 其余允许产物直接复制，保留相对路径结构。
    """
    manifest_path = run_dir / "artifact-manifest.json"
    if not manifest_path.exists():
        raise GovernanceError(f"no artifact-manifest.json under {run_dir}; refuse to feed un-enumerated run dir to AI")
    manifest = load_manifest(manifest_path)
    bundle_dir = dest or Path(tempfile.mkdtemp(prefix="autophone-ai-context-"))
    bundle_dir.mkdir(parents=True, exist_ok=True)

    run_root = run_dir.resolve()
    bundle_root = bundle_dir.resolve()
    included: list[str] = []
    skipped: list[str] = []
    raw_included: list[str] = []
    needs_redaction: list[str] = []
    redacted_projection: list[str] = []
    for item in manifest.get("artifacts", []):
        rel = str(item["path"])
        # fail-closed 路径守护：manifest 的 path 不可信，绝对路径 / `..` / symlink 逃逸都拒绝——
        # 否则 `../outside.txt` 会把 run/bundle 外的文件拷进净化包再内联进 prompt（审查 P2）。
        if not _is_safe_relpath(rel):
            raise GovernanceError(f"unsafe artifact path in manifest（拒绝 absolute/`..`）: {rel!r}")
        blocked = artifact_ai_blocked(item)
        if blocked == "needs_redaction":
            needs_redaction.append(rel)
        if blocked and not allow_raw:
            skipped.append(rel)
            continue
        src = run_dir / rel
        target = bundle_dir / rel
        # resolve 后必须分别落在 run_dir / bundle_dir 内（兜住 symlink 逃逸与残余 `..`）。
        if not _within(src, run_root):
            raise GovernanceError(f"artifact path escapes run dir after resolve: {rel!r}")
        if not _within(target, bundle_root):
            raise GovernanceError(f"artifact target escapes bundle dir after resolve: {rel!r}")
        if not src.exists():
            skipped.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if (artifact_ai_policy(item) == "allowed_after_redaction" and not blocked
                and _write_redacted_projection(src, target)):
            redacted_projection.append(rel)
        else:
            shutil.copy2(src, target)
        included.append(rel)
        if blocked:
            raw_included.append(rel)

    # 净化清单本身入包，既是证据也让 agent 知道喂了哪些——allow_raw 放行什么留痕在此。
    write_json(bundle_dir / AI_CONTEXT_MANIFEST, {
        "schema_version": "v1",
        "run_id": manifest.get("run_id"),
        "source_run_dir": str(run_dir),
        "allow_raw": allow_raw,
        "included": included,
        "skipped": skipped,
        "raw_included": raw_included,
        "needs_redaction": needs_redaction,
        "redacted_projection": redacted_projection,
    })
    return AiContextBundle(
        bundle_dir=bundle_dir, included=included, skipped=skipped,
        raw_included=raw_included, needs_redaction=needs_redaction,
        redacted_projection=redacted_projection,
    )


# ─────────────────────────── provider 后端 ───────────────────────────


@dataclass
class _Completed:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[..., _Completed]


def _terminate_process_group(proc: subprocess.Popen, *, grace_s: float = 0.5) -> None:
    """Terminate the CLI and descendants; cancellation must not orphan MCP tools."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()
    proc.wait()


def _supervise_device_agent(proc, env):
    if env.get("AUTOPHONE_MANAGED_DEVICE_AGENT") != "1":
        return
    from autophone.device.stage_guard import StageGuard, OperationClass
    from autophone.device.stage_threads import start_device_thread
    guard = StageGuard.current()
    if guard is None:
        _terminate_process_group(proc)
        raise RuntimeError("agent_device_authority_missing")
    def supervise():
        try:
            while proc.poll() is None:
                guard.assert_allowed(OperationClass.FRIDA)
                time.sleep(.05)
        except Exception:
            _terminate_process_group(proc)
        finally:
            _terminate_process_group(proc)
            proc.wait()
    try:
        start_device_thread(supervise, name="device-agent-authority")
    except BaseException:
        _terminate_process_group(proc)
        raise


def _subprocess_runner(argv: list[str], *, timeout: float, env: dict[str, str], input_text: str | None = None,
                       on_line: Callable[[str], None] | None = None,
                       cwd: Path | None = None) -> _Completed:
    # 显式喂空 stdin：claude/codex -p 会等 stdin，不接会先卡 3s 才 proceed。
    # on_line 被忽略：这是「跑完才回」的非流式 runner（与 stream runner 同签名，便于互换）。
    from autophone.application.ai_jobs import JobCanceled, current_cancel_token

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    _supervise_device_agent(proc, env)
    if proc.stdin is not None:
        try:
            proc.stdin.write(input_text if input_text is not None else "")
            proc.stdin.close()
            proc.stdin = None
        except (BrokenPipeError, OSError):
            pass
    token = current_cancel_token()
    deadline = time.monotonic() + timeout
    # 轮询必须经 communicate（内部读线程持续排水管道）：只 poll 不读的话，子进程
    # 输出一旦超过 OS 管道缓冲（~64KB）就写阻塞永不退出，会被误判成超时。
    while True:
        if token is not None and token.canceled:
            _terminate_process_group(proc)
            raise JobCanceled("AI provider process canceled")
        if time.monotonic() >= deadline:
            _terminate_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = exc.output, exc.stderr
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        try:
            stdout, stderr = proc.communicate(timeout=0.05)
        except subprocess.TimeoutExpired:
            continue
        return _Completed(returncode=proc.returncode, stdout=stdout, stderr=stderr)


def _subprocess_stream_runner(argv: list[str], *, timeout: float, env: dict[str, str], input_text: str | None = None,
                              on_line: Callable[[str], None] | None = None,
                              cwd: Path | None = None) -> _Completed:
    """流式 runner：Popen 起进程，stdout 逐行回调 on_line（边跑边吐），返回累计 _Completed。

    用于 claude `--output-format stream-json --verbose`——每行一个 JSON 事件（assistant 消息 /
    工具调用 / 最终 result）。timeout 用独立读线程 + 截止时间兜住（裸 readline 阻塞不会触发软超时）：
    到点 SIGKILL 进程并抛 TimeoutExpired，与非流式 runner 的 fail-closed 语义对齐。
    """
    import queue
    import threading
    import time as _time
    from autophone.application.ai_jobs import JobCanceled, current_cancel_token

    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, bufsize=1, start_new_session=True, cwd=cwd,
    )
    _supervise_device_agent(proc, env)
    if proc.stdin is not None:
        try:
            proc.stdin.write(input_text if input_text is not None else "")
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - 子进程提前退出时写 stdin 可能 broken pipe，不致命
            pass

    q: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                q.put(line)
        finally:
            q.put(None)  # 哨兵：stdout 读完

    threading.Thread(target=_reader, name="ai-stream-reader", daemon=True).start()

    out_lines: list[str] = []
    deadline = _time.monotonic() + timeout
    token = current_cancel_token()
    while True:
        if token is not None and token.canceled:
            _terminate_process_group(proc)
            raise JobCanceled("AI provider process canceled")
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            _terminate_process_group(proc)
            raise subprocess.TimeoutExpired(argv, timeout)
        try:
            line = q.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue
        if line is None:
            break
        out_lines.append(line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 - 单条事件回调出错不应中断整个流
                pass

    proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ""
    return _Completed(returncode=proc.returncode, stdout="".join(out_lines), stderr=stderr)


def _json_directive(schema: dict[str, Any]) -> str:
    import json as _json
    return ("\n\n只输出一个 JSON 对象，不要任何解释、markdown 或代码围栏，严格符合此 JSON Schema：\n"
            + _json.dumps(schema, ensure_ascii=False))


def _extract_json(text: str) -> dict:
    """从模型文本里抠出 JSON 对象（容忍代码围栏/前后散文）。抠不出 → 抛，由上层 fail-closed。"""
    import json as _json
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s[:4].lower() == "json" else s
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return _json.loads(s)  # 让它以 JSONDecodeError 抛
    return _json.loads(s[start:end + 1])


# ─────────────────────────── stream-json → timeline 事件 ───────────────────────────
# claude `--output-format stream-json` 每行一个 JSON 事件。把它翻成前端可渲染的 timeline 事件
# （assistant 文本 / 每次工具调用），claude 的 wire 格式收口在此，不外泄到 job/前端。


def _short_tool(name: str) -> str:
    """去掉 MCP 前缀（mcp__vphone__tap → tap），裸工具名原样。"""
    if name.startswith("mcp__"):
        return name.rsplit("__", 1)[-1]
    return name


def _tool_summary(short: str, inp: dict[str, Any]) -> str:
    """工具入参的一句话摘要：tap 给坐标、swipe 给起止、open_app 给名字，其余压成紧凑 JSON。"""
    try:
        if short == "tap":
            return f"({inp.get('x')}, {inp.get('y')})"
        if short == "swipe":
            return f"({inp.get('x1')},{inp.get('y1')}) → ({inp.get('x2')},{inp.get('y2')})"
        if short == "open_app":
            return str(inp.get("name") or "")
        if short in ("type", "type_text"):
            return str(inp.get("text") or "")[:40]
        if short in ("key", "wait_ready"):
            return str(inp.get("name") or inp.get("timeout_s") or "")
    except Exception:  # noqa: BLE001 - 摘要尽力而为，出错落到通用分支
        pass
    if not inp:
        return ""
    import json as _json
    return _json.dumps(inp, ensure_ascii=False)[:60]


def stream_events(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """一条 stream-json 行 → 零或多个 timeline 事件。

    - assistant 消息：text block → ``{type:text}``；tool_use block → ``{type:tool, name, summary}``。
    - 其它行（system/init、user/tool_result、result）不入 timeline（结果回执含 base64 截图，噪声）。
    """
    out: list[dict[str, Any]] = []
    if obj.get("type") != "assistant":
        return out
    for block in (obj.get("message") or {}).get("content") or []:
        bt = block.get("type")
        if bt == "text":
            text = (block.get("text") or "").strip()
            if text:
                out.append({"type": "text", "text": text})
        elif bt == "tool_use":
            short = _short_tool(block.get("name") or "")
            out.append({"type": "tool", "name": short,
                        "summary": _tool_summary(short, block.get("input") or {})})
    return out


def resolve_vphone_mcp_config() -> str:
    """vphone_mcp 的 MCP 配置文件路径：env 覆写优先，否则生成指向本仓 vphone_mcp 的默认配置。

    生成的配置走 `uv run --project <repo>/vphone_mcp vphone-mcp`（stdio）——这样 device 工具面
    无需预装、随仓库可用。真驱动需活 VM（UDS socket 在跑）。
    """
    env = os.environ.get("AUTOPHONE_VPHONE_MCP_CONFIG")
    if env:
        return env
    from autophone.device.target import resolve_target

    selected_sock = str(resolve_target().sock)
    cfg_dir = STATE_ROOT / "runs" / ".ai"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "vphone-mcp.json"
    desired = {"mcpServers": {"vphone": {
        "command": "uv",
        "args": ["run", "--project", str(CODE_ROOT / "vphone_mcp"), "vphone-mcp"],
        "env": {"VPHONE_SOCK": selected_sock},
    }}}
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        current = None
    # This is a generated cache, not a user-owned config.  Reconcile it on
    # every use so a repository move cannot strand device-enabled agents on a
    # stale --project path.  AUTOPHONE_VPHONE_MCP_CONFIG remains the explicit
    # escape hatch for custom configurations.
    if current != desired:
        write_json(path, desired)
    return str(path)


def load_vphone_mcp_server(config_path: str) -> dict[str, Any]:
    """读取 MCP 配置文件里的 `mcpServers.vphone`；缺失或不是 stdio 命令即抛 AiError（fail-closed）。"""
    try:
        doc = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AiError(f"vphone MCP config unreadable: {config_path}: {type(exc).__name__}") from exc
    server = ((doc or {}).get("mcpServers") or {}).get("vphone") if isinstance(doc, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("command"), str) or not server["command"]:
        raise AiError(f"vphone MCP config has no stdio server mcpServers.vphone.command: {config_path}")
    args = server.get("args") or []
    env = server.get("env") or {}
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise AiError(f"vphone MCP config args must be a string list: {config_path}")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise AiError(f"vphone MCP config env must map strings to strings: {config_path}")
    return {"command": server["command"], "args": list(args), "env": dict(env)}


def _tool_flags_claude(tools: str, mcp_config: str | None) -> list[str]:
    if tools == "none":
        # 真·一轮纯推理：--tools 空集合从可用工具面移除所有内置工具；空 allowlist
        # 再收紧授权面。tools=none 的上下文由 caller 内联进 prompt，不依赖工具读取。
        return ["--tools", "", "--allowed-tools", ""]
    if tools == "device":
        # 经 vphone_mcp 驱动真机（BL-067）：`--tools ""` 移除全部内置工具（官方 CLI 参考：该参数
        # 不影响 MCP 工具），`--strict-mcp-config` 只加载这份 MCP 配置，`--allowed-tools` 放行
        # vphone 工具免提示。受管与非受管路径用同一组参数；build_argv 对 device 不加 --safe-mode，
        # 因为 2.1.299 的 --safe-mode 不加载 --mcp-config 的服务器（本机桩实测，见
        # docs/platform-skill-boundary.md §9.3）。device 进程的工作目录是隔离临时目录
        # （`claude_device_isolation`），配置路径因此取绝对路径。
        cfg = os.path.abspath(mcp_config or resolve_vphone_mcp_config())
        return ["--mcp-config", cfg, "--strict-mcp-config", "--tools", "",
                "--allowed-tools", "mcp__vphone__*"]
    return ["--permission-mode", "acceptEdits"]  # full


def _claude_isolation_flags(tools: str) -> list[str]:
    """claude 的定制隔离参数。

    tools=none/full 用 `--safe-mode` 关闭 CLAUDE.md、skills、插件、hooks 与 MCP 等定制。
    tools=device 不能用它：`--safe-mode` 同时丢弃 `--mcp-config` 的服务器，设备工具面会变成空集。
    device 改用 `--setting-sources ""`（不读 user/project/local settings，因而不加载其中的 hooks、
    插件、权限规则与 user/project CLAUDE.md）加 `--strict-mcp-config`（见 `_tool_flags_claude`）；
    `--setting-sources` 不控制的 auto memory 由 `claude_device_isolation` 的环境变量关闭。
    """
    if tools == "device":
        return ["--setting-sources", ""]
    return ["--safe-mode"]


#: claude device 通道的上下文隔离环境变量（BL-067 控费）。出处：官方环境变量参考
#: https://code.claude.com/docs/en/env-vars ——`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`「prevent loading any
#: CLAUDE.md memory files into context, including user, project, and auto memory files」；
#: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 关闭 auto memory。官方 Agent SDK 文档
#: https://code.claude.com/docs/en/agent-sdk/claude-code-features 「What settingSources does not control」
#: 写明 auto memory（`~/.claude/projects/<project>/memory/`）不受 setting sources 控制，在会话开始时载入系统提示。
CLAUDE_DEVICE_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def claude_device_isolation(env: Mapping[str, str]) -> tuple[dict[str, str], tempfile.TemporaryDirectory]:
    """claude device 子进程的环境与工作目录：关闭记忆文件加载，工作目录换成空的临时目录。

    工作目录决定 CLAUDE.md 向上查找的起点、auto memory 的项目键与系统提示中的 cwd / git 状态段
    （官方 memory 文档「How CLAUDE.md files load」）。空临时目录使三者都不指向仓库。调用方负责
    `cleanup()` 返回的临时目录。
    """
    out = dict(env)
    out.update(CLAUDE_DEVICE_ISOLATION_ENV)
    return out, tempfile.TemporaryDirectory(prefix="autophone-claude-device-")


def _claude_image_refs(req: AiRequest) -> str:
    # device 进程的工作目录是隔离临时目录，相对路径会失效，统一转绝对路径。
    paths = [os.path.abspath(p) if req.tools == "device" else str(p) for p in req.images]
    return "\n\n参考图片：\n" + "\n".join("@" + p for p in paths)


def claude_stream_events(stdout: str) -> list[dict[str, Any]]:
    """claude `--output-format json`（单个信封）或 `stream-json`（每行一个事件）的 JSON 对象序列。"""
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def claude_result_event(stdout: str) -> dict[str, Any] | None:
    """最后一条 `type=result` 事件（JSON 信封本身也是 result 事件）；没有时返回 None。

    事件结构出处：官方 Agent SDK TypeScript 参考 `SDKResultMessage`
    （https://code.claude.com/docs/en/agent-sdk/typescript）：`result`、`total_cost_usd`、`usage`、
    `modelUsage`、`permission_denials`；错误分支的 `subtype` 含 `error_max_budget_usd`。
    """
    found = None
    for event in claude_stream_events(stdout):
        if event.get("type") == "result":
            found = event
    return found


def _claude_init_model(stdout: str) -> str | None:
    for event in claude_stream_events(stdout):
        if event.get("type") == "system" and event.get("subtype") == "init" and event.get("model"):
            return str(event["model"])
    return None


def _claude_usage_from_event(event: Mapping[str, Any] | None) -> tuple[dict | None, list[str] | None]:
    if not event:
        return None, None
    usage = event.get("usage")
    model_usage = event.get("modelUsage")
    return (
        usage if isinstance(usage, dict) else None,
        sorted(str(name) for name in model_usage) if isinstance(model_usage, dict) else None,
    )


def _finite_cost(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


_CLAUDE_VPHONE_PREFIX = "mcp__vphone__"


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text")
    return ""


def claude_device_tool_trace(stdout: str) -> dict[str, Any]:
    """从 claude `stream-json` 事件流统计 vphone MCP 调用，结构与 `codex_device_tool_trace` 相同。

    出处（官方 Agent SDK TypeScript 参考，https://code.claude.com/docs/en/agent-sdk/typescript）：
    `SDKAssistantMessage.message` 为 Anthropic `BetaMessage`，其 `tool_use` 内容块带 `id` 与 `name`；
    `SDKUserMessage.message` 为 `MessageParam`，工具结果是 `tool_result` 内容块（`tool_use_id`、
    `is_error`）；`SDKResultMessage.permission_denials` 是被拒工具调用的权威记录（`tool_name`、
    `tool_use_id`）。只统计名称以 `mcp__vphone__` 开头的调用。

    - attempted：vphone `tool_use` 块数；completed：对应 `tool_result` 且 `is_error` 不为真；
    - failed：对应 `tool_result.is_error` 为真，或出现在 `permission_denials`；
    - denied：出现在 `permission_denials` 的 vphone 调用（denied ⊆ failed）。
    没有结果块的调用（例如 CLI 在工具执行前因预算停止）只计入 attempted。
    """
    trace: dict[str, Any] = {"server": "vphone", "attempted": 0, "completed": 0,
                             "failed": 0, "denied": 0, "errors": []}
    names: dict[str, str] = {}
    outcomes: dict[str, tuple[bool, str]] = {}
    denied: dict[str, str] = {}
    for event in claude_stream_events(stdout):
        kind = event.get("type")
        content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and str(block.get("name") or "").startswith(_CLAUDE_VPHONE_PREFIX)):
                    names.setdefault(str(block.get("id")), str(block["name"]))
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outcomes[str(block.get("tool_use_id"))] = (
                        bool(block.get("is_error")), _tool_result_text(block.get("content")))
        elif kind == "result":
            for item in event.get("permission_denials") or []:
                if isinstance(item, dict) and str(item.get("tool_name") or "").startswith(_CLAUDE_VPHONE_PREFIX):
                    denied[str(item.get("tool_use_id"))] = str(item["tool_name"])
    for use_id, name in denied.items():
        names.setdefault(use_id, name)
    for use_id, name in names.items():
        trace["attempted"] += 1
        if use_id in denied:
            trace["failed"] += 1
            trace["denied"] += 1
            message = outcomes.get(use_id, (True, "permission denied"))[1] or "permission denied"
        elif use_id in outcomes:
            is_error, message = outcomes[use_id]
            if not is_error:
                trace["completed"] += 1
                continue
            trace["failed"] += 1
        else:
            continue
        if len(trace["errors"]) < 5:
            short = name[len(_CLAUDE_VPHONE_PREFIX):]
            trace["errors"].append(_short(redact_text(f"{short}: {message}"), _FAILURE_PART_CAP))
    return trace


# 仅显式值 0 允许 CLI 使用 OAuth/keychain；默认保留原有 bare 行为。
class _ClaudeBackend:
    name = "claude"
    binary = "claude"

    def version_argv(self) -> list[str]:
        return [self.binary, "--version"]

    @staticmethod
    def _stream_mode(req: AiRequest) -> bool:
        # device 通道总用 stream-json（BL-067）：工具调用与结果逐条可见，可统计 device_tool_trace；
        # 结构化输出仍从最后的 result 事件取。`-p --json-schema` 在 2.1.177 实测挂死，沿用 JSON 指令。
        return req.tools == "device"

    def build_argv(self, req: AiRequest, schema_path: Path | None) -> list[str]:
        # 注意：claude 2.1.177 的 `-p --json-schema` 实测会挂死（结构化输出路径卡住），
        # 所以 json 模式不用 --json-schema，改 prompt 指令 JSON + 解析 result 串（见 parse）。
        prompt = req.prompt + (_json_directive(req.json_schema) if req.json_schema is not None else "")
        # claude -p 模式用 @path 引用会被 CLI 在发模型前内联（含图片），不依赖 Read 工具权限，
        # 所以 tools=none（--allowed-tools ""）也能喂截图做纯推理。
        if req.images:
            prompt += _claude_image_refs(req)
        argv = [
            self.binary,
            *([] if os.environ.get("AUTOPHONE_CLAUDE_BARE") == "0" else ["--bare"]),
            *_claude_isolation_flags(req.tools),
            "--no-session-persistence",
            "--disable-slash-commands",
            "-p",
            prompt,
        ]
        if self._stream_mode(req):
            # print 模式下 stream-json 须配 --verbose（与 build_argv_stream 相同）
            argv += ["--output-format", "stream-json", "--verbose"]
        else:
            argv += ["--output-format", "json" if req.json_schema is not None else "text"]
        if req.system:
            argv += ["--append-system-prompt", req.system]
        for d in req.context_dirs:
            argv += ["--add-dir", str(d)]
        if req.model:
            argv += ["--model", req.model]
        if req.cli_budget_usd() is not None:
            argv += ["--max-budget-usd", str(req.cli_budget_usd())]
        argv += _tool_flags_claude(req.tools, os.environ.get("AUTOPHONE_VPHONE_MCP_CONFIG"))
        return argv

    def build_argv_stream(self, req: AiRequest) -> list[str]:
        """流式版 argv：--output-format stream-json + --verbose（print 模式下 stream-json 强制要 verbose）。"""
        prompt = req.prompt
        if req.images:
            prompt += _claude_image_refs(req)
        argv = [
            self.binary,
            *([] if os.environ.get("AUTOPHONE_CLAUDE_BARE") == "0" else ["--bare"]),
            *_claude_isolation_flags(req.tools),
            "--no-session-persistence",
            "--disable-slash-commands",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if req.system:
            argv += ["--append-system-prompt", req.system]
        for d in req.context_dirs:
            argv += ["--add-dir", str(d)]
        if req.model:
            argv += ["--model", req.model]
        if req.cli_budget_usd() is not None:
            argv += ["--max-budget-usd", str(req.cli_budget_usd())]
        argv += _tool_flags_claude(req.tools, os.environ.get("AUTOPHONE_VPHONE_MCP_CONFIG"))
        return argv

    def parse(self, req: AiRequest, c: _Completed) -> tuple[str, dict | None, float | None, str | None]:
        """→ (text, json, cost_usd, model)。json / stream-json 的 result 事件是信封，result 是模型文本。"""
        if req.json_schema is None and not self._stream_mode(req):
            return c.stdout.strip(), None, None, None
        import json as _json
        if self._stream_mode(req):
            if not claude_stream_events(c.stdout):
                # 非 JSON 输出（注入的文本 runner 等）原样返回，与 codex device 的处理相同；
                # 有事件却没有 result 事件时 fail-closed。
                text = c.stdout.strip()
                return text, (_extract_json(text) if req.json_schema is not None else None), None, None
            envelope = claude_result_event(c.stdout)
            if envelope is None:
                raise AiError("claude stream-json output has no result event")
            model = _claude_init_model(c.stdout)
        else:
            envelope = _json.loads(c.stdout)  # 信封解析不过 → 上层 fail-closed
            model = None
        result = envelope.get("result")
        cost = _finite_cost(envelope.get("total_cost_usd"))
        if req.json_schema is None:
            return str(result or "").strip(), None, cost, model
        parsed = result if isinstance(result, dict) else _extract_json(str(result))
        text = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False)
        return text, parsed, cost, model

    def usage_metadata(self, req: AiRequest, c: _Completed) -> tuple[dict | None, list[str] | None]:
        """result 事件里的 `usage` 与 `modelUsage` 键名（实际模型标识）；文本模式没有信封。"""
        if req.json_schema is None and not self._stream_mode(req):
            return None, None
        if self._stream_mode(req):
            return _claude_usage_from_event(claude_result_event(c.stdout))
        envelope = json.loads(c.stdout)
        return _claude_usage_from_event(envelope if isinstance(envelope, dict) else None)


# ─────────────── codex tools=device 通道（BL-067）───────────────
# 出处（codex-cli 0.159.3）：键名与取值由本机 CLI 的配置解析报错核对（`unknown variant …,
# expected one of …`），语义见 openai/codex tag rust-v0.159.3 的 codex-rs/config/src/mcp_types.rs、
# codex-rs/core/src/mcp_tool_call.rs、codex-rs/core/src/tools/spec_plan.rs 与官方配置参考。
#
# - `default_tools_approval_mode="approve"`：该服务器工具调用不进入审批。缺省 `auto` 对未标注
#   readOnlyHint 的工具（go_home、tap 等）要求审批，而 `codex exec` 的审批策略默认 `never`，
#   调用即被拒（"MCP tool call requires approval, but approval policy is never"），CLI 仍退出 0。
# - `required=true`：vphone MCP 初始化失败时 `codex exec` 非零退出，不会静默以零工具运行。
# - `-s read-only` + `approval_policy="never"`：内置执行面只读、无网络、不提权。
# - `--disable shell_tool` / `view_image` / `multi_agent` / `goals` 与 `web_search="disabled"`：
#   从模型工具面移除命令执行、本地读图、子代理、目标与网页搜索，对齐 claude 的 `--tools ""`。
_CODEX_VPHONE_KEY = "mcp_servers.vphone."
_CODEX_DEVICE_SANDBOX_FLAGS = [
    "-s", "read-only",
    "-c", 'approval_policy="never"',
    "-c", 'web_search="disabled"',
    "--disable", "shell_tool",
    "--disable", "view_image",
    "--disable", "multi_agent",
    "--disable", "goals",
]
DEVICE_TOOL_CALL_DENIED = "device_tool_call_denied"
DEVICE_TRACE_EMPTY = "device_trace_empty"
#: 非受管 tools=device 调用（无 StageGuard）在控制面已配置时的拒绝码（2026-10-14 裁定）。
DEVICE_AUTHORITY_REQUIRED = "device_authority_required"
# codex 拒绝 MCP 调用时 item.error.message 的特征文本（core/src/mcp_tool_call.rs 的拒绝路径）。
_CODEX_DENIAL_MARKERS = ("requires approval", "approval policy", "blocked by", "rejected")


def _toml_inline_table(values: Mapping[str, str]) -> str:
    # JSON 字符串转义是 TOML basic string 的子集；键一律加引号。
    return "{" + ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in values.items()) + "}"


def codex_vphone_mcp_flags(server: Mapping[str, Any]) -> list[str]:
    """把一份 vphone MCP stdio 配置翻成 `codex exec -c` 覆写，并固定放行与 required。"""
    flags = [
        "-c", _CODEX_VPHONE_KEY + "command=" + json.dumps(server["command"]),
        "-c", _CODEX_VPHONE_KEY + "args=" + json.dumps(list(server.get("args") or [])),
    ]
    env = server.get("env") or {}
    if env:
        flags += ["-c", _CODEX_VPHONE_KEY + "env=" + _toml_inline_table(env)]
    flags += [
        "-c", _CODEX_VPHONE_KEY + 'default_tools_approval_mode="approve"',
        "-c", _CODEX_VPHONE_KEY + "required=true",
    ]
    return flags


def _strip_codex_vphone_flags(argv: list[str]) -> list[str]:
    """去掉 argv 里既有的 `-c mcp_servers.vphone.*` 覆写对（受管注入前清掉非受管直连配置）。"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "-c" and i + 1 < len(argv) and str(argv[i + 1]).startswith(_CODEX_VPHONE_KEY):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def codex_device_tool_trace(stdout: str) -> dict[str, Any]:
    """从 `codex exec --json` 事件流统计 vphone MCP 调用；只读 item.completed 的 mcp_tool_call。"""
    trace: dict[str, Any] = {"server": "vphone", "attempted": 0, "completed": 0,
                             "failed": 0, "denied": 0, "errors": []}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") != "mcp_tool_call" or item.get("server") != "vphone":
            continue
        trace["attempted"] += 1
        status = item.get("status")
        if status == "completed":
            trace["completed"] += 1
            continue
        trace["failed"] += 1
        err = item.get("error")
        message = str(err.get("message") if isinstance(err, dict) else err or status or "")
        if any(marker in message.lower() for marker in _CODEX_DENIAL_MARKERS):
            trace["denied"] += 1
        if len(trace["errors"]) < 5:
            text = _short(redact_text(f"{item.get('tool')}: {message}"), _FAILURE_PART_CAP)
            trace["errors"].append(text)
    return trace


def require_device_actions(result: AiResult) -> AiResult:
    """计划执行类调用：有设备工具留痕却没有一次成功的设备调用 → 判失败，不报告为执行成功。

    只对带 `device_tool_trace` 的结果生效（当前为 codex device 通道）；claude 文本模式没有事件流，
    保持原判定。"""
    trace = result.device_tool_trace
    if not result.ok or trace is None or trace.get("completed", 0) > 0:
        return result
    result.ok = False
    result.error_code = DEVICE_TRACE_EMPTY
    result.error = (f"{result.provider} finished without any completed vphone MCP call "
                    f"(attempted={trace.get('attempted', 0)}); device actions were not executed")
    return result


class _CodexBackend:
    name = "codex"
    binary = "codex"

    def version_argv(self) -> list[str]:
        return [self.binary, "--version"]

    def effective_model(self, req: AiRequest) -> str | None:
        model = (req.model or os.environ.get("AUTOPHONE_CODEX_MODEL") or "").strip()
        return model or None

    def validate_request(self, req: AiRequest) -> str | None:
        effective = self.effective_model(req)
        model = (effective or "").lower()
        if not model:
            return None
        if model.startswith("claude") or "opus" in model or "sonnet" in model:
            return f"model {effective!r} is not a Codex/OpenAI model; clear model or choose claude provider"
        if model in {"gpt-5.5", "gpt-5-5"}:
            return f"model {effective!r} is not available to codex; clear model or use a supported Codex model"
        return None

    def build_argv(self, req: AiRequest, schema_path: Path | None) -> list[str]:
        # Platform AI calls are isolated one-shot jobs.  Do not let Codex session/plugin
        # persistence contaminate them (or make a stale local session cache turn a valid
        # lift into ai_error fallbacks).
        argv = [
            self.binary,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
        ]
        if schema_path is not None or req.tools in {"none", "device"}:
            # none/device 均用 JSONL 事件流；最终文本取 agent_message，工具事件可核对。
            argv += ["--json"]
        if req.system:
            # Keep application instructions separate from task/evidence text.  The
            # CLI parses -c values as TOML; JSON string quoting preserves newlines.
            argv += ["-c", "developer_instructions=" + json.dumps(req.system, ensure_ascii=False)]
        argv += [req.prompt]
        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]
        for d in req.context_dirs:
            argv += ["--add-dir", str(d)]
        for img in req.images:
            argv += ["-i", str(Path(img).resolve()) if req.tools == "none" else str(img)]
        model = self.effective_model(req)
        if model:
            argv += ["-m", model]
        if req.tools == "none":
            from autophone.application.codex_analysis import toolless_flags
            argv += ["-s", "read-only", *toolless_flags()]
        elif req.tools == "full":
            argv += ["-s", "workspace-write"]
        elif req.tools == "device":
            # 非受管路径的 MCP 来源与 claude 一致（resolve_vphone_mcp_config）；受管路径由
            # _run_with_device_authority 换成私有代理 socket。读不出 vphone 服务器即抛 AiError。
            cfg = os.environ.get("AUTOPHONE_VPHONE_MCP_CONFIG") or resolve_vphone_mcp_config()
            argv += _CODEX_DEVICE_SANDBOX_FLAGS
            argv += codex_vphone_mcp_flags(load_vphone_mcp_server(cfg))
        return argv

    def parse(self, req: AiRequest, c: _Completed) -> tuple[str, dict | None, float | None, str | None]:
        text = c.stdout.strip()
        if req.json_schema is None and req.tools in {"none", "device"}:
            final, saw_event = None, False
            for line in text.splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                saw_event = saw_event or isinstance(obj, dict)
                item = obj.get("item") if isinstance(obj, dict) else None
                if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    final = item["text"]
            if final is not None:
                return final.strip(), None, None, None
            if saw_event and req.tools == "none":
                raise AiError("codex tools=none JSON event stream has no final agent_message")
            # 注入的纯文本 runner 保持兼容；device 的空留痕由设备通道门禁校验。
            return ("" if saw_event else text), None, None, None
        if req.json_schema is None:
            return text, None, None, None
        import json as _json
        # `codex exec --json --output-schema` emits JSONL events. The final answer sits in the
        # last agent_message item; parse that message, not the whole event stream.
        messages: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = _json.loads(line)
            item = obj.get("item") if isinstance(obj, dict) else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                msg = item.get("text")
                if isinstance(msg, str):
                    messages.append(msg)
        if not messages:
            parsed = _json.loads(text)
            return text, parsed, None, None
        final = messages[-1].strip()
        parsed = _extract_json(final)
        return final, parsed, None, None

    def usage_metadata(self, req: AiRequest, c: _Completed) -> tuple[dict | None, list[str] | None]:
        """`codex exec --json` 最后一条 `turn.completed` 事件的 usage；事件流不回报模型标识。"""
        if req.json_schema is None and req.tools not in {"none", "device"}:
            return None, None
        import json as _json
        usage = None
        for line in c.stdout.splitlines():
            if not line.strip():
                continue
            obj = _json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "turn.completed":
                candidate = obj.get("usage")
                usage = candidate if isinstance(candidate, dict) else usage
        return usage, None


_BACKENDS = {"claude": _ClaudeBackend(), "codex": _CodexBackend()}

_CODEX_TRANSIENT_MODEL_CACHE_ERRORS = (
    "codex_models_manager::cache: failed to load models cache",
    "codex_models_manager::manager: failed to refresh available models: timeout waiting for child process to exit",
)
_CODEX_VERSION_CACHE: dict[str, str | None] = {}
_CODEX_VERSION_CACHE_LOCK = threading.Lock()


def _codex_stdout_is_pre_inference(stdout: str) -> bool:
    """Codex JSON mode emits thread.started before it reaches model inference."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return True
    try:
        events = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return False
    return all(isinstance(event, dict) and event.get("type") == "thread.started" for event in events)


def _retryable_codex_model_cache_exit(completed: _Completed) -> bool:
    """True only when Codex died before inference on its local model-cache refresh path."""
    return (
        completed.returncode != 0
        and _codex_stdout_is_pre_inference(completed.stdout)
        and any(marker in completed.stderr for marker in _CODEX_TRANSIENT_MODEL_CACHE_ERRORS)
    )


_FAILURE_DETAIL_CAP = 500
_FAILURE_PART_CAP = 300
_CLAUDE_TEXT_STDOUT_CAP = 200


def _short(text: str, cap: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _api_error_summary(message: str) -> str:
    """Codex 把上游 HTTP 错误体作为 JSON 字符串放进 message；只取 code/message/param/status。"""
    start = message.find("{")
    if start < 0:
        return message
    try:
        doc = json.loads(message[start:])
    except ValueError:
        return message
    if not isinstance(doc, dict):
        return message
    err = doc.get("error") if isinstance(doc.get("error"), dict) else doc
    head = [str(err[key]) for key in ("code", "message") if err.get(key)]
    if not head:
        return message
    extra = []
    if err.get("param"):
        extra.append(f"param={err['param']}")
    status = doc.get("status") or err.get("status")
    if status:
        extra.append(f"status={status}")
    prefix = message[:start].strip()
    summary = ": ".join(head) + (f" ({', '.join(extra)})" if extra else "")
    return f"{prefix} {summary}" if prefix else summary


def _stdout_error_messages(provider: str, stdout: str) -> list[str]:
    """从非零退出时的 stdout 提取 provider 自报的错误，不回传模型正文。

    - codex ``--json``：JSONL 中 ``type`` 为 ``error`` 的 ``message`` 与 ``turn.failed`` 的 ``error.message``；
    - claude ``--output-format json``：``is_error`` 为真的结果信封的 ``result``（如未登录提示）。
    无可识别错误时返回空列表；纯文本 stdout 不作为错误来源，避免把模型输出写进错误。
    """
    messages: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        raw: Any = None
        if kind == "error":
            raw = event.get("message")
        elif kind == "turn.failed":
            err = event.get("error")
            raw = err.get("message") if isinstance(err, dict) else err
        elif provider == "claude" and kind == "result" and event.get("is_error"):
            raw = event.get("result") or event.get("subtype")
            status = event.get("api_error_status")
            if raw and status:
                raw = f"{raw} (status={status})"
        if raw:
            text = _short(redact_text(_api_error_summary(str(raw))), _FAILURE_PART_CAP)
            if text not in messages:
                messages.append(text)
    return messages


def _nonzero_exit_error(provider: str, completed: _Completed) -> str:
    """非零退出的错误文本：stdout 中 provider 自报的错误在前，stderr 在后，整体截断并脱敏。"""
    stdout = (completed.stdout or "").strip()
    reported = _stdout_error_messages(provider, stdout)
    stderr = _short(redact_text((completed.stderr or "").strip()), _FAILURE_PART_CAP)
    if not reported and not stderr and provider == "claude" and stdout and not stdout.startswith("{"):
        # claude 文本模式未登录等错误只写 stdout 且 stderr 为空；只取短前缀，避免回传模型正文。
        reported = [f"stdout: {_short(redact_text(stdout), _CLAUDE_TEXT_STDOUT_CAP)}"]
    parts = list(reported)
    if stderr:
        parts.append(f"stderr: {stderr}" if reported else stderr)
    head = f"{provider} exited {completed.returncode}: "
    return head + _short(" | ".join(parts), _FAILURE_DETAIL_CAP - len(head))


#: CLI 未认证或凭据无效的结构化拒绝码（BL-067）。
AI_CLI_NOT_AUTHENTICATED = "ai_cli_not_authenticated"

# 认证失败的特征文本，取自本机 CLI 输出（2026-10-14，Claude Code 2.1.299 / codex-cli 0.159.3）：
# - claude 无凭据（`--bare` 且无 ANTHROPIC_API_KEY）：text / json / stream-json 三种输出均为
#   `Not logged in · Please run /login`，退出 1；stream-json 的 assistant 事件带 `error: "authentication_failed"`。
# - claude API key 被拒（本地桩返回 401）：result `Invalid API key · Fix external API key`，`api_error_status: 401`。
# - codex 无凭据：`codex login status` 输出 `Not logged in`；`codex exec --json` 的 error / turn.failed 事件为
#   `unexpected status 401 Unauthorized: …`，退出 1。ChatGPT 登录过期等其他形态的文本未观察，待验证。
_CLAUDE_AUTH_MARKERS = ("not logged in", "please run /login", "invalid api key")
_CODEX_AUTH_MARKERS = ("401 unauthorized", "not logged in")


def _auth_failure(provider: str, completed: _Completed) -> bool:
    """非零退出是否为 CLI 认证失败（未登录 / 凭据无效）。只读 provider 自报的错误与 stderr。"""
    stdout = completed.stdout or ""
    texts = [completed.stderr or ""]
    if provider == "claude":
        for event in claude_stream_events(stdout):
            if event.get("type") == "assistant" and event.get("error") == "authentication_failed":
                return True
            if event.get("type") == "result" and event.get("is_error"):
                if event.get("api_error_status") == 401:
                    return True
                texts.append(str(event.get("result") or ""))
        if not stdout.lstrip().startswith("{"):
            texts.append(stdout[:_CLAUDE_TEXT_STDOUT_CAP])
        markers = _CLAUDE_AUTH_MARKERS
    else:
        texts += _stdout_error_messages(provider, stdout)
        markers = _CODEX_AUTH_MARKERS
    lowered = " ".join(texts).lower()
    return any(marker in lowered for marker in markers)


def _failure_text(provider: str, req: AiRequest, completed: _Completed) -> str:
    """非零退出时保留的 `AiResult.text`：claude stream-json 只保留 result 事件（不回传含截图的整段事件流）。"""
    if provider == "claude" and req.tools == "device":
        event = claude_result_event(completed.stdout)
        return json.dumps(event, ensure_ascii=False) if event is not None else ""
    return (completed.stdout or "").strip()


def _failure_metadata(provider: str, req: AiRequest, completed: _Completed
                      ) -> tuple[float | None, dict | None, list[str] | None, str | None]:
    """非零退出时仍从 claude 结果信封回读成本、用量、模型标识（BL-067：预算停止时照旧留痕）。"""
    if provider != "claude":
        return None, None, None, None
    event = claude_result_event(completed.stdout)
    if event is None:
        return None, None, None, None
    usage, models = _claude_usage_from_event(event)
    return _finite_cost(event.get("total_cost_usd")), usage, models, _claude_init_model(completed.stdout)


def _normalize_provider_choice(provider: str | None) -> str | None:
    choice = (provider or "").strip().lower()
    if not choice:
        return None
    if choice != "auto" and choice not in _BACKENDS:
        raise AiError(f"unknown AI provider: {choice}")
    return choice


def _resolve_backend_executable(backend: Any) -> str | None:
    """Resolve one CLI path; Codex prefers ChatGPT.app over a PATH installation."""
    if backend.name == "codex":
        configured = (os.environ.get("AUTOPHONE_CODEX_BIN") or "").strip()
        if configured:
            return shutil.which(configured)
        bundled = shutil.which(CHATGPT_APP_CODEX_BINARY)
        if bundled:
            return bundled
    return shutil.which(backend.binary)


def _missing_executable_error(backend: Any) -> str:
    if backend.name == "codex":
        return (
            "codex executable not found (checked AUTOPHONE_CODEX_BIN, "
            f"{CHATGPT_APP_CODEX_BINARY}, and PATH)"
        )
    return f"{backend.binary} not found on PATH"


def resolve_provider(requested: str | None) -> str:
    """requested > AUTOPHONE_AI_PROVIDER > default; auto resolves the first installed CLI."""
    requested_choice = _normalize_provider_choice(requested)
    choice = (
        requested_choice
        or _normalize_provider_choice(os.environ.get("AUTOPHONE_AI_PROVIDER"))
        or DEFAULT_PROVIDER
    )
    if choice == "auto":
        for name in PROVIDERS:
            if _resolve_backend_executable(_BACKENDS[name]):
                return name
        raise AiError("no AI provider CLI found on PATH (claude/codex)")
    return choice


def _detect_version(
    backend: Any,
    runner: CommandRunner,
    *,
    executable_path: str | None = None,
) -> str | None:
    argv = backend.version_argv()
    if executable_path:
        argv[0] = executable_path
    try:
        c = runner(argv, timeout=10.0, env=dict(os.environ))
    except Exception:  # noqa: BLE001 - version 探测失败不致命
        return None
    return c.stdout.strip() or None if c.returncode == 0 else None


def _detect_version_for_invoke(
    backend: Any,
    runner: CommandRunner,
    executable_path: str,
) -> str | None:
    """Cache Codex version per resolved executable path for this server process."""
    if backend.name != "codex":
        return _detect_version(backend, runner, executable_path=executable_path)
    cache_key = os.path.abspath(executable_path)
    try:
        identity = Path(executable_path).stat()
        cache_key += f":{identity.st_ino}:{identity.st_size}:{identity.st_mtime_ns}"
    except OSError:
        pass  # Missing executables remain covered by resolution / invocation failure.
    with _CODEX_VERSION_CACHE_LOCK:
        if cache_key not in _CODEX_VERSION_CACHE:
            _CODEX_VERSION_CACHE[cache_key] = _detect_version(
                backend,
                runner,
                executable_path=executable_path,
            )
        return _CODEX_VERSION_CACHE[cache_key]


def unmanaged_device_refusal(req: AiRequest) -> str | None:
    """非受管 tools=device 调用在控制面已配置时的拒绝原因；允许执行时返回 None。

    受管路径（本线程有 StageGuard）经私有代理 socket 访问设备，不在此拒绝。非受管路径会让
    vphone MCP 直连 `VPHONE_SOCK`：只有未配置控制面的单 VM 兼容模式允许。控制面是否已配置
    复用 `configured_control_plane()` 的三态判定（与 serve / CLI / Rig 同一口径）：返回 None
    为未配置；返回实例或抛 `ControlPlaneUnavailable` 均视为已配置，一律拒绝，不回退直连。
    调用方须在起任何 CLI 进程（含 `--version` 探测）之前调用。
    """
    if req.tools != "device":
        return None
    from autophone.device.stage_guard import StageGuard
    try:
        if StageGuard.for_device_call() is not None:
            return None
    except Exception:  # noqa: BLE001 - 外来或缺失的 Stage 由 _run_with_device_authority 照旧拒绝
        return None
    from autophone.application.control_plane import ControlPlaneUnavailable, configured_control_plane
    try:
        control = configured_control_plane()
    except ControlPlaneUnavailable as exc:
        return ("tools=device refused: control plane is configured but unavailable; device access "
                f"must run under a Control Plane Assignment, not direct VPHONE_SOCK ({exc})")
    if control is None:
        return None
    return ("tools=device refused: control plane is configured; device access must run under a "
            "Control Plane Assignment (StageGuard), not direct VPHONE_SOCK")


def unmanaged_device_refusal_result(req: AiRequest) -> AiResult | None:
    """`unmanaged_device_refusal` 的 AiResult 形态，供在取 ScreenLock 之前拒绝的调用方使用。"""
    refusal = unmanaged_device_refusal(req)
    if not refusal:
        return None
    try:
        provider = resolve_provider(req.provider)
    except AiError:
        provider = str(req.provider or DEFAULT_PROVIDER)
    return AiResult(ok=False, text="", provider=provider, model=req.model,
                    error_code=DEVICE_AUTHORITY_REQUIRED, error=refusal)


def _run_with_device_authority(req, runner, argv, **kwargs):
    if req.tools != "device":
        return runner(argv, **kwargs)
    from autophone.device.stage_guard import StageGuard, OperationClass
    from autophone.device.agent_proxy import AgentDeviceProxy
    guard = StageGuard.for_device_call()
    if guard is None:
        return runner(argv, **kwargs)
    with guard.operation(OperationClass.FRIDA), AgentDeviceProxy() as proxy:
        config = Path(proxy.directory.name) / "mcp.json"
        server = {
            "command": "uv",
            "args": ["run", "--project", str(CODE_ROOT / "vphone_mcp"), "vphone-mcp"],
            "env": {"VPHONE_SOCK": proxy.path},
        }
        write_json(config, {"mcpServers": {"vphone": server}})
        command = list(argv)
        if "--mcp-config" in command:
            command[command.index("--mcp-config") + 1] = str(config)
            # claude device 参数已含 --strict-mcp-config 与 --tools ""；只补缺，不重复
            if "--strict-mcp-config" not in command:
                command.append("--strict-mcp-config")
            if "--tools" not in command:
                command += ["--tools", ""]
        else:
            # codex：先清掉非受管直连配置（真实 socket），再注入私有代理 socket，审批与 required
            # 口径与非受管路径相同（codex_vphone_mcp_flags）。
            command = _strip_codex_vphone_flags(command) + codex_vphone_mcp_flags(server)
        env = dict(kwargs.pop("env"))
        env["VPHONE_SOCK"] = proxy.path
        env["AUTOPHONE_MANAGED_DEVICE_AGENT"] = "1"
        result = runner(command, env=env, **kwargs)
        guard.assert_allowed(OperationClass.FRIDA)
        return result


def context_governance_error(req: AiRequest) -> str | None:
    """loader 门禁 3：`--add-dir` 只能指向受治理的 ai-context 净化包。

    模型能看到的只应是**渲染后的 bundle**；把 run_dir、仓库或任意目录挂进去，等于让它顺着
    指针自己取内容，绕过 §5.2 的 artifact 治理。判据是目录里有 ``ai-context-manifest.json``
    （`build_ai_context` 写的身份文件）。不通过即 fail-closed，不降级放行。
    """
    ungoverned = [
        str(d) for d in req.context_dirs
        if not (Path(d) / AI_CONTEXT_MANIFEST).is_file()
    ]
    if not ungoverned:
        if req.tools == "none" and req.context_dirs:
            return "tools=none requires inline evidence and explicit images; context_dirs cannot be read"
        return None
    return (
        "context_dirs must be ai-context bundles produced by build_ai_context "
        f"(missing {AI_CONTEXT_MANIFEST}): {', '.join(ungoverned)}"
    )


def invoke(req: AiRequest, *, runner: CommandRunner = _subprocess_runner) -> AiResult:
    """调本地 agent CLI。fail-closed：缺 CLI / 非零退出 / 超时 / 要 JSON 却解析不出 → ok=false。"""
    import time

    provider = resolve_provider(req.provider)
    governance_error = context_governance_error(req)
    if governance_error:
        return AiResult(ok=False, text="", provider=provider, model=req.model, error=governance_error)
    refusal = unmanaged_device_refusal(req)
    if refusal:
        return AiResult(ok=False, text="", provider=provider, model=req.model,
                        error_code=DEVICE_AUTHORITY_REQUIRED, error=refusal)
    backend = _BACKENDS[provider]
    validator = getattr(backend, "validate_request", None)
    if validator is not None:
        validation_error = validator(req)
        if validation_error:
            return AiResult(ok=False, text="", provider=provider, model=req.model, error=validation_error)
    executable_path = _resolve_backend_executable(backend)
    if executable_path is None:
        return AiResult(ok=False, text="", provider=provider,
                        error=_missing_executable_error(backend))
    cli_version = _detect_version_for_invoke(backend, runner, executable_path)
    if provider == "codex" and req.tools == "none":
        from autophone.application.codex_analysis import SUPPORTED_TOOLLESS_CLI_VERSIONS
        if cli_version not in SUPPORTED_TOOLLESS_CLI_VERSIONS:
            return AiResult(ok=False, text="", provider=provider, model=req.model, cli_version=cli_version,
                            error_code="ai_tool_isolation_unsupported",
                            error=f"tools=none isolation is not verified for CLI {cli_version!r}; "
                                  f"supported: {sorted(SUPPORTED_TOOLLESS_CLI_VERSIONS)}")

    schema_path: Path | None = None
    tmpdir: tempfile.TemporaryDirectory | None = None
    if req.json_schema is not None:
        tmpdir = tempfile.TemporaryDirectory(prefix="autophone-ai-schema-")
        schema_path = Path(tmpdir.name) / "schema.json"
        write_json(schema_path, req.json_schema)

    try:
        argv = backend.build_argv(req, schema_path)
    except AiError as exc:  # device 通道缺 vphone MCP 配置等：起进程前 fail-closed
        if tmpdir is not None:
            tmpdir.cleanup()
        return AiResult(ok=False, text="", provider=provider, cli_version=cli_version, error=str(exc))
    argv[0] = executable_path
    run_env, run_kwargs, isolation = _provider_process(provider, req)
    started = time.monotonic()
    try:
        if provider == "codex" and req.tools == "none":
            from autophone.application.codex_analysis import write_toolless_catalog
            remaining_s = req.timeout_s - (time.monotonic() - started)
            if remaining_s <= 0:
                raise subprocess.TimeoutExpired(argv, req.timeout_s)
            catalog = write_toolless_catalog(argv, runner, env=run_env, directory=Path(isolation.name),
                                             timeout_s=remaining_s)
            argv += ["-c", "model_catalog_json=" + json.dumps(str(catalog))]
        for attempt in range(2):
            remaining_s = req.timeout_s - (time.monotonic() - started)
            if remaining_s <= 0:
                raise subprocess.TimeoutExpired(argv, req.timeout_s)
            c = _run_with_device_authority(req, runner, argv, timeout=remaining_s, env=dict(run_env),
                                           **run_kwargs)
            # Codex 0.142.x may reject a models_cache.json written by its own refresh,
            # then have the refresh child time out.  This happens before inference;
            # JSON mode may have emitted only thread.started.  One bounded retry is
            # therefore safe and avoids turning a recorded tap into a coordinate
            # fallback.  All other non-zero exits stay fail-closed and are never retried.
            if (
                provider == "codex"
                and req.tools == "none"
                and attempt == 0
                and _retryable_codex_model_cache_exit(c)
            ):
                continue
            break
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        partial = _Completed(1, decoded(exc.output), decoded(exc.stderr))
        usage, reported_models = _usage_metadata(backend, req, partial)
        error = f"{provider} timed out after {req.timeout_s}s"
        tool_events = []
        if provider == "codex" and req.tools == "none":
            from autophone.application.codex_analysis import unexpected_tool_events
            tool_events = unexpected_tool_events(partial.stdout)
            if tool_events:
                error += f"; codex tools=none unexpected tool events: {tool_events}"
        return AiResult(ok=False, text="" if tool_events else _failure_text(provider, req, partial),
                        provider=provider, cli_version=cli_version,
                        model=req.model, usage=usage, reported_models=reported_models,
                        error_code="ai_tool_isolation_violation" if tool_events else None,
                        duration_s=req.timeout_s, error=error)
    except Exception as exc:  # noqa: BLE001 - 启动失败本身是证据
        return AiResult(ok=False, text="", provider=provider, cli_version=cli_version,
                        model=req.model, error_code=getattr(exc, "code", None),
                        error=f"{provider} invocation error: {type(exc).__name__}: {exc}")
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()
        if isolation is not None:
            isolation.cleanup()
    duration = round(time.monotonic() - started, 3)

    trace = _device_tool_trace(provider, req, c.stdout)
    if provider == "codex" and req.tools == "none":
        from autophone.application.codex_analysis import unexpected_tool_events
        tool_events = unexpected_tool_events(c.stdout)
        if tool_events:
            usage, reported_models = _usage_metadata(backend, req, c)
            return AiResult(ok=False, text="", provider=provider, model=req.model,
                            cli_version=cli_version, duration_s=duration, usage=usage,
                            reported_models=reported_models, error_code="ai_tool_isolation_violation",
                            error=f"codex tools=none unexpected tool events: {tool_events}")
    if c.returncode != 0:
        return _nonzero_exit_result(provider, req, c, cli_version=cli_version, duration=duration, trace=trace)
    if trace is not None and trace["denied"]:
        # CLI 退出 0 不代表设备动作执行：被审批策略拒绝的 MCP 调用一律判失败（BL-067），
        # 且优先于输出解析失败报告，拒绝才是根因。
        try:
            denied_text = backend.parse(req, c)[0]
        except Exception:  # noqa: BLE001 - 失败结果的文本只作留痕
            denied_text = c.stdout.strip()
        return AiResult(ok=False, text=denied_text, provider=provider, model=req.model,
                        cli_version=cli_version, duration_s=duration, error_code=DEVICE_TOOL_CALL_DENIED,
                        error=(f"{provider} denied {trace['denied']} vphone MCP call(s): "
                               + " | ".join(trace["errors"])),
                        device_tool_trace=trace)
    try:
        text, parsed, cost, model = backend.parse(req, c)
    except Exception as exc:  # noqa: BLE001 - 要 JSON 却解析不出 → fail-closed
        return AiResult(ok=False, text=c.stdout.strip(), provider=provider, cli_version=cli_version,
                        duration_s=duration, error=f"{provider} output parse failed: {type(exc).__name__}: {exc}",
                        device_tool_trace=trace)
    usage, reported_models = _usage_metadata(backend, req, c)
    return AiResult(ok=True, text=text, provider=provider, json=parsed,
                    model=req.model or model, cli_version=cli_version, cost_usd=cost, duration_s=duration,
                    usage=usage, reported_models=reported_models, device_tool_trace=trace)


def _provider_process(provider: str, req: AiRequest
                      ) -> tuple[dict[str, str], dict[str, Any], tempfile.TemporaryDirectory | None]:
    """Codex none 与 Claude device 使用隔离工作目录；Claude device 另关闭记忆加载。"""
    if provider == "codex" and req.tools == "none":
        isolation = tempfile.TemporaryDirectory(prefix="autophone-codex-none-")
        cwd = Path(isolation.name) / "work"
        cwd.mkdir()
        return dict(os.environ), {"cwd": cwd}, isolation
    if provider == "claude" and req.tools == "device":
        env, isolation = claude_device_isolation(os.environ)
        return env, {"cwd": Path(isolation.name)}, isolation
    return dict(os.environ), {}, None


def _device_tool_trace(provider: str, req: AiRequest, stdout: str) -> dict[str, Any] | None:
    if req.tools != "device":
        return None
    if provider == "codex":
        return codex_device_tool_trace(stdout)
    return claude_device_tool_trace(stdout)


def _nonzero_exit_result(provider: str, req: AiRequest, c: _Completed, *, cli_version: str | None,
                         duration: float, trace: dict[str, Any] | None) -> AiResult:
    """非零退出 → ok=false；认证失败给 `ai_cli_not_authenticated`，claude 信封的成本、用量、模型照旧留痕。"""
    cost, usage, models, session_model = _failure_metadata(provider, req, c)
    return AiResult(ok=False, text=_failure_text(provider, req, c), provider=provider,
                    model=req.model or session_model, cli_version=cli_version, cost_usd=cost,
                    duration_s=duration, error=_nonzero_exit_error(provider, c),
                    usage=usage, reported_models=models,
                    error_code=AI_CLI_NOT_AUTHENTICATED if _auth_failure(provider, c) else None,
                    device_tool_trace=trace)


def _usage_metadata(backend: Any, req: AiRequest, c: _Completed) -> tuple[dict | None, list[str] | None]:
    """计量元数据只是留痕：读不出来时记 None，不把一次成功调用改判为失败。"""
    reader = getattr(backend, "usage_metadata", None)
    if reader is None:
        return None, None
    try:
        return reader(req, c)
    except Exception:  # noqa: BLE001 - 留痕字段，解析失败不影响调用结果
        return None, None


def invoke_streaming(req: AiRequest, *, on_event: Callable[[dict[str, Any]], None] | None = None,
                     runner: CommandRunner = _subprocess_stream_runner) -> AiResult:
    """像 invoke，但用 claude stream-json 增量解析：每个 timeline 事件回调 on_event（边跑边可见）。

    仅 claude backend + 非 JSON 模式支持流式；codex / json_schema 走普通 invoke 兜底。
    final 文本/cost/model 从最后那条 ``type:result`` 行取，与 ``--output-format json`` 的信封同源。
    fail-closed 同 invoke：缺 CLI / 非零退出 / 超时 → ok=false。
    """
    import json as _json
    import time

    provider = resolve_provider(req.provider)
    if provider != "claude" or req.json_schema is not None:
        return invoke(req, runner=runner)  # codex / 结构化输出：无流式增量，跑完才回
    governance_error = context_governance_error(req)
    if governance_error:
        return AiResult(ok=False, text="", provider=provider, model=req.model, error=governance_error)
    refusal = unmanaged_device_refusal(req)
    if refusal:
        return AiResult(ok=False, text="", provider=provider, model=req.model,
                        error_code=DEVICE_AUTHORITY_REQUIRED, error=refusal)
    backend = _BACKENDS["claude"]
    executable_path = _resolve_backend_executable(backend)
    if executable_path is None:
        return AiResult(ok=False, text="", provider=provider, error=_missing_executable_error(backend))
    cli_version = _detect_version(backend, runner, executable_path=executable_path)

    final: dict[str, Any] = {"text": None, "cost": None, "model": req.model}
    seq = [0]

    def _on_line(line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = _json.loads(line)
        except Exception:  # noqa: BLE001 - 非 JSON 行（极少）跳过，不污染 timeline
            return
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            final["model"] = obj.get("model") or final["model"]
        elif t == "result":
            final["text"] = obj.get("result")
            cost = obj.get("total_cost_usd")
            final["cost"] = float(cost) if cost is not None else None
        if on_event is not None:
            for ev in stream_events(obj):
                seq[0] += 1
                ev["seq"] = seq[0]
                on_event(ev)

    argv = backend.build_argv_stream(req)
    argv[0] = executable_path
    run_env, run_kwargs, isolation = _provider_process(provider, req)
    started = time.monotonic()
    try:
        c = _run_with_device_authority(req, runner, argv, timeout=req.timeout_s,
                                       env=dict(run_env), on_line=_on_line, **run_kwargs)
    except subprocess.TimeoutExpired:
        return AiResult(ok=False, text="", provider=provider, cli_version=cli_version,
                        duration_s=req.timeout_s, error=f"{provider} timed out after {req.timeout_s}s")
    except Exception as exc:  # noqa: BLE001 - 启动失败本身是证据
        return AiResult(ok=False, text="", provider=provider, cli_version=cli_version,
                        error=f"{provider} invocation error: {type(exc).__name__}: {exc}")
    finally:
        if isolation is not None:
            isolation.cleanup()
    duration = round(time.monotonic() - started, 3)

    trace = _device_tool_trace(provider, req, c.stdout)
    if c.returncode != 0:
        failed = _nonzero_exit_result(provider, req, c, cli_version=cli_version, duration=duration, trace=trace)
        failed.text = (final["text"] or "").strip()
        return failed
    usage, reported_models = _claude_usage_from_event(claude_result_event(c.stdout))
    text = (final["text"] or "").strip()
    if trace is not None and trace["denied"]:
        return AiResult(ok=False, text=text, provider=provider, model=final["model"] or req.model,
                        cli_version=cli_version, cost_usd=final["cost"], duration_s=duration,
                        usage=usage, reported_models=reported_models, error_code=DEVICE_TOOL_CALL_DENIED,
                        error=(f"{provider} denied {trace['denied']} vphone MCP call(s): "
                               + " | ".join(trace["errors"])),
                        device_tool_trace=trace)
    return AiResult(ok=True, text=text, provider=provider, model=final["model"] or req.model,
                    cli_version=cli_version, cost_usd=final["cost"], duration_s=duration,
                    usage=usage, reported_models=reported_models, device_tool_trace=trace)


def invoke_and_persist(req: AiRequest, raw_path: Path, *, runner: CommandRunner = _subprocess_runner) -> AiResult:
    """invoke + 把结果落 raw_path（run 证据 / 排障锚点），回填 result.raw_path。"""
    result = invoke(req, runner=runner)
    write_json(raw_path, result.to_contract())
    result.raw_path = str(raw_path)
    return result


#: 单次预算上限的 fail-closed 拒绝码（`AiResult.error_code`）。交付工作流在
#: `autophone.workflow.composition.AI_BUDGET_FAILURE_KINDS` 登记同一组字符串，由契约测试核对一致。
AI_BUDGET_EXCEEDED = "ai_budget_exceeded"
AI_BUDGET_COST_UNREPORTED = "ai_budget_cost_unreported"
AI_BUDGET_LIMIT_UNSUPPORTED = "ai_budget_limit_unsupported"
AI_BUDGET_ERROR_CODES = frozenset(
    {AI_BUDGET_EXCEEDED, AI_BUDGET_COST_UNREPORTED, AI_BUDGET_LIMIT_UNSUPPORTED, "ai_call_limit_exceeded"},
)

#: Claude CLI 因 `--max-budget-usd` 停止时结果信封的 `subtype`。2026-10-14 vm-alt 活机第二轮 go-home 实际触发
#: （退出 1，`terminal_reason=budget_exhausted`；evidence/outputs/bl067-device-channel-live-20261014 第七节）。
_CLAUDE_BUDGET_STOP_SUBTYPE = "error_max_budget_usd"
_BUDGET_EPSILON_USD = 1e-9


def _claude_budget_stop(text: str) -> tuple[bool, float | None]:
    """从 Claude JSON 信封（失败时 `AiResult.text` 保留的 stdout）识别预算停止，并取回报的成本。"""
    stripped = (text or "").strip()
    for line in (stripped, *stripped.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if isinstance(envelope, dict) and envelope.get("subtype") == _CLAUDE_BUDGET_STOP_SUBTYPE:
            cost = envelope.get("total_cost_usd")
            known = (
                float(cost)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(float(cost))
                else None
            )
            return True, known
    return False, None


def _overrun(cost: float | None, max_budget_usd: float) -> float | None:
    if cost is None or cost <= max_budget_usd + _BUDGET_EPSILON_USD:
        return None
    return round(cost - max_budget_usd, 8)


def enforce_call_budget(
    result: AiResult, max_budget_usd: float, *, stop_budget_usd: float | None = None,
) -> AiResult:
    """按单次授权上限对一次已完成的调用做 fail-closed 判定，原地改写并返回 `result`。

    `max_budget_usd` 是授权上限；`stop_budget_usd` 是下发给 CLI `--max-budget-usd` 的停止阈值（缺省即
    授权上限）。两者都记入结果；回报成本高于授权上限时 `budget_overrun_usd` 记超出金额。

    - provider 自报因预算停止（Claude 信封 `subtype=error_max_budget_usd`）→ `ai_budget_exceeded`
      （CLI 停止阈值已触达，任务未完成；是否超出授权上限看 `budget_overrun_usd`）；
    - 调用成功但未回报成本 → `ai_budget_cost_unreported`（无法证明没有超限）；
    - 调用成功且回报成本大于授权上限 → `ai_budget_exceeded`。Claude 在轮次之间检查上限，单轮调用
      可能在超限后仍返回成功，所以调用后必须再核对一次。

    命中时 `ok=false`、`json=None`（不产出分析结果），`text` 与成本、用量、模型标识照旧留痕
    （`invoke` 在非零退出时也从结果信封回读这些字段）。其余失败保持原样，不改判。
    """
    result.max_budget_usd = float(max_budget_usd)
    if stop_budget_usd is not None:
        result.stop_budget_usd = float(stop_budget_usd)
    threshold = float(stop_budget_usd) if stop_budget_usd is not None else float(max_budget_usd)
    if not result.ok:
        stopped, cost = _claude_budget_stop(result.text)
        if stopped:
            if result.cost_usd is None:
                result.cost_usd = cost
            result.budget_overrun_usd = _overrun(result.cost_usd, max_budget_usd)
            result.error_code = AI_BUDGET_EXCEEDED
            where = (f"max_budget_usd {max_budget_usd}" if stop_budget_usd is None
                     else f"cli budget {threshold} (authorized max_budget_usd {max_budget_usd})")
            result.error = (
                f"{AI_BUDGET_EXCEEDED}: provider stopped at {where}"
                + (f" (cost_usd {result.cost_usd})" if result.cost_usd is not None else "")
                + (f", over the authorized limit by {result.budget_overrun_usd}"
                   if result.budget_overrun_usd is not None else "")
                + (f"; {result.error}" if result.error else "")
            )
        else:
            result.budget_overrun_usd = _overrun(result.cost_usd, max_budget_usd)
        return result
    if result.cost_usd is None:
        result.ok = False
        result.json = None
        result.error_code = AI_BUDGET_COST_UNREPORTED
        result.error = (
            f"{AI_BUDGET_COST_UNREPORTED}: provider reported no cost; "
            f"cannot prove the call stayed within max_budget_usd {max_budget_usd}"
        )
    elif result.cost_usd > max_budget_usd + _BUDGET_EPSILON_USD:
        result.ok = False
        result.json = None
        result.error_code = AI_BUDGET_EXCEEDED
        result.budget_overrun_usd = _overrun(result.cost_usd, max_budget_usd)
        result.error = (
            f"{AI_BUDGET_EXCEEDED}: cost_usd {result.cost_usd} exceeds max_budget_usd {max_budget_usd} "
            f"by {result.budget_overrun_usd}"
        )
    return result


class FindingSetAnalysisAi:
    """`autophone.workflow.ui_verification.ports.AnalysisAi` 的本地 provider 实现。

    组合根（CLI `ui-verification analyze`、工作流 UI 适配器）把它装进 `analyze_finding_set`；
    UI Verification Module 自己不 import 本模块。`runner` 无论注入与否都被钉在 `cwd` 内执行。

    `max_budget_usd` 缺省为 None：不传 `--max-budget-usd`，行为与原先相同；给出时随每次调用
    写进 `AiRequest`（只有声明了 `budget_limit` 的后端会据此设上限），并写进原始回执的
    `max_budget_usd`。

    `enforce_budget=True`（交付工作流组合根使用，BL-036 G4）在此之上把上限变成硬约束：后端未声明
    `budget_limit` 时不调用、直接拒绝；调用后按 `enforce_call_budget` 核对回报成本。缺省 False，
    E6 harness 等既有调用方的判定不变（它们自行按回执成本记账）。
    """

    def __init__(
        self, *, max_budget_usd: float | None = None, enforce_budget: bool = False,
        read_isolated: bool = False,
    ) -> None:
        if max_budget_usd is not None and (
            isinstance(max_budget_usd, bool)
            or not isinstance(max_budget_usd, (int, float))
            or not math.isfinite(float(max_budget_usd))
            or max_budget_usd <= 0
        ):
            raise ValueError("max_budget_usd must be a positive finite number")
        if enforce_budget and max_budget_usd is None:
            raise ValueError("enforce_budget requires max_budget_usd")
        self.max_budget_usd = None if max_budget_usd is None else float(max_budget_usd)
        self.enforce_budget = bool(enforce_budget)
        self.read_isolated = read_isolated

    def invoke(
        self,
        *,
        prompt: str,
        system: str,
        json_schema: Mapping[str, Any],
        provider: str | None,
        model: str | None,
        raw_path: Path,
        cwd: Path,
        runner: CommandRunner | None,
    ) -> AiResult:
        request = AiRequest(
            prompt=prompt,
            system=system,
            tools="none",
            json_schema=dict(json_schema),
            provider=provider,
            model=model,
            max_budget_usd=self.max_budget_usd,
        )
        base = runner if runner is not None else _subprocess_runner
        if self.read_isolated and resolve_provider(provider) == "codex":
            from autophone.application.codex_analysis import isolated_runner as codex_isolated_runner

            base = codex_isolated_runner(base, raw_path=raw_path)

        def isolated_runner(argv, **kwargs):
            kwargs.setdefault("cwd", cwd)
            return base(argv, **kwargs)

        if self.max_budget_usd is None:
            return invoke_and_persist(request, raw_path, runner=isolated_runner)
        if self.enforce_budget:
            resolved, capabilities = provider_capabilities(provider)
            if not capabilities.budget_limit:
                result = AiResult(
                    ok=False, text="", provider=resolved, model=model,
                    error=(
                        f"{AI_BUDGET_LIMIT_UNSUPPORTED}: provider {resolved} cannot enforce "
                        f"max_budget_usd {self.max_budget_usd}; the call was not made"
                    ),
                    max_budget_usd=self.max_budget_usd,
                    error_code=AI_BUDGET_LIMIT_UNSUPPORTED,
                )
            else:
                result = enforce_call_budget(
                    invoke(request, runner=isolated_runner), self.max_budget_usd,
                )
        else:
            result = invoke(request, runner=isolated_runner)
            result.max_budget_usd = self.max_budget_usd
        write_json(raw_path, result.to_contract())
        result.raw_path = str(raw_path)
        return result


# ─────────────────────────── doctor ───────────────────────────


def ai_doctor(*, probe: bool = False, provider: str | None = None,
              timeout_s: float = 30.0, runner: CommandRunner = _subprocess_runner) -> dict[str, Any]:
    """CLI capability detection：在不在 / 版本 / 默认 provider。probe=True 才打一次极小真调用。"""
    providers: dict[str, Any] = {}
    for name in PROVIDERS:
        backend = _BACKENDS[name]
        path = _resolve_backend_executable(backend)
        providers[name] = {
            "available": bool(path),
            "path": path,
            "version": _detect_version(backend, runner, executable_path=path) if path else None,
            "capabilities": _PROVIDER_CAPABILITIES[name].to_dict(),
        }
    try:
        resolved = resolve_provider(provider)
    except AiError:
        resolved = None
    available_any = any(p["available"] for p in providers.values())
    result: dict[str, Any] = {
        "schema_version": "v1",
        "providers": providers,
        "resolved_default": resolved,
        "env_provider": os.environ.get("AUTOPHONE_AI_PROVIDER"),
        "probe": probe,
        "status": "healthy" if available_any else "not_configured",
    }
    if not probe or resolved is None:
        return result

    probe_res = invoke(AiRequest(prompt="reply with the single word: pong", tools="none",
                                 provider=resolved, timeout_s=timeout_s), runner=runner)
    result["probe_ok"] = probe_res.ok
    result["probe_provider"] = probe_res.provider
    result["probe_model"] = probe_res.model
    if not probe_res.ok:
        result["status"] = "probe_failed"
        result["detail"] = probe_res.error
    return result
