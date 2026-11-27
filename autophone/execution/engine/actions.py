"""actions.py — CaseContext: the semantic action vocabulary (docs §10.3).

Built on two already-landed deps:
  - autophone.device.uds : request / preview_to_native / save_image / GeometryGuard
  - autophone.device.ocr_target : run_ocr / find_text -> Match (carries native_x/native_y + nx/ny)

The verbs are methods on CaseContext so a step-script (§10.2) reads as
`ctx.tap_text("设置")`. They share one ocr_now() frame cache and the run's
screenshot dir + retry config.

Failure model (reconciles §10.3/§10.4): an action records an ActionResult into
ctx.results either way; a *hard* failure (target not found after retries, an
assertion that did not hold) also raises StepFailure so the terse step-script
stops without per-call `if` checks. The runner catches StepFailure → marks the
case `fail`; it catches GeometryDrift/OcrError/ProtocolError → marks `error`.
handle_dialog_if_present never fails on absence (conditional branch, §4.1).
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import socket
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from autophone.logging import bind_step
from autophone.device.uds import (
    HARDWARE_KEYS,
    GeometryGuard,
    ProtocolError,
    preview_to_native,
    request,
)
from autophone.device.ax_target import find_node
from autophone.device.ocr_target import Match, OcrError, find_text, run_ocr
from autophone.device.shell import ShellError, run_shell
from autophone.device.springboard import (
    SpringBoardIdentity,
    SpringBoardProbeError,
    probe_springboard,
)
from autophone.execution.contracts import CaseBlocked, blocker

logger = logging.getLogger(__name__)

# Post-tap settle: poll screenshots until two consecutive frames are byte-
# identical (the transition finished) instead of a blind sleep (tap+settle was
# ≈520ms in architecture-v1.md §10.1). Polls ride the host-side fast path — NOT
# OCR, whose low-confidence boxes jitter frame-to-frame and would never agree on
# a static screen. SETTLE_CAP_MS bounds a perpetually-animating screen (spinner)
# or a host that stamps per-frame JPEG metadata; in that degraded case we hit
# the bounded cap rather than hang.
# 优化（2026-07-06）：900ms→700ms，实测平均232ms，700ms留3x余量且节省200ms最坏情况
SETTLE_POLL_MS = 200
SETTLE_CAP_MS = 700
# `type` is a clipboard write with a long tail (p99=1.3s); give it ≥3s (CLAUDE.md).
TYPE_TIMEOUT_S = 3.0
# 读到 SpringBoard 不在进程表时，间隔这么久复探一次再下结论（launchd 重启 SpringBoard 的间隙）。
SPRINGBOARD_ABSENT_RECHECK_S = 1.0
# launch_app 拿不到 pid 时，就绪标记只出现在与启动前字节相同的帧上：显示停止刷新
# （熄屏/锁屏时 UDS 截图停在最后一帧，2026-10-05 vm），该帧不能证明 App 已启动。
# 失败说明带此标记，Rig 就绪门据此写出「截图未刷新」的恢复说明。
STALE_SCREEN_MARK = "screen not refreshing"

# 低对比度页（如协议规则：浅灰艺术字 + 满屏手机号水印）Vision OCR 直接返回 0 box。
# 增强 OCR 先把灰阶 level 拉伸（压暗淡灰文字、洗掉中灰水印）再识别，命中率显著回升。
# magick 是外部依赖：缺它时 fail-closed（raise OcrError → 用例判 error，绝不伪 pass）。
ENHANCE_LEVEL = "38%,55%"  # 真机校准 2026-06-09：协议规则页「协议内容」稳定回到 conf 1.00

Region = tuple[float, float, float, float]  # normalized (nx0, ny0, nx1, ny1)
SUBSTRING_AMBIGUITY_MIN_CHARS = 6


def resolve_sock() -> str:
    """Pick the vphone.sock path portably.

    The default is ``vm``; explicit VPHONE_SOCK/VM_DIR selection (including
    ``VM_DIR=vm-alt``) is centralized in vphone_target.
    """
    from autophone.device.target import resolve_target

    return str(resolve_target().sock)


class StepFailure(RuntimeError):
    """Raised by an action on a hard failure so the step-script stops. Carries
    the ActionResult that was already recorded into ctx.results; its `kind`
    classifies the failure (locator_missing / ambiguous_match / assertion_failed /
    …) for diagnosis — see docs §10.10."""

    def __init__(self, result: "ActionResult") -> None:
        super().__init__(result.detail)
        self.result = result


class SubmitOutcomeUnknown(StepFailure):
    """An irreversible/outbound action may have reached the app, but the host did
    not receive a trustworthy result. It must not be auto-retried."""


def ambiguous_label(
    matches: list,
    query: str,
    attr: str,
    *,
    identifier: str | None = None,
    require_unique: bool = False,
) -> bool:
    """True when a locator result is unsafe to auto-pick.

    When ``require_unique=True``, any multi-match (``len(matches) >= 2``) is
    strictly ambiguous. For identifier-only lookups (``identifier`` provided and
    ``not query``), multiple matching nodes are likewise ambiguous rather than
    silently clicking ``matches[0]``.

    Exact duplicate labels are always ambiguous. For long, business-entity style
    queries, multiple containing-substring matches with no exact label are also
    ambiguous: picking the highest-confidence row would silently choose between
    repeated entities. Short partial queries (`登录` ⊂ `账号密码登录`) and truncated
    navigation hints (R17's `我的业` ⊂ `我的业绩`) keep the old behavior."""
    if require_unique:
        return len(matches) >= 2
    if not query:
        return bool(identifier) and len(matches) >= 2
    values = [getattr(m, attr, None) for m in matches]
    exact = sum(1 for value in values if value == query)
    if exact >= 2:
        return True
    if exact == 1:
        return False
    if len(query) < SUBSTRING_AMBIGUITY_MIN_CHARS:
        return False
    return sum(1 for value in values if isinstance(value, str) and query in value) >= 2


@dataclass
class ActionResult:
    action: str
    ok: bool
    detail: str = ""
    shot_path: str | None = None
    elapsed_ms: float = 0.0
    recorded_at: str = ""
    # 辅助帧：原始截图之外的派生图（如增强 OCR 的灰阶帧）。report/HTML 会把这些也
    # 列进截图条，「为什么判 pass」的实际 OCR 输入对 reviewer 可见，而非埋在目录里。
    aux_shots: list[str] = field(default_factory=list)
    # 失败归类（§10.10）：失败动作带 taxonomy kind，让「fail/error」之下可定位失败性质。
    # 成功动作恒空。ActionResult 加字段即自动流进 result.json / HTML / AI 诊断净化包。
    kind: str = ""
    # 动作内部子段（trace P2，docs/run-execution-trace.md §三B）：把 elapsed_ms 这个合计值
    # 拆成 screenshot/ocr/enhance/dispatch/settle 等子段，看穿黑盒。每项 {name, ms, count}
    # （count=该子段触发次数，如 settle 的轮询数、assert 轮询里的多次 ocr）。缺省空 →
    # 老报告/前端/trace 行为不变，fail-open。由 _record 折叠 CaseContext._spans 写入。
    spans: list[dict[str, Any]] = field(default_factory=list)
    # Run 内唯一 opaque ID。交互动作在副作用发生前分配，并与 API mark 共用。
    # 放在所有旧字段之后，保留现有位置参数调用的语义。
    action_id: str = ""
    subject_epoch_ref: str = ""
    # Explicit observation from an Operation executor; never inferred from detail.
    witness_stage: str | None = None
    # 非图像证据单独引用，避免报告将 AX JSON 作为截图内嵌。
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class RuntimeResourceScope:
    """Own lazy, non-persisted resources for one runner execution group.

    ``runner.run()`` is already the scheduling boundary used for one execution
    group. Cases borrow resources from this scope; direct ``run_case()`` callers
    keep the narrower case-owned fallback. The scope deliberately exposes only
    lazy lookup and close, so operation args/results cannot accidentally persist
    live Frida/ADB handles.
    """

    _resources: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str, factory: Callable[[], Any]) -> Any:
        if not key:
            raise ValueError("runtime resource key is required")
        if key not in self._resources:
            self._resources[key] = factory()
        return self._resources[key]

    def close(self) -> list[str]:
        resources = list(self._resources.items())
        self._resources.clear()
        errors: list[str] = []
        for key, resource in reversed(resources):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - report every cleanup failure
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
        return errors


def enhanced_ocr_backend_available() -> bool:
    if shutil.which("magick") is not None:
        return True
    try:
        import PIL  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def _level_bounds(level: str) -> tuple[int, int]:
    left, right = (part.strip().rstrip("%") for part in level.split(",", 1))
    lo = round(float(left) * 255 / 100)
    hi = round(float(right) * 255 / 100)
    if not 0 <= lo < hi <= 255:
        raise ValueError(f"invalid ImageMagick level range: {level!r}")
    return lo, hi


@dataclass
class CaseContext:
    """Per-case execution context. One per case; not reused across cases so the
    GeometryGuard lock and frame cache stay scoped to one run."""
    case_id: str
    shot_dir: Path
    sock_path: str = field(default_factory=resolve_sock)
    timeout: float = 15.0
    min_confidence: float = 0.5
    results: list[ActionResult] = field(default_factory=list)
    guard: GeometryGuard = field(default_factory=GeometryGuard)
    # Phase 4 关联层（§六）：抓取句柄，须有 .mark(case_id, step, operation?)。None = 不抓，api_mark 是 no-op，
    # phase2 行为完全不变。由 autophone/observation/frida 的 orchestrator 注入；phase2 自身从不构造它。
    capture: Any = None
    # AX 寻址提供方（§七）：须有 .refresh() -> AxDump（autophone.device.ax_session.AxSession）。
    # None = 未接 AX，tap_ax 退坐标兜底或 fail（见方法）。由 run orchestrator 注入；
    # phase2 自身从不构造它，也不依赖 frida。
    ax: Any = None
    # App operations may attach fixture identity here (for example orderNum)
    # so later UI operations can assert they are acting on the intended entity.
    fixture_order: dict[str, Any] = field(default_factory=dict)
    # Explicit API assertion inputs captured by operations before business actions.
    # Separate from the mutable fixture so cleanup cannot change the expected identity.
    api_assertion_context: dict[str, Any] = field(default_factory=dict)
    # Post-action entity readback observations written by read-only readback operations.
    entity_readback_context: dict[str, Any] = field(default_factory=dict)
    # Run-scoped order instance pool.  None keeps per-case create_order behavior.
    order_pool: Any = None
    order_pool_lease: dict[str, Any] = field(default_factory=dict)
    # A3 runtime policy. None keeps legacy/direct scripts unchanged; platform
    # app-aware runs set this from profile.allowed_effects before dispatching.
    allowed_effects: set[str] | None = None
    # Current dispatcher step effect. Irreversible operations set this while
    # their internal ctx.tap_* calls run, so dispatch uncertainty is reported as
    # outcome_unknown instead of infra retry bait.
    _current_effect: Any = field(default=None, repr=False)
    # Optional screen asset document for A5 blocking-overlay recovery and
    # controlled tap_ax single-frame screen_id pre-checks (D4).
    screen_asset_doc: dict[str, Any] | None = None
    # Suppress recipe-level blocking-overlay auto-dismiss across the entire case
    # when executing symbol-bound recipes (Round 1.5 Item 4).
    suppress_overlay_dismiss: bool = False
    # Frozen AX observation backend for this run (D8).
    ax_backend: str | None = None
    # M4 multi-effect & isolated draft trial admission context.
    effect_mode: str | None = None
    trial_draft_recipes: bool = False
    confirm_trial_effects: bool = False
    trial_data_isolation: str = "unisolated"

    _ocr: dict[str, Any] | None = field(default=None, repr=False)
    _dirty: bool = field(default=True, repr=False)
    _last_shot: str | None = field(default=None, repr=False)
    _seq: int = field(default=0, repr=False)
    _current_operation: str | None = field(default=None, repr=False)
    _current_symbol_id: str | None = field(default=None, repr=False)
    _current_window_tag: str | None = field(default=None, repr=False)
    # Post-body housekeeping lock ("body_end" | "entity_readback" | "post_case_cleanup").
    # When active, every api_mark call pins window_tag to this value and strips symbol_id
    # so nested operations/steps inside readback or teardown can never leak into body windows.
    _housekeeping_window_tag: str | None = field(default=None, repr=False)
    # A semantic operation can nominate the very next interaction's capture
    # mark.  This preserves the strict pre-dispatch ordering while avoiding API
    # assertions that depend on incidental verbs such as ``tap_xy``.
    _next_api_mark: tuple[Any, ...] | None = field(default=None, repr=False)
    _current_action_id: str | None = field(default=None, repr=False)
    _current_subject_epoch_ref: str | None = field(default=None, repr=False)
    # 当前动作累计的子段（trace P2）。动作同步顺序执行，故「上次 _record 至今」累计的子段
    # 都归属当前动作；_record 折叠后写进 ActionResult.spans 并清空。
    _spans: list[tuple[str, float, int]] = field(default_factory=list, repr=False)
    _last_ax_dump: Any = field(default=None, repr=False)
    _symbol_observations: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Direct-case runtime resources (Frida observation sessions, etc.).
    # ``run()`` normally supplies the group scope below; standalone run_case()
    # falls back here and closes once after setup/body/teardown.
    _runtime_resources: dict[str, Any] = field(default_factory=dict, repr=False)
    # ``run()`` supplies one shared owner for all cases in an execution group.
    # Direct ``run_case()`` callers leave this unset and retain per-case cleanup.
    runtime_scope: RuntimeResourceScope | None = field(default=None, repr=False)

    # ── primitives ──────────────────────────────────────────────────────────
    def screenshot(self) -> Path:
        """Take a screenshot, save it under shot_dir, return its path. Runs the
        GeometryGuard so a mid-run resolution drift fails closed (GeometryDrift)."""
        obj, sample = request({"t": "screenshot"}, sock_path=self.sock_path,
                              timeout=self.timeout)
        self.guard.check(sample)
        if not obj.get("image"):
            raise RuntimeError(f"screenshot returned no image: {obj.get('error')}")
        self._seq += 1
        path = self.shot_dir / f"{self.case_id}_{self._seq:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(obj["image"]))
        self._last_shot = str(path)
        return path

    def ocr_now(self, *, force: bool = False) -> dict[str, Any]:
        """Screenshot + OCR with a one-frame cache. Reused by multiple asserts on
        the same screen; invalidated (dirty) by any tap so a post-tap assert
        re-shoots the new state."""
        if self._ocr is not None and not self._dirty and not force:
            return self._ocr
        with self._span("screenshot"):
            img = self.screenshot()
        with self._span("ocr"):
            self._ocr = run_ocr(img)
        self._dirty = False
        return self._ocr

    def invalidate_frame(self) -> None:
        """Mark the one-frame OCR cache stale so the next ocr_now re-shoots.
        Used when the screen is driven outside this ctx's own verbs — e.g. an
        operation executor (login/logout) that screenshots via its own UDS calls
        and never touches `_dirty`; without this, a following assert reuses the
        pre-operation frame (0ms cache hit) and judges the wrong screen."""
        self._dirty = True

    def runtime_resource(self, key: str, factory: Callable[[], Any]) -> Any:
        """Return one group- or case-owned runtime resource, constructing lazily."""
        if self.runtime_scope is not None:
            return self.runtime_scope.get(key, factory)
        if not key:
            raise ValueError("runtime resource key is required")
        if key not in self._runtime_resources:
            self._runtime_resources[key] = factory()
        return self._runtime_resources[key]

    def close_runtime_resources(self) -> list[str]:
        """Close only direct-case resources; a borrowed group scope outlives us."""
        local = RuntimeResourceScope(self._runtime_resources)
        self._runtime_resources = {}
        return local.close()

    # ── span instrumentation (trace P2) ──────────────────────────────────────
    @contextlib.contextmanager
    def _span(self, name: str):
        """Time a sub-operation and fold it into the current action's spans. Used
        to crack open elapsed_ms (screenshot vs ocr vs dispatch vs settle). The
        block's exceptions still propagate — the span is recorded in `finally` so
        a failing action still attributes the time it spent before blowing up."""
        s0 = time.perf_counter()
        try:
            yield
        finally:
            self._spans.append((name, (time.perf_counter() - s0) * 1000.0, 1))

    def _add_span(self, name: str, ms: float, count: int = 1) -> None:
        """Record a pre-measured sub-segment (e.g. _settle, which counts its own
        poll iterations as `count`)."""
        self._spans.append((name, ms, count))

    def _fold_spans(self) -> list[dict[str, Any]]:
        """Collapse the buffered (name, ms, count) tuples into per-name totals in
        first-seen order, then clear the buffer. Same-named sub-ops (e.g. an assert
        that polled OCR 3×) sum into one row so result.json stays compact."""
        order: list[str] = []
        agg: dict[str, list[float]] = {}
        for name, ms, count in self._spans:
            if name not in agg:
                agg[name] = [0.0, 0]
                order.append(name)
            agg[name][0] += ms
            agg[name][1] += count
        self._spans = []
        return [{"name": n, "ms": round(agg[n][0], 1), "count": int(agg[n][1])} for n in order]

    # ── recording helper ────────────────────────────────────────────────────
    def _grab_raw(self) -> bytes:
        """Geometry-guarded screenshot returning raw JPEG bytes, NOT saved to the
        shot trail. Used only by _settle, which polls many frames and persists
        none of them — the following assert re-shoots and saves the real one."""
        obj, sample = request({"t": "screenshot"}, sock_path=self.sock_path,
                              timeout=self.timeout)
        self.guard.check(sample)
        img = obj.get("image")
        if not img:
            raise RuntimeError(f"screenshot returned no image: {obj.get('error')}")
        return base64.b64decode(img)

    def _settle(self) -> None:
        """Wait for a post-tap transition to finish: poll until two consecutive
        screenshots are byte-identical, or SETTLE_CAP_MS elapses. Replaces the
        fixed sleep so a slow transition isn't asserted mid-flight and a fast one
        isn't over-waited. Callers already set _dirty, so the next ocr re-shoots
        the settled state; _settle deliberately persists no frame of its own."""
        t0 = time.perf_counter()
        polls = 0
        prev = self._grab_raw()
        while (time.perf_counter() - t0) * 1000 < SETTLE_CAP_MS:
            time.sleep(SETTLE_POLL_MS / 1000.0)
            cur = self._grab_raw()
            polls += 1
            if cur == prev:
                break
            prev = cur
        # 把轮询总时长 + 帧数记成 settle 子段（trace P2）：settle 常是 tap 动作的耗时大头，
        # 但此前不落任何记录、被吞进调用方 elapsed_ms。count=轮询帧数，暴露是否熬满 cap。
        self._add_span("settle", (time.perf_counter() - t0) * 1000.0, polls)

    def _allocate_action_id(self) -> str:
        action_id = f"act-{uuid.uuid4().hex}"
        self._current_action_id = action_id
        return action_id

    def api_mark(
        self,
        step: str,
        *,
        operation: str | None = None,
        action_id: str | None = None,
        symbol_id: str | None = None,
        window_tag: str | None = None,
    ) -> None:
        """Phase 4 关联层（§六）：往抓取流插一条 mark，让随后抓到的 API 调用按时间窗归到
        (case_id, step[, operation])。capture 为 None 时是 no-op，所以现有 step-script 无需感知抓取。

        **必须在触发网络的动作之前打点**：net_capture.merge 把每条 req 归到「最后一个 mark.ts <= t_req」
        的 mark，若在动作收尾后才打点（mark.ts > t_req），该动作触发的 API 会被错归到上一个动作或漏归。
        故各交互动词（tap_*/type_text）在 `request({"t":"tap"/"type"})` 之前调用本方法，而非在 _record 里。"""
        cap = self.capture
        if cap is None:
            return
        op = operation if operation is not None else self._current_operation
        housekeeping_tag = self._housekeeping_window_tag
        if housekeeping_tag:
            sym = None
            wtag: str | None = housekeeping_tag
        else:
            sym = symbol_id if symbol_id is not None else self._current_symbol_id
            wtag = window_tag if window_tag is not None else self._current_window_tag
        if action_id:
            correlation_id = action_id
        elif housekeeping_tag or step in {
            "body_end",
            "entity_readback",
            "post_case_cleanup",
            "operation",
            "operation_end",
        }:
            # Always allocate a distinct action_id for boundary/housekeeping/operation marks
            # so host-side CaptureHandle._action_meta never overwrites an earlier interaction.
            correlation_id = f"act-{uuid.uuid4().hex}"
        else:
            correlation_id = self._current_action_id or self._allocate_action_id()
        epoch_ref = self._current_subject_epoch_ref or str(
            getattr(cap, "subject_epoch_ref", "") or ""
        )
        self._current_subject_epoch_ref = epoch_ref or None
        try:
            try:
                cap.mark(
                    self.case_id,
                    step,
                    op,
                    action_id=correlation_id,
                    subject_epoch_ref=epoch_ref or None,
                    symbol_id=sym,
                    window_tag=wtag,
                )
            except TypeError:
                try:
                    cap.mark(
                        self.case_id,
                        step,
                        op,
                        action_id=correlation_id,
                        subject_epoch_ref=epoch_ref or None,
                    )
                except TypeError:
                    # Third-party/test handles pinned to the pre-M0 signature remain
                    # usable, but do not satisfy the new correlation contract.
                    cap.mark(self.case_id, step, op)
        except Exception:  # noqa: BLE001 — 抓取链路问题绝不能拖垮被测用例
            pass

    def mark_next_api(
        self,
        step: str,
        *,
        operation: str | None = None,
        symbol_id: str | None = None,
        window_tag: str | None = None,
    ) -> None:
        """Attribute the next UDS interaction to a semantic capture step.

        The override is deliberately one-shot: scrolling and later dialog taps
        retain their normal low-level marks, while the exact tap that submits a
        fulfillment action has a stable business identity.
        """
        self._next_api_mark = (step, operation, symbol_id, window_tag)

    def _mark_interaction(self, default_step: str) -> None:
        action_id = self._allocate_action_id()
        self._current_subject_epoch_ref = str(
            getattr(self.capture, "subject_epoch_ref", "") or ""
        ) or None
        pending = self._next_api_mark
        self._next_api_mark = None
        if pending is None:
            self.api_mark(default_step, action_id=action_id)
            return
        if len(pending) >= 4:
            step, operation, symbol_id, window_tag = pending[:4]
        else:
            step, operation = pending[:2]
            symbol_id, window_tag = None, None
        self.api_mark(
            step,
            operation=operation,
            action_id=action_id,
            symbol_id=symbol_id,
            window_tag=window_tag,
        )

    def manual_pause(self, prompt: str, *, timeout_s: float = 900.0, poll_ms: int = 500) -> ActionResult:
        """Pause a case for a human action, holding the screen lock until resume.

        The Web/API side writes ``manual/resume.json`` in the run directory.
        Timeout is a test failure, not an infrastructure hang: the runner catches
        StepFailure, releases the lock, and the queue can continue.
        """
        from autophone.execution.manual import (
            ManualCheckpointAborted,
            ManualCheckpointTimeout,
            request_manual_checkpoint,
            wait_for_manual_resume,
        )

        t0 = time.perf_counter()
        run_dir = self.shot_dir.parent
        run_id = run_dir.name
        request_manual_checkpoint(
            run_dir,
            run_id=run_id,
            case_id=self.case_id,
            prompt=prompt,
            timeout_s=timeout_s,
            shot_path=self._last_shot,
        )
        self._record("manual_pause", True, f"waiting for human: {prompt}", t0)
        try:
            resume = wait_for_manual_resume(run_dir, timeout_s=timeout_s, poll_ms=poll_ms)
        except ManualCheckpointTimeout as exc:
            result = self._record("manual_timeout", False, str(exc), t0, kind="manual_timeout")
            raise StepFailure(result) from exc
        except ManualCheckpointAborted as exc:
            result = self._record("manual_abort", False, str(exc), t0, kind="manual_abort")
            raise StepFailure(result) from exc

        actor = resume.get("actor") or "unknown"
        note = resume.get("note") or ""
        return self._record("manual_resume", True, f"resumed by {actor}{': ' + note if note else ''}", t0)

    def _resolved_ax_backend(self, dump: Any = None) -> str | None:
        raw = getattr(dump, "ax_backend", None)
        if raw is None and isinstance(dump, dict):
            raw = dump.get("ax_backend")
        if raw is None:
            raw = getattr(self.ax, "ax_backend", None)
        if raw is None:
            raw = self.ax_backend
        return str(raw) if raw is not None else None

    def _write_ax_dump_file(self, dump: Any) -> Path:
        self._seq += 1
        path = self.shot_dir / f"{self.case_id}_{self._seq:02d}.ax.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            asdict(dump)
            if is_dataclass(dump)
            else (dict(dump) if isinstance(dump, dict) else dump)
        )
        resolved_backend = self._resolved_ax_backend(dump)
        if isinstance(payload, dict) and resolved_backend is not None:
            payload["ax_backend"] = resolved_backend
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _record_symbol_observation(
        self,
        binding: dict[str, Any],
        action_result: ActionResult | None,
        *,
        screen_id: str | None = None,
    ) -> None:
        dump = self._last_ax_dump
        self._last_ax_dump = None
        ax_evidence_ref: str | None = None
        if dump is not None:
            try:
                path = self._write_ax_dump_file(dump)
                try:
                    ax_evidence_ref = path.relative_to(self.shot_dir.parent).as_posix()
                except ValueError:
                    ax_evidence_ref = str(path)
                if isinstance(action_result, ActionResult) and ax_evidence_ref not in action_result.evidence_refs:
                    action_result.evidence_refs.append(ax_evidence_ref)
            except OSError:
                logger.debug("failed to persist symbol AX dump evidence", exc_info=True)
        self._symbol_observations.append({
            "symbol_id": str(binding.get("symbol_id") or ""),
            "binding_sha256": str(binding.get("binding_sha256") or ""),
            "artifact_content_digest": str(binding.get("artifact_content_digest") or ""),
            "screen_id": str(binding.get("screen_id") or screen_id or ""),
            "ax_backend": self._resolved_ax_backend(dump),
            "ax_evidence_ref": ax_evidence_ref,
            "shot_path": getattr(action_result, "shot_path", None),
        })

    def ax_snapshot(self, *, label: str = "") -> tuple[Any | None, Path | None]:
        """Read a fresh AX dump and save it as JSON evidence.

        This is intentionally softer than tap_ax: AX absence or transport failure
        is an observation-quality issue for the verifier to score, not an immediate
        step failure. The returned JSON path is for VerifierResult.evidence_refs;
        it never goes into shot_path/aux_shots, which the report treats as images.
        """
        from autophone.device.ax_session import UnsupportedAxBackendError

        t0 = time.perf_counter()
        what = label or "ax_snapshot"
        if self.ax is None:
            self._record("ax_snapshot", True, f"{what}: ax unavailable: no AX provider", t0)
            return None, None

        try:
            dump = self.ax.refresh()
        except UnsupportedAxBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 — verifier decides how to degrade
            self._record("ax_snapshot", True, f"{what}: ax unavailable: {exc}", t0)
            return None, None

        path = self._write_ax_dump_file(dump)
        self._record("ax_snapshot", True,
                     f"{what}: saved {len(getattr(dump, 'nodes', []) or [])} AX nodes to {path.name}",
                     t0)
        return dump, path

    def _record(self, action: str, ok: bool, detail: str, t0: float,
                aux_shots: list[str] | None = None, kind: str = "",
                *, witness_stage: str | None = None,
                evidence_refs: list[str] | None = None) -> ActionResult:
        if not ok:
            # 语义 mark 是为紧随其后的那次交互预留的；该交互所在动作失败即作废，
            # 不能泄漏给同用例内下一个不相关的交互造成 API 误归因。
            self._next_api_mark = None
        action_id = self._current_action_id or self._allocate_action_id()
        r = ActionResult(
                         action=action, ok=ok, action_id=action_id,
                         subject_epoch_ref=self._current_subject_epoch_ref or "",
                         detail=detail,
                         shot_path=self._last_shot,
                         elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                         recorded_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                         aux_shots=list(aux_shots or []), kind=kind,
                         spans=self._fold_spans(), witness_stage=witness_stage,
                         evidence_refs=list(evidence_refs or []))
        self.results.append(r)
        self._current_action_id = None
        self._current_subject_epoch_ref = None
        # 每个动作都过这里：成功走 DEBUG（-v 才显示），失败走 WARNING（默认可见）。
        # detail/elapsed_ms 已采集，零额外成本——这是把「事后看报告」变「运行时可观测」的关键。
        (logger.debug if ok else logger.warning)(
            "%s %s (%.0fms)%s", action, "ok" if ok else "FAIL",
            r.elapsed_ms, f" {detail}" if detail else "")
        return r

    def _is_irreversible_context(self) -> bool:
        decision = self._current_effect
        return bool(decision is not None and getattr(decision, "irreversible", False))

    def _request_dispatch(self, msg: dict[str, Any], *, action: str, detail: str,
                          t0: float, timeout: float | None = None) -> tuple[dict[str, Any], Any]:
        """UDS request wrapper for trigger-like actions.

        Reversible actions keep existing infra-error behavior. For an irreversible
        context, transport uncertainty after dispatch is a business outcome risk:
        fail the step as outcome_unknown and do not invite an automatic retry.
        """
        try:
            return request(
                msg,
                sock_path=self.sock_path,
                timeout=self.timeout if timeout is None else timeout,
            )
        except (TimeoutError, socket.timeout, ProtocolError, OSError, ConnectionError) as exc:
            if not self._is_irreversible_context():
                raise
            result = self._record(
                action,
                False,
                f"{detail}: outcome unknown after dispatch ({type(exc).__name__}: {exc})",
                t0,
                kind="outcome_unknown",
            )
            raise SubmitOutcomeUnknown(result) from exc

    def _mc(self, override: float | None) -> float:
        return self.min_confidence if override is None else override

    def text_present(self, query: str, *, region: Region | None = None,
                     min_confidence: float | None = None) -> bool:
        """Non-recording predicate: is `query` on the current (cached) frame?
        Used by app-specific navigation helpers (e.g. 'am I on the menu?')."""
        ocr = self.ocr_now()
        return bool(find_text(ocr, query, min_confidence=self._mc(min_confidence),
                              region=region))

    def tap_xy(self, px: int, py: int, *, label: str = "") -> ActionResult:
        """Tap a preview (430×932) coordinate. The escape hatch for textless
        icon targets (profile entry, back chevron) until AX pull lands (§六/§七).
        Prefer tap_text everywhere a label exists."""
        t0 = time.perf_counter()
        nx, ny = preview_to_native(px, py)
        self._mark_interaction("tap_xy")  # 触发前打点：mark.ts < t_req，tap 触发的 API 才归到本动作
        with self._span("dispatch"):
            resp, _ = self._request_dispatch(
                {"t": "tap", "x": nx, "y": ny},
                action="tap_xy",
                detail=f"tap_xy({px},{py}){' ' + label if label else ''}",
                t0=t0,
            )
        self._dirty = True
        ok = bool(resp.get("ok"))
        detail = f"tap_xy({px},{py}){' ' + label if label else ''}"
        if not ok:
            r = self._record("tap_xy", False, f"{detail} failed: {resp.get('error')}", t0,
                             kind="tap_failed")
            raise StepFailure(r)
        self._settle()
        return self._record("tap_xy", True, detail, t0)

    def swipe_xy(self, px1: int, py1: int, px2: int, py2: int, *,
                 ms: int = 300, label: str = "") -> ActionResult:
        """Swipe from preview (430×932) coord (px1,py1) to (px2,py2) over `ms`.
        The geometric escape hatch for scroll/drag gestures that have no semantic
        anchor (carousel paging, pull-to-refresh, list scroll) — the swipe analogue
        of tap_xy. Both endpoints are preview coords, ×3 to native for the UDS swipe;
        native is derived per-call and never re-fed (same contract as tap_xy)."""
        t0 = time.perf_counter()
        # Runtime floor for direct callers that bypass the dispatcher's static gate
        # (validate_recipe enforces ms>0 too): a 0/negative duration is not a
        # replayable swipe — fail closed before touching the device.
        if not (isinstance(ms, int) and not isinstance(ms, bool) and ms > 0):
            r = self._record("swipe_xy", False, f"swipe_xy: ms must be a positive int, got {ms!r}",
                             t0, kind="swipe_failed")
            raise StepFailure(r)
        nx1, ny1 = preview_to_native(px1, py1)
        nx2, ny2 = preview_to_native(px2, py2)
        self._mark_interaction("swipe_xy")  # 触发前打点：滑动触发的 API 归到本动作
        with self._span("dispatch"):
            resp, _ = self._request_dispatch(
                {"t": "swipe", "x1": nx1, "y1": ny1, "x2": nx2, "y2": ny2, "ms": ms},
                action="swipe_xy",
                detail=f"swipe_xy({px1},{py1})->({px2},{py2}) {ms}ms",
                t0=t0,
            )
        self._dirty = True
        ok = bool(resp.get("ok"))
        detail = f"swipe_xy({px1},{py1})->({px2},{py2}) {ms}ms{' ' + label if label else ''}"
        if not ok:
            r = self._record("swipe_xy", False, f"{detail} failed: {resp.get('error')}", t0,
                             kind="swipe_failed")
            raise StepFailure(r)
        self._settle()
        return self._record("swipe_xy", True, detail, t0)

    def press_key(self, name: str) -> ActionResult:
        """按一次硬件键（home/power/volup/voldown）。

        坐标无关的第四种基础动作：Home 回桌面、Power 熄屏/亮屏、音量键。名字过
        `HARDWARE_KEYS` 封闭集事前校验——写错的键名在 guest 只会换回一句 unknown
        key，看起来像「按了没反应」，不如在碰设备之前就失败。
        """
        t0 = time.perf_counter()
        if name not in HARDWARE_KEYS:
            r = self._record(
                "press_key", False,
                f"press_key: unknown key {name!r} (expected one of {'/'.join(HARDWARE_KEYS)})",
                t0, kind="key_failed")
            raise StepFailure(r)
        self._mark_interaction("press_key")  # 触发前打点：按键触发的 API 归到本动作
        with self._span("dispatch"):
            # screen:False —— 设备侧截图这里没用，动作后由 _settle 自己重新取帧。
            resp, _ = self._request_dispatch(
                {"t": "key", "name": name, "screen": False},
                action="press_key",
                detail=f"press_key({name})",
                t0=t0,
            )
        self._dirty = True
        detail = f"press_key({name})"
        if not bool(resp.get("ok")):
            r = self._record("press_key", False, f"{detail} failed: {resp.get('error')}", t0,
                             kind="key_failed")
            raise StepFailure(r)
        # Home 会拉起 SpringBoard 动画；_settle 轮询到两帧字节相同才继续，比固定
        # sleep 更准（同 tap_xy 的收口）。
        self._settle()
        return self._record("press_key", True, detail, t0)

    # ── semantic verbs ──────────────────────────────────────────────────────
    def tap_text(self, query: str, *, region: Region | None = None,
                 retries: int = 3,
                 retry_delay_s: float = 0.0,
                 min_confidence: float | None = None) -> ActionResult:
        """OCR-locate `query`, tap its native center. `region` (normalized) keeps
        a duplicate label (two「确定」) unambiguous. `min_confidence` overrides the
        ctx default for a known-but-low-confidence label. Re-shoots between retries;
        `retry_delay_s` lets slow async lists populate between re-shoots."""
        t0 = time.perf_counter()
        last = ""
        with bind_step("tap_text"):
            for attempt in range(retries):
                ocr = self.ocr_now(force=attempt > 0)
                matches = find_text(ocr, query, min_confidence=self._mc(min_confidence),
                                    region=region)
                if matches:
                    # fail-closed on genuine ambiguity (§10.10): duplicate exact
                    # labels, or long substring-only duplicates that should be
                    # narrowed by region rather than picked by confidence.
                    if ambiguous_label(matches, query, "text"):
                        r = self._record("tap_text", False,
                                         f"ambiguous: {query!r} matched {len(matches)} "
                                         f"candidate text boxes; add region to disambiguate", t0,
                                         kind="ambiguous_match")
                        raise StepFailure(r)
                    m: Match = matches[0]
                    self._mark_interaction("tap_text")  # 触发前打点（见 api_mark 注释）
                    with self._span("dispatch"):
                        resp, _ = self._request_dispatch(
                            {"t": "tap", "x": m.native_x, "y": m.native_y},
                            action="tap_text",
                            detail=f"tap {query!r}",
                            t0=t0,
                        )
                    self._dirty = True  # post-tap state differs; next ocr re-shoots
                    if not resp.get("ok"):
                        r = self._record("tap_text", False,
                                         f"tap {query!r} failed: {resp.get('error')}", t0,
                                         kind="tap_failed")
                        raise StepFailure(r)
                    self._settle()
                    detail = (f"tapped {m.text!r} @native({m.native_x},{m.native_y}) "
                              f"conf={m.confidence:.2f}")
                    return self._record("tap_text", True, detail, t0)
                last = f"no match for {query!r} (attempt {attempt + 1}/{retries})"
                logger.debug("tap_text 未命中 %r，第 %d/%d 次重试", query, attempt + 1, retries)
                if retry_delay_s > 0 and attempt < retries - 1:
                    with self._span("retry_wait"):
                        time.sleep(retry_delay_s)
            r = self._record("tap_text", False, last, t0, kind="locator_missing")
            raise StepFailure(r)

    def _verify_tap_ax_screen(self, screen_id: str, *, what: str, t0: float) -> dict[str, Any]:
        """Single-frame screen_id pre-check against screens.yaml before AX click (D4)."""
        from autophone.assets.screens import (
            ScreenAssetError,
            detect_blocking_overlay,
            match_screen,
            screen_by_id,
        )

        doc = self.screen_asset_doc
        if not isinstance(doc, dict):
            r = self._record(
                "tap_ax",
                False,
                f"{what}: screen_id={screen_id!r} cannot be verified (no screen_asset_doc loaded)",
                t0,
                kind="screen_mismatch",
            )
            raise StepFailure(r)
        try:
            screen_def = screen_by_id(doc, screen_id)
        except KeyError as exc:
            r = self._record(
                "tap_ax",
                False,
                f"{what}: unknown screen_id={screen_id!r} in screen_asset_doc",
                t0,
                kind="screen_mismatch",
            )
            raise StepFailure(r) from exc
        ocr = self.ocr_now(force=True)
        self._ocr = ocr
        try:
            match = match_screen(ocr, screen_def)
        except ScreenAssetError as exc:
            r = self._record(
                "tap_ax",
                False,
                f"{what}: screen_id={screen_id!r} pre-check error: {exc}",
                t0,
                kind="screen_mismatch",
            )
            raise StepFailure(r) from exc
        if not match.matched:
            r = self._record(
                "tap_ax",
                False,
                f"{what}: screen_id={screen_id!r} mismatch (reason={match.reason}, "
                f"missed={list(match.tokens_missed)}, forbid={list(match.forbid_hit)}, "
                f"hit={list(match.tokens_hit)})",
                t0,
                kind="screen_mismatch",
            )
            raise StepFailure(r)
        # Item (2): pre-tap OCR blocking_overlays fail-closed check on the same frame.
        overlay = detect_blocking_overlay(ocr, screen_def)
        if overlay is not None:
            overlay_name = str(overlay.get("name") or "blocking_overlay")
            r = self._record(
                "tap_ax",
                False,
                f"{what}: screen_id={screen_id!r} occluded by blocking overlay {overlay_name!r}",
                t0,
                kind="overlay_occluded",
            )
            raise StepFailure(r)
        return screen_def

    def tap_ax(self, query: str = "", *, role: str | None = None,
               identifier: str | None = None, region: Region | None = None,
               enabled_only: bool = True,
               fallback_xy: tuple[int, int] | None = None,
               label: str = "",
               screen_id: str | None = None,
               require_unique: bool = False) -> ActionResult:
        """Accessibility-locate a target and tap its native center — the structural
        peer of tap_text (§七). Reaches what OCR can't: icon-only buttons (no
        visible text, matched by `identifier` or an a11y `label`), and role/region
        disambiguation of a repeated label.

        When `screen_id` is supplied, verifies the current single OCR frame
        against `self.screen_asset_doc` (`screens.yaml`) before any tap dispatch;
        a mismatch fails closed with `kind="screen_mismatch"` and zero UDS taps.

        When `require_unique=True`, `fallback_xy` is ignored (strictly fail-closed)
        and matching 0 nodes (`locator_missing`) or >=2 nodes (`ambiguous_match`)
        raises `StepFailure` with zero UDS taps. Identifier-only lookups
        (`identifier` provided without `query`) likewise fail closed on 0 or >=2
        matches with zero UDS taps.

        Needs an AX provider on `ctx.ax` (AxSession; injected by the run
        orchestrator). When AX is unavailable — no provider, or a transport/parse
        failure — legacy non-unique callers degrade to `fallback_xy` if given,
        otherwise fail closed. `region` is the same normalized (nx0,ny0,nx1,ny1)
        box tap_text uses."""
        from autophone.assets.screens import detect_blocking_overlay
        from autophone.device.ax_session import UnsupportedAxBackendError

        t0 = time.perf_counter()
        what = label or identifier or query or role or "ax-target"
        effective_fallback = None if require_unique else fallback_xy
        identifier_only = bool(identifier) and not query

        screen_def: dict[str, Any] | None = None
        if screen_id is not None:
            screen_def = self._verify_tap_ax_screen(screen_id, what=what, t0=t0)

        if self.ax is None:
            if effective_fallback is not None:
                return self.tap_xy(*effective_fallback, label=f"{what} (no AX)")
            r = self._record("tap_ax", False, f"{what}: no AX provider wired", t0,
                             kind="ax_unavailable")
            raise StepFailure(r)

        # AX 树对**动画中途态**敏感（弹层/bottom sheet 未落定时，行 frame 停在过渡位——
        # 实测门店选择弹层动画中 refresh，门店行 native_y 落到屏外，tap 打空）。先 _settle
        # 到两帧一致（同 OCR 动作），再 refresh，AX 抓的是落定后的 frame。
        self._settle()
        try:
            with self._span("ax_refresh"):
                dump = self.ax.refresh()
        except UnsupportedAxBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 — AxUnavailable et al.
            if effective_fallback is not None:
                return self.tap_xy(*effective_fallback, label=f"{what} (AX refresh failed)")
            r = self._record("tap_ax", False, f"{what}: AX refresh failed: {exc}", t0,
                             kind="ax_unavailable")
            raise StepFailure(r) from exc

        # Item (1): Active foreground state gate — blocks Control Center / system
        # overlays even WITHOUT screen_id and NEVER degrades to fallback_xy.
        foreground_active = getattr(dump, "foreground_active", None)
        if foreground_active is False:
            occ_reason = str(getattr(dump, "foreground_occlusion_reason", "") or "not_foreground")
            r = self._record(
                "tap_ax",
                False,
                f"{what}: app/scene not foreground active ({occ_reason})",
                t0,
                kind="overlay_occluded",
            )
            raise StepFailure(r)
        app_state = getattr(dump, "application_state", None)
        scene_state = getattr(dump, "key_window_scene_activation_state", None)
        if (app_state is not None and app_state != 0) or (
            scene_state is not None and scene_state != 0
        ):
            r = self._record(
                "tap_ax",
                False,
                f"{what}: app/scene not foreground active "
                f"(application_state={app_state}, "
                f"key_window_scene_activation_state={scene_state})",
                t0,
                kind="overlay_occluded",
            )
            raise StepFailure(r)

        # Item (2) AX-side check: evaluate ax_text-based blocking_overlays on screen_def
        # against the fresh AX dump (fail-closed, 0 UDS taps, no fallback_xy).
        if screen_def is not None and any(
            isinstance(ov, dict)
            and isinstance(ov.get("detect"), dict)
            and ov["detect"].get("ax_text")
            for ov in (screen_def.get("blocking_overlays") or [])
        ):
            ax_overlay = detect_blocking_overlay(self._ocr, screen_def, ax_dump=dump)
            if ax_overlay is not None:
                overlay_name = str(ax_overlay.get("name") or "blocking_overlay")
                r = self._record(
                    "tap_ax",
                    False,
                    f"{what}: screen_id={screen_id!r} occluded by blocking overlay "
                    f"{overlay_name!r} (ax)",
                    t0,
                    kind="overlay_occluded",
                )
                raise StepFailure(r)

        matches = find_node(dump, query, role=role, identifier=identifier,
                            region=region, enabled_only=enabled_only)
        if not matches:
            if not require_unique and not identifier_only and effective_fallback is not None:
                return self.tap_xy(*effective_fallback, label=f"{what} (AX miss)")
            r = self._record("tap_ax", False,
                             f"no AX node for {what!r} "
                             f"(role={role} id={identifier}) in {len(dump.nodes)} nodes", t0,
                             kind="locator_missing")
            raise StepFailure(r)

        # Item (3): hittable == False occlusion gate — fail closed with 0 UDS taps
        # and NO fallback_xy degradation when all matched nodes are occluded.
        hittable_matches = [m for m in matches if getattr(m, "hittable", None) is not False]
        if not hittable_matches:
            occluded_reasons = sorted({
                str(getattr(m, "occluded_reason", "") or "occluded")
                for m in matches
            })
            r = self._record(
                "tap_ax",
                False,
                f"occluded: {what!r} matched {len(matches)} AX node(s) but none are hittable "
                f"(role={role} id={identifier} occluded_reasons={occluded_reasons})",
                t0,
                kind="locator_occluded",
            )
            raise StepFailure(r)

        # genuine ambiguity: fail-closed when require_unique or identifier-only;
        # legacy query-based callers may degrade to fallback_xy if given (§10.10).
        if ambiguous_label(
            hittable_matches,
            query,
            "label",
            identifier=identifier,
            require_unique=require_unique,
        ):
            if not require_unique and not identifier_only and effective_fallback is not None:
                return self.tap_xy(*effective_fallback, label=f"{what} (AX ambiguous)")
            target_desc = identifier or query or what
            r = self._record("tap_ax", False,
                             f"ambiguous: {target_desc!r} matched {len(hittable_matches)} candidate AX nodes "
                             f"(role={role} id={identifier}); add role/identifier/region",
                             t0, kind="ambiguous_match")
            raise StepFailure(r)

        n = hittable_matches[0]
        self._mark_interaction("tap_ax")  # 触发前打点（见 api_mark 注释）
        with self._span("dispatch"):
            resp, _ = self._request_dispatch(
                {"t": "tap", "x": n.native_x, "y": n.native_y},
                action="tap_ax",
                detail=f"tap {what!r}",
                t0=t0,
            )
        self._dirty = True
        if not resp.get("ok"):
            r = self._record("tap_ax", False,
                             f"tap {what!r} failed: {resp.get('error')}", t0,
                             kind="tap_failed")
            raise StepFailure(r)
        self._settle()
        self._last_ax_dump = dump
        detail = (f"tapped [{n.role}] {n.label or n.identifier or query!r} "
                  f"@native({n.native_x},{n.native_y})")
        return self._record("tap_ax", True, detail, t0)

    def wait_text(self, query: str, *, region: Region | None = None,
                  timeout_s: float = 8.0, poll_ms: int = 600,
                  min_confidence: float | None = None) -> ActionResult:
        """Poll screenshot+OCR until `query` appears or timeout — replaces a fixed
        sleep (§4.1)."""
        t0 = time.perf_counter()
        deadline = t0 + timeout_s
        with bind_step("wait_text"):
            polls = 0
            while True:
                ocr = self.ocr_now(force=True)
                if find_text(ocr, query, min_confidence=self._mc(min_confidence), region=region):
                    return self._record("wait_text", True, f"saw {query!r}", t0)
                if time.perf_counter() >= deadline:
                    r = self._record("wait_text", False,
                                     f"{query!r} not seen within {timeout_s}s", t0,
                                     kind="locator_missing")
                    raise StepFailure(r)
                polls += 1
                logger.debug("wait_text 轮询 %r 第 %d 次未见，已等 %.1fs",
                             query, polls, time.perf_counter() - t0)
                time.sleep(poll_ms / 1000.0)

    def assert_screen(self, screen_id: str, screen_asset: str, screen_sha256: str,
                      *, retries: int = 3) -> ActionResult:
        """Assert an active Screen on a fresh required frame, bound to exact asset bytes."""
        import hashlib
        import yaml
        from types import SimpleNamespace
        from autophone.assets.screens import validate_screen_asset
        from autophone.execution.engine.recipe import scoped_asset_root
        from autophone.execution.operation_run import _checkpoint_screen

        t0 = time.perf_counter()
        label = f"{self.case_id}_{len(self.results):03d}"
        evidence = self.shot_dir / f"assert_screen_{label}.json"
        checkpoint: dict[str, Any] = {"status": "failed", "expected": screen_id}
        # This action declares OCR only. Do not activate a lazy AX/Frida session
        # through the Operation checkpoint helper's supplemental evidence hook.
        checkpoint_ctx = SimpleNamespace(_grab_raw=self._grab_raw, ax=None, checkpoint_identity={})
        try:
            root = scoped_asset_root().resolve()
            path = (root / screen_asset).resolve()
            if Path(screen_asset).is_absolute() or not path.is_relative_to(root):
                raise ValueError("screen_asset_outside_root")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != screen_sha256:
                raise ValueError("screen_asset_identity_mismatch")
            doc = yaml.safe_load(raw)
            validate_screen_asset(doc, path=path)
            if not any(s.get("id") == screen_id and s.get("status") == "active" for s in doc["screens"]):
                raise ValueError("screen_not_active")
            checkpoint = _checkpoint_screen(
                checkpoint_ctx, self.shot_dir, screen_doc=doc, expected=screen_id, label=label,
                ocr_runner=run_ocr, statuses=("active",), retries=retries, interval_s=0.3,
            )
            if checkpoint["status"] == "passed":
                # A navigation may still expose the previous page in its first fresh frame.
                # Confirm on another required frame; preserve both observations.
                initial = checkpoint
                time.sleep(0.3)
                checkpoint = _checkpoint_screen(
                    checkpoint_ctx, self.shot_dir, screen_doc=doc, expected=screen_id, label=label + "_confirm",
                    ocr_runner=run_ocr, statuses=("active",), retries=retries, interval_s=0.3,
                )
                checkpoint["initial_match"] = initial
            if path.read_bytes() != raw:
                checkpoint.update(status="failed", reason="screen_asset_identity_mismatch")
        except Exception as exc:  # noqa: BLE001 required assertion cannot silently pass
            checkpoint.update(status="failed", reason="checkpoint_error", error=f"{type(exc).__name__}: {exc}")
        checkpoint["screen_sha256"] = screen_sha256
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = checkpoint["status"] == "passed"
        result = self._record("assert_screen", ok, json.dumps(checkpoint, ensure_ascii=False), t0,
                              kind="" if ok else "assertion_failed",
                              evidence_refs=[evidence.relative_to(self.shot_dir.parent).as_posix()])
        self._dirty = True
        if not ok:
            raise StepFailure(result)
        return result

    def assert_present(self, queries: list[str], *, region: Region | None = None,
                       min_confidence: float | None = None,
                       wait_s: float = 6.0, poll_ms: int = 600) -> ActionResult:
        """Every query must be found. Polls screenshot+OCR up to `wait_s` so a
        slow-loading sub-page doesn't fail vacuously on the first frame (set
        wait_s=0 for a strict single-frame check). This is the executable form of
        「预期结果」."""
        return self._assert_with_ocr(
            "assert_present", queries,
            lambda: (self.ocr_now(force=True), None),
            region=region, min_confidence=min_confidence,
            wait_s=wait_s, poll_ms=poll_ms)

    def _assert_with_ocr(self, action: str, queries: list[str],
                         ocr_fn: Callable[[], tuple[dict[str, Any], str | None]], *,
                         region: Region | None, min_confidence: float | None,
                         wait_s: float, poll_ms: int,
                         suffix: str = "") -> ActionResult:
        """assert_present / assert_present_enhanced 的共用轮询体；只有 OCR 来源
        （裸帧 vs 灰阶增强帧）不同。ocr_fn 返回 (ocr, 增强帧路径或 None)。"""
        t0 = time.perf_counter()
        deadline = t0 + wait_s
        missing = list(queries)
        aux_path: str | None = None
        while True:
            ocr, aux_path = ocr_fn()
            missing = [q for q in queries
                       if not find_text(ocr, q, min_confidence=self._mc(min_confidence), region=region)]
            if not missing:
                return self._record(action, True, f"present {queries}{suffix}", t0,
                                    aux_shots=[aux_path] if aux_path else None)
            if time.perf_counter() >= deadline:
                break
            time.sleep(poll_ms / 1000.0)
        r = self._record(action, False,
                         f"missing {missing} of {queries}{suffix}", t0,
                         aux_shots=[aux_path] if aux_path else None,
                         kind="assertion_failed")
        raise StepFailure(r)

    def _enhanced_ocr(self, level: str = ENHANCE_LEVEL) -> tuple[dict[str, Any], str]:
        """灰阶 level 拉伸后 OCR，给低对比度页用（协议规则等）。fail-closed：
        优先用 magick；缺失时退到 Pillow 做等价的灰阶线性拉伸。原始帧入 shot trail，
        增强帧另存 <case>_NN.enh.jpg。返回 (ocr_dict, 增强帧路径)，调用方把增强帧
        塞进 aux_shots，让报告能展示「判定所依据的实际 OCR 输入」。"""
        with self._span("screenshot"):
            src = self.screenshot()  # 原始帧入 shot trail，便于人工复核
        enh = src.with_suffix(".enh.jpg")
        with self._span("enhance"):
            if shutil.which("magick") is not None:
                proc = subprocess.run(
                    ["magick", str(src), "-colorspace", "Gray", "-level", level, str(enh)],
                    capture_output=True, text=True)
                if proc.returncode != 0:
                    raise OcrError(f"magick enhance rc={proc.returncode}: {proc.stderr.strip()}")
            else:
                try:
                    from PIL import Image
                except ModuleNotFoundError as exc:
                    raise OcrError(
                        "enhanced OCR needs ImageMagick `magick` or Pillow; run `uv sync`"
                    ) from exc
                lo, hi = _level_bounds(level)
                scale = 255 / (hi - lo)
                with Image.open(src) as img:
                    gray = img.convert("L")
                    enhanced = gray.point(
                        lambda p: 0 if p <= lo else (255 if p >= hi else round((p - lo) * scale))
                    )
                    enhanced.save(enh, quality=92)
        with self._span("ocr"):
            ocr = run_ocr(enh)
        return ocr, str(enh)

    def assert_present_enhanced(self, queries: list[str], *,
                                region: Region | None = None,
                                min_confidence: float | None = None,
                                level: str = ENHANCE_LEVEL,
                                wait_s: float = 6.0, poll_ms: int = 600) -> ActionResult:
        """assert_present 的增强 OCR 变体：每帧先做灰阶拉伸再识别。用于浅灰文字 +
        水印导致裸 OCR 返回 0 box 的页面（§协议规则）。语义与 assert_present 一致。"""
        return self._assert_with_ocr(
            "assert_present_enhanced", queries,
            lambda: self._enhanced_ocr(level),
            region=region, min_confidence=min_confidence,
            wait_s=wait_s, poll_ms=poll_ms, suffix=" (enhanced)")

    def assert_region_nonempty(self, regions: list[tuple[Region, str]], *,
                               min_confidence: float = 0.3,
                               level: str = ENHANCE_LEVEL,
                               wait_s: float = 6.0, poll_ms: int = 600) -> ActionResult:
        """每个 (region, label) 的归一化区域内，增强 OCR 至少命中一个 box（conf≥阈值）。

        用于「该区域确实渲染了内容」的存在性断言，当区域内文字 OCR 不可稳定**识读**
        （浅灰艺术字 + 水印 → 字符误读，如『注册协议』→『汪册协议』）但**有像素文本可被
        框出**时。比赌一个糊掉的子串忠实：它断言的是「这块卡片渲染了」，不假装能读准字。
        regions 用 normalized (nx0,ny0,nx1,ny1)；box 中心落入区域且 conf≥阈值即算命中。"""
        t0 = time.perf_counter()
        deadline = t0 + wait_s
        missing: list[str] = [lbl for _, lbl in regions]
        enh_path = None
        while True:
            ocr, enh_path = self._enhanced_ocr(level)
            iw, ih = ocr["image_w"], ocr["image_h"]
            missing = []
            for (rx0, ry0, rx1, ry1), label in regions:
                hit = False
                for box in ocr["boxes"]:
                    if box["confidence"] < min_confidence:
                        continue
                    nx = (box["x"] + box["w"] / 2.0) / iw
                    ny = (box["y"] + box["h"] / 2.0) / ih
                    if rx0 <= nx <= rx1 and ry0 <= ny <= ry1:
                        hit = True
                        break
                if not hit:
                    missing.append(label)
            if not missing:
                labels = [lbl for _, lbl in regions]
                return self._record("assert_region_nonempty", True,
                                    f"rendered {labels} (enhanced, region)", t0,
                                    aux_shots=[enh_path])
            if time.perf_counter() >= deadline:
                break
            time.sleep(poll_ms / 1000.0)
        r = self._record("assert_region_nonempty", False,
                         f"empty region(s) {missing} (enhanced)", t0,
                         aux_shots=[enh_path] if enh_path else None,
                         kind="assertion_failed")
        raise StepFailure(r)

    def assert_absent(self, query: str, *, region: Region | None = None,
                      min_confidence: float | None = None) -> ActionResult:
        t0 = time.perf_counter()
        ocr = self.ocr_now()
        if find_text(ocr, query, min_confidence=self._mc(min_confidence), region=region):
            r = self._record("assert_absent", False, f"unexpectedly present: {query!r}", t0,
                             kind="assertion_failed")
            raise StepFailure(r)
        return self._record("assert_absent", True, f"absent {query!r}", t0)

    def assert_remains_absent(
        self,
        query: str,
        *,
        region: Region | None = None,
        min_confidence: float | None = None,
        duration_s: float = 3.0,
        poll_ms: int = 300,
    ) -> ActionResult:
        """Continuously require ``query`` to stay absent for an observation window.

        This is a temporal assertion, not ``assert_absent`` followed by a fixed
        sleep: every interval captures a fresh frame, and the first observed
        match fails the step immediately.
        """
        if duration_s <= 0:
            raise ValueError("duration_s 必须大于 0")
        if poll_ms <= 0:
            raise ValueError("poll_ms 必须大于 0")
        t0 = time.perf_counter()
        deadline: float | None = None
        polls = 0
        poll_shots: list[str] = []
        with bind_step("assert_remains_absent"):
            while True:
                capture_started = time.perf_counter()
                if deadline is None:
                    deadline = capture_started + duration_s
                ocr = self.ocr_now(force=True)
                polls += 1
                if self._last_shot:
                    poll_shots.append(self._last_shot)
                if find_text(
                    ocr,
                    query,
                    min_confidence=self._mc(min_confidence),
                    region=region,
                ):
                    r = self._record(
                        "assert_remains_absent",
                        False,
                        f"unexpectedly present during {duration_s}s window: {query!r}",
                        t0,
                        aux_shots=poll_shots[:-1],
                        kind="assertion_failed",
                    )
                    raise StepFailure(r)
                if capture_started >= deadline:
                    return self._record(
                        "assert_remains_absent",
                        True,
                        f"remained absent for {duration_s}s: {query!r} ({polls} polls)",
                        t0,
                        aux_shots=poll_shots[:-1],
                    )
                now = time.perf_counter()
                if now < deadline:
                    time.sleep(min(poll_ms / 1000.0, deadline - now))

    def handle_dialog_if_present(self, query: str, *, tap_query: str,
                                 region: Region | None = None,
                                 min_confidence: float | None = None) -> ActionResult:
        """Conditional branch (§4.1): if `query` is on screen, tap `tap_query`;
        otherwise skip silently. Never a hard failure on absence — lets one
        script cover both the 200m-inside and 200m-outside routes."""
        t0 = time.perf_counter()
        ocr = self.ocr_now()
        if not find_text(ocr, query, min_confidence=self._mc(min_confidence), region=region):
            return self._record("handle_dialog_if_present", True,
                                f"{query!r} absent — skipped", t0)
        # dialog present → tap the confirm button (its own re-shoot via tap_text)
        self.tap_text(tap_query, region=region)
        return self._record("handle_dialog_if_present", True,
                            f"{query!r} present → tapped {tap_query!r}", t0)

    def type_text(self, text: str) -> ActionResult:
        """Clipboard write (NOT keyboard input) — `type` is UIPasteboard setString
        (CLAUDE.md). The caller must have focused a field and arranged a paste;
        this only stages the text. Long tail → TYPE_TIMEOUT_S."""
        t0 = time.perf_counter()
        self._mark_interaction("type_text")  # 触发前打点：paste 触发的 search-as-you-type 等 API 才归到本动作
        with self._span("dispatch"):
            resp, _ = self._request_dispatch(
                {"t": "type", "text": text, "screen": False},
                action="type_text",
                detail="type_text",
                t0=t0,
                timeout=max(self.timeout, TYPE_TIMEOUT_S),
            )
        ok = bool(resp.get("ok"))
        r = self._record("type_text", ok,
                         f"clipboard<={text!r}" if ok else f"type failed: {resp.get('error')}", t0,
                         kind="" if ok else "input_failed")
        if not ok:
            raise StepFailure(r)
        return r

    # ── SpringBoard guard ──────────────────────────────────────────────────
    def _read_springboard(self, action: str) -> tuple[bool, SpringBoardIdentity | None]:
        """``(可判定, 身份)``。读到不在时间隔 ``SPRINGBOARD_ABSENT_RECHECK_S`` 复探一次。"""
        try:
            identity = probe_springboard(self.sock_path)
            if identity is None:
                time.sleep(SPRINGBOARD_ABSENT_RECHECK_S)
                identity = probe_springboard(self.sock_path)
        except SpringBoardProbeError as exc:
            logger.warning("%s: SpringBoard 身份不可判定，跳过比对：%s", action, exc)
            return False, None
        return True, identity

    def springboard_identity(self, action: str) -> SpringBoardIdentity | None:
        """读取当前 SpringBoard 身份，供启动类动作前后比对。

        SpringBoard 不在进程表中（间隔复探后仍不在）时记录失败并抛 rig 范围阻断
        ``springboard_not_running``。探测本身不可判定（shell 失败、输出格式不符）时返回 None
        并写 WARNING，调用方跳过比对，动作照常执行：守卫不新增失败来源。"""
        t0 = time.perf_counter()
        conclusive, identity = self._read_springboard(action)
        if not conclusive:
            return None
        if identity is None:
            self._springboard_blocked(
                action, "springboard_not_running",
                "SpringBoard is not in the guest process table", t0)
        return identity

    def ensure_springboard_unchanged(self, before: SpringBoardIdentity | None, action: str) -> None:
        """``before`` 之后 SpringBoard 退出或重启即抛 rig 范围阻断。

        ``before`` 为 None（事前不可判定）时不比对；事后不可判定时同样只写 WARNING。"""
        if before is None:
            return
        t0 = time.perf_counter()
        conclusive, after = self._read_springboard(action)
        if not conclusive:
            return
        if after is None:
            self._springboard_blocked(
                action, "springboard_not_running",
                f"SpringBoard exited during {action} (was {before.describe()})", t0)
        if after != before:
            self._springboard_blocked(
                action, "springboard_restarted",
                f"SpringBoard restarted during {action}: {before.describe()} -> {after.describe()}",
                t0)

    def _springboard_blocked(self, action: str, code: str, detail: str, t0: float) -> None:
        self._record("springboard_guard", False, f"{action}: {detail}", t0, kind=code)
        logger.error("%s: %s（设施故障，按 rig 阻断结束）", action, detail)
        self._dirty = True
        raise CaseBlocked(
            blocker(
                "rig", code, capability="springboard",
                recovery=("SpringBoard crashed or restarted on the guest; collect the guest "
                          "SpringBoard-*.ips crash report, confirm the home screen is back, "
                          "then rerun"),
            ),
            detail=f"{action}: {detail}",
        )

    def launch_app(
        self,
        bundle_id: str,
        *,
        ready_markers: list[str] | None = None,
        springboard_icon_preview: tuple[int, int] | None = None,
        startup_timeout_s: float = 6.0,
    ) -> ActionResult:
        """Launch an app by bundle id, then verify that its process materializes.

        Updated vphoned builds expose an entitled native ``app_launch`` command;
        use it first so launch does not occupy the serial shell channel.  Older
        guests that reject the command fall back to the ``uiopen`` CLI.  PID/ready
        marker verification below remains fail-closed for both paths.

        SpringBoard 在启动转场中崩溃时 App 进程随之消失（2026-09-24 vm 两次复现）。
        启动前后比对 SpringBoard 身份：退出或重启时以 rig 范围 ``CaseBlocked`` 结束，
        启动以任何异常失败（StepFailure、UDS 超时、OSError、ProtocolError 等）也先比对，
        SpringBoard 变了即按此改判，不报 App 侧或传输原因。"""
        before = self.springboard_identity("launch_app")
        try:
            result = self._launch_app_unguarded(
                bundle_id,
                ready_markers=ready_markers,
                springboard_icon_preview=springboard_icon_preview,
                startup_timeout_s=startup_timeout_s,
            )
        except Exception:
            self.ensure_springboard_unchanged(before, "launch_app")
            raise
        self.ensure_springboard_unchanged(before, "launch_app")
        return result

    def _launch_app_unguarded(
        self,
        bundle_id: str,
        *,
        ready_markers: list[str] | None,
        springboard_icon_preview: tuple[int, int] | None,
        startup_timeout_s: float,
    ) -> ActionResult:
        t0 = time.perf_counter()
        launch_via = "native app_launch"
        native_failure = ""
        # 就绪标记兜底只在 pid 不可得时生效；先留一帧启动前画面，用于核对标记所在帧是否新鲜。
        baseline = self._pre_launch_frame() if ready_markers else None
        try:
            response, _ = request(
                {"t": "app_launch", "bundle_id": bundle_id, "screen": False},
                sock_path=self.sock_path,
                timeout=20.0,
                parse_image=False,
            )
        except Exception as exc:  # noqa: BLE001 old/partially upgraded guest
            native_failure = str(exc)
        else:
            if not response.get("ok"):
                native_failure = f"rejected: {response.get('error')!r}"

        if native_failure:
            try:
                r = run_shell(
                    f"uiopen --bundleid {shlex.quote(bundle_id)}",
                    timeout_ms=15_000,
                    sock_path=self.sock_path,
                )
            except ShellError as exc:
                res = self._record(
                    "launch_app",
                    False,
                    f"launch {bundle_id!r} failed: native app_launch {native_failure}; "
                    f"uiopen error: {exc}",
                    t0,
                    kind="launch_failed",
                )
                raise StepFailure(res) from exc
            if r.code != 0:
                res = self._record(
                    "launch_app",
                    False,
                    f"launch {bundle_id!r} failed: native app_launch {native_failure}; "
                    f"uiopen exit {r.code}: {r.stderr.strip()!r}",
                    t0,
                    kind="launch_failed",
                )
                raise StepFailure(res)
            launch_via = "uiopen fallback"
        # A successful launch response only means the request was accepted. Fail closed by
        # confirming the app actually materialized: poll app_list until its pid
        # appears (covers cold start / a bad bundle id that uiopen still accepts).
        startup_deadline = time.perf_counter() + max(0.0, float(startup_timeout_s))
        pid = self._await_app_pid(
            bundle_id,
            deadline_s=min(3.0, max(0.0, startup_deadline - time.perf_counter())),
        )
        if pid <= 0:
            stale_marker = ""
            marker = self._await_app_ready_marker(
                ready_markers or [],
                deadline_s=max(0.0, startup_deadline - time.perf_counter()),
            )
            if marker and self._marker_frame_is_stale(baseline):
                stale_marker, marker = marker, ""
            if marker:
                self._settle()
                self._dirty = True
                return self._record(
                    "launch_app",
                    True,
                    f"launched {bundle_id!r} pid=unavailable ready marker={marker!r}",
                    t0,
                )
            if springboard_icon_preview is not None:
                self._tap_springboard_icon(springboard_icon_preview)
                marker = self._await_app_ready_marker(ready_markers or [], deadline_s=5.0)
                if marker and self._marker_frame_is_stale(baseline):
                    stale_marker, marker = marker, ""
                if marker:
                    self._settle()
                    self._dirty = True
                    return self._record(
                        "launch_app",
                        True,
                        f"launched {bundle_id!r} via springboard icon ready marker={marker!r}",
                        t0,
                    )
            detail = f"{bundle_id!r} did not start ({launch_via} ok, no pid)"
            if stale_marker:
                logger.warning("launch_app：就绪标记 %r 只出现在与启动前字节相同的帧上，截图未刷新"
                               "（可能熄屏或锁屏），不判启动成功", stale_marker)
                detail += (f"; ready marker {stale_marker!r} only on a frame byte-identical to the "
                           f"pre-launch frame ({STALE_SCREEN_MARK}: display off or device locked?)")
            res = self._record("launch_app", False, detail, t0, kind="launch_failed")
            raise StepFailure(res)
        self._settle()
        self._dirty = True
        return self._record(
            "launch_app", True, f"launched {bundle_id!r} pid={pid} via {launch_via}", t0
        )

    def _pre_launch_frame(self) -> bytes | None:
        """Raw bytes of the screen before launch; None when no frame is available."""
        try:
            return self._grab_raw()
        except Exception as exc:  # noqa: BLE001 - freshness then stays unverified
            logger.debug("pre-launch frame unavailable: %s", exc)
            return None

    def _marker_frame_is_stale(self, baseline: bytes | None) -> bool:
        """Whether the frame that carried the ready marker equals the pre-launch frame.

        A display that stopped refreshing returns the same JPEG bytes on every
        screenshot. Without a baseline or a saved marker frame the freshness is
        unverified and the existing fallback verdict stands.
        """
        if baseline is None or not self._last_shot:
            return False
        try:
            return Path(self._last_shot).read_bytes() == baseline
        except OSError:
            return False

    def _await_app_ready_marker(self, markers: list[str], *, deadline_s: float) -> str:
        """Poll screenshot OCR for a profile-owned ready marker.

        This is a narrow fallback for guest builds where SpringBoard visibly
        foregrounds the app but `app_list` / `app_foreground` report stale data.
        No markers means no fallback: callers that do not know the app-specific
        ready surface still fail closed on missing pid.
        """
        markers = [str(m).strip() for m in markers if str(m).strip()]
        if not markers:
            return ""
        end = time.perf_counter() + deadline_s
        while True:
            try:
                ocr = self.ocr_now(force=True)
                for marker in markers:
                    if find_text(ocr, marker, min_confidence=self._mc(0.4)):
                        return marker
            except Exception as exc:  # noqa: BLE001 - keep polling until deadline
                logger.debug("launch ready marker probe failed: %s", exc)
            if time.perf_counter() >= end:
                return ""
            time.sleep(0.5)

    def _tap_springboard_icon(self, preview_xy: tuple[int, int]) -> None:
        """Fallback for broken bundle-id launch registration.

        Some VM states keep the SpringBoard icon usable while
        `uiopen --bundleid` and LaunchServices info both fail. The caller must
        opt in with an app-profile-owned icon coordinate.
        """
        request({"t": "key", "name": "home", "screen": False},
                sock_path=self.sock_path, timeout=self.timeout)
        time.sleep(0.5)
        x, y = preview_xy
        nx, ny = preview_to_native(int(x), int(y))
        request({"t": "tap", "x": nx, "y": ny, "screen": False},
                sock_path=self.sock_path, timeout=self.timeout)
        self._dirty = True

    def _await_app_pid(self, bundle_id: str, *, deadline_s: float, require_readback: bool = False) -> int:
        """Poll the guest app_list for `bundle_id`, returning its pid (>0) once it
        is running, or 0 if it never appears before the deadline. Used to make
        launch_app fail-closed on the current guest build, whose UDS app_launch
        does not itself verify materialization."""
        end = time.perf_counter() + deadline_s
        while True:
            resp, _ = request({"t": "app_list", "filter": "running", "screen": False},
                              sock_path=self.sock_path, timeout=self.timeout)
            if require_readback and (resp.get("ok") is not True or not isinstance(resp.get("apps"), list)):
                raise ProtocolError(f"app_list readback unavailable: {resp.get('error')!r}")
            for app in resp.get("apps", []):
                if app.get("bundle_id") == bundle_id:
                    pid = int(app.get("pid") or 0)
                    if pid > 0:
                        return pid
            if time.perf_counter() >= end:
                return 0
            time.sleep(0.2)

    def _remaining_app_pid(self, bundle_id: str, *, deadline_s: float) -> int:
        """Return zero on verified absence or the last PID at the deadline."""
        end = time.perf_counter() + deadline_s
        while True:
            pid = self._await_app_pid(bundle_id, deadline_s=0.0, require_readback=True)
            remaining = end - time.perf_counter()
            if pid == 0 or remaining <= 0:
                return pid
            time.sleep(min(0.2, remaining))

    def terminate_app(self, bundle_id: str) -> ActionResult:
        """Kill `bundle_id` via the UDS app_terminate command, fail-closed. An app
        that is not running is a no-op success — the goal is "not running", which
        an already-dead app satisfies. Otherwise we issue app_terminate and poll
        app_list until the pid is gone (app_terminate's ok only means the request
        was accepted, not that the process died — see VPhoneHostControl).

        ok=false is likewise not a conclusion: the guest returns it when its own
        single 200 ms sample after SIGKILL still sees the process, so we re-check
        via app_list. If the original process was replaced by another instance
        of the same app, terminate that replacement once and verify absence."""
        t0 = time.perf_counter()
        original_pid = self._await_app_pid(bundle_id, deadline_s=0.0, require_readback=True)
        if original_pid <= 0:
            return self._record("terminate_app", True, f"{bundle_id!r} not running", t0)
        command = {"t": "app_terminate", "bundle_id": bundle_id, "screen": False}
        resp, _ = request(command, sock_path=self.sock_path, timeout=self.timeout)
        remaining_pid = self._remaining_app_pid(bundle_id, deadline_s=5.0)
        replacement_pid = None
        if resp.get("ok") is True and remaining_pid not in (0, original_pid):
            replacement_pid = remaining_pid
            resp, _ = request(command, sock_path=self.sock_path, timeout=self.timeout)
            remaining_pid = self._remaining_app_pid(bundle_id, deadline_s=5.0)
        if remaining_pid:
            detail = (f"{bundle_id!r} still running after app_terminate"
                      if resp.get("ok")
                      else f"app_terminate {bundle_id!r} failed: {resp.get('error')!r}, "
                           f"and {bundle_id!r} is still running")
            detail += f" (original pid={original_pid}, remaining pid={remaining_pid})"
            res = self._record("terminate_app", False, detail, t0, kind="launch_failed")
            raise StepFailure(res)
        self._dirty = True
        detail = f"terminated {bundle_id!r} (pid={original_pid})"
        if replacement_pid is not None:
            detail += f" and replacement pid={replacement_pid}"
        return self._record("terminate_app", True, detail, t0)

    def relaunch_app(
        self,
        bundle_id: str,
        *,
        ready_markers: list[str] | None = None,
        springboard_icon_preview: tuple[int, int] | None = None,
    ) -> ActionResult:
        """Cold-restart `bundle_id` to its launch screen: terminate first (so the
        next launch is a real cold start, not just a foreground — `uiopen
        --bundleid` only foregrounds a live app and won't unwind in-app nav), then
        launch_app (fail-closed pid verify + settle). This is the deterministic
        reset entry that does not depend on in-app back-navigation: wherever the
        app was left, it lands on the app's launch screen."""
        self.terminate_app(bundle_id)
        return self.launch_app(
            bundle_id,
            ready_markers=ready_markers,
            springboard_icon_preview=springboard_icon_preview,
        )

    def open_url(self, url: str) -> ActionResult:
        """Open a URL on the guest via `uiopen --url` (same entitlement rationale
        as launch_app). Note custom app schemes only route if the target app
        registers them in CFBundleURLTypes; an unregistered scheme is a no-op.
        That is an app-registration fact, not a launch-channel failure."""
        t0 = time.perf_counter()
        try:
            r = run_shell(f"uiopen --url {shlex.quote(url)}",
                          timeout_ms=15_000, sock_path=self.sock_path)
        except ShellError as exc:
            res = self._record("open_url", False,
                               f"open {url!r} failed: {exc}", t0, kind="launch_failed")
            raise StepFailure(res) from exc
        ok = r.code == 0
        if not ok:
            res = self._record("open_url", False,
                               f"uiopen exit {r.code} for {url!r}: "
                               f"{r.stderr.strip()!r}", t0, kind="launch_failed")
            raise StepFailure(res)
        self._settle()
        self._dirty = True
        return self._record("open_url", True, f"opened {url!r}", t0)
