"""平台健康检查聚合：把分散的健康源汇成一张 doctor 卡（`GET /api/doctor`）。

四个源，各自独立 try/except **fail-open**（一个源挂不连累其它，整卡仍出）：
  - vsock_health 守护进程状态文件（`peek_state`，纯文件读、never raises）
  - UDS screenshot 探活（一帧，证明设备可达 + 截图面就绪）
  - guest root shell 轻探活（`id -u`，证明 shell 链路活 + root 身份）
  - AI provider CLI doctor（在不在 / 版本 / 默认 provider，**不** probe 真调用）

每个 check 返回 `{id,title,status,detail}`，status ∈ {ok,warn,down}；整体 status =
各 check 最差（ok→healthy / warn→degraded / down→down）。重活（完整 shell_smoke 11 项、
ai --probe 真调用、frida 抓取）**不在此处跑**——doctor 是轻量随手探活，不驱动被测 app。

各 check 接口都开了注入 seam（默认懒 import 真实现），宿主无 VM 也能单测。
运行期回归链不经此路：doctor 只在 `/api/doctor` 端点被显式调用。
"""
from __future__ import annotations

from typing import Any, Callable

_STATUS_ORDER = {"down": 0, "warn": 1, "ok": 2}
_OVERALL = {"ok": "healthy", "warn": "degraded", "down": "down"}


def _num(v: Any) -> str:
    return "—" if v is None else str(v)


def check_vsock(peek: Callable[[], Any] | None = None) -> dict[str, Any]:
    """vsock_health 守护进程状态文件。daemon 没跑/陈旧 → warn（不等于设备死，截图/shell 另证）。"""
    title = "vsock_health 守护进程"
    try:
        if peek is None:
            from autophone.device.vsock_health_client import peek_state as peek
        hs = peek()
        state = hs.state
        if state == "healthy":
            st = "ok"
        elif state == "down":
            st = "down"
        else:  # degraded / unknown / stale / missing
            st = "warn"
        if state in ("missing", "stale"):
            detail = f"{state} · {hs.last_error or 'daemon 未在更新状态文件'}"
        else:
            detail = f"{state} · UDS p50 {_num(hs.uds_rt_p50_ms)}ms · age {_num(hs.age_s)}s · probes {hs.probe_count}"
        return {"id": "vsock_health", "title": title, "status": st, "detail": detail}
    except Exception as e:  # noqa: BLE001 fail-open
        return {"id": "vsock_health", "title": title, "status": "down", "detail": f"读取失败: {e}"}


def check_screenshot(request: Callable[..., Any] | None = None,
                     resolve_sock: Callable[[], str] | None = None) -> dict[str, Any]:
    """一帧 UDS 截图：证明设备可达 + 截图面就绪。无帧 / 异常 → down。"""
    title = "screenshot UDS"
    try:
        if request is None:
            from autophone.device.uds import request
        if resolve_sock is None:
            from autophone.execution.engine.actions import resolve_sock
        import base64
        obj, _ = request({"t": "screenshot"}, sock_path=resolve_sock(), timeout=5)
        image = obj.get("image")
        if not image:
            return {"id": "screenshot", "title": title, "status": "down",
                    "detail": f"截图无 image 字段: {obj.get('error') or 'ok=' + str(obj.get('ok'))}"}
        nbytes = len(base64.b64decode(image))
        return {"id": "screenshot", "title": title, "status": "ok",
                "detail": f"一帧 {nbytes} bytes · 设备可达 · 坐标面就绪"}
    except Exception as e:  # noqa: BLE001 fail-open
        return {"id": "screenshot", "title": title, "status": "down", "detail": f"截图失败: {e}"}


def check_shell(run_shell: Callable[..., Any] | None = None) -> dict[str, Any]:
    """guest root shell 轻探活（`id -u`，4s 上限）。非零 / 异常 → down；uid≠0 → warn。"""
    title = "guest root shell"
    try:
        if run_shell is None:
            from autophone.device.shell import run_shell
        r = run_shell("id -u", timeout_ms=4000)
        uid = (r.stdout or "").strip()
        if r.code != 0:
            return {"id": "shell", "title": title, "status": "down",
                    "detail": f"code={r.code} · {(r.stderr or '').strip()[:80]}"}
        st = "ok" if uid == "0" else "warn"
        note = "root" if uid == "0" else f"非 root（uid={uid}）"
        return {"id": "shell", "title": title, "status": st, "detail": f"{note} · id -u 探活通过"}
    except Exception as e:  # noqa: BLE001 fail-open
        return {"id": "shell", "title": title, "status": "down", "detail": f"shell 探活失败: {type(e).__name__}: {e}"}


def check_ai(ai_doctor: Callable[..., Any] | None = None) -> dict[str, Any]:
    """AI provider CLI 在不在 / 版本（不 probe 真调用）。无可用 provider → warn（AI 为可选能力）。"""
    title = "AI provider CLI"
    try:
        if ai_doctor is None:
            from autophone.application.ai import ai_doctor
        d = ai_doctor(probe=False)
        providers = d.get("providers", {}) or {}
        avail = [n for n, p in providers.items() if p.get("available")]
        st = "ok" if d.get("status") == "healthy" else "warn"
        detail = f"可用 {', '.join(avail) if avail else '无'} · 默认 {d.get('resolved_default') or '—'}"
        return {"id": "ai", "title": title, "status": st, "detail": detail}
    except Exception as e:  # noqa: BLE001 fail-open
        return {"id": "ai", "title": title, "status": "warn", "detail": f"AI doctor 不可用: {e}"}


def check_ffmpeg(which: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """ffmpeg 在不在：run 期录屏 mux 成 mp4 的前置。缺 → warn（录屏降级为仅存帧，无可播放视频）。"""
    title = "ffmpeg（录屏 mux）"
    try:
        if which is None:
            import shutil
            which = shutil.which
        path = which("ffmpeg")
        if path:
            return {"id": "ffmpeg", "title": title, "status": "ok", "detail": f"已安装 · {path}"}
        return {"id": "ffmpeg", "title": title, "status": "warn",
                "detail": "未安装 · 录屏只存帧不出 mp4（brew install ffmpeg）"}
    except Exception as e:  # noqa: BLE001 fail-open
        return {"id": "ffmpeg", "title": title, "status": "warn", "detail": f"探测失败: {e}"}


def run_doctor(checks: list[Callable[[], dict[str, Any]]] | None = None) -> dict[str, Any]:
    """聚合各源 → `{schema_version, status, checks:[...]}`。整体态取各 check 最差。"""
    if checks is None:
        checks = [check_vsock, check_screenshot, check_shell, check_ai, check_ffmpeg]
    results = [c() for c in checks]
    worst = min((r["status"] for r in results), key=lambda s: _STATUS_ORDER.get(s, 0)) if results else "down"
    return {"schema_version": "v1", "status": _OVERALL.get(worst, "down"), "checks": results}
