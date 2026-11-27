"""Login-state probe boundary for Phase 5.

Default calls are fail-closed and do not touch the device. Pass live=True (or
use the CLI) to classify the current vphone screen via screenshot + OCR.
"""
from __future__ import annotations

import argparse
import base64
import json
from dataclasses import dataclass
from pathlib import Path

from autophone.device.lock_state import (
    CUSTOMER_SERVICE_MARKERS,
    LOGIN_MARKERS,
    looks_like_lockscreen,
)
from autophone.paths import STATE_ROOT
from autophone.execution.engine.actions import resolve_sock


@dataclass
class LoginStateResult:
    status: str       # persisted | needs_login | unknown | customer_service_probe
    detail: str
    evidence_ref: str = ""
    ocr_texts: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "evidence_ref": self.evidence_ref,
            "ocr_texts": list(self.ocr_texts or []),
        }


def _classify_texts(texts: list[str], evidence_ref: str = "") -> LoginStateResult:
    """黑名单模式：检测登录页和客服页，其它默认已登录。

    客服页返回 customer_service_probe 状态，需要点返回进一步判断。
    """
    joined = "\n".join(texts)

    # 锁屏优先判断（不是 App 界面）
    if looks_like_lockscreen(texts):
        return LoginStateResult(
            status="unknown",
            detail="疑似停在锁屏（仅识别到时钟/日期，无 App 文字）；请先解锁设备",
            evidence_ref=evidence_ref,
            ocr_texts=texts,
        )

    # 黑名单 1: 登录页
    for marker in LOGIN_MARKERS:
        if marker in joined:
            return LoginStateResult(
                status="needs_login",
                detail=f"login marker present: {marker}",
                evidence_ref=evidence_ref,
                ocr_texts=texts,
            )

    # 黑名单 2: 在线客服页（需要点返回判断真实登录态）
    for marker in CUSTOMER_SERVICE_MARKERS:
        if marker in joined:
            return LoginStateResult(
                status="customer_service_probe",
                detail=f"customer service page detected: {marker}; need to go back and re-probe",
                evidence_ref=evidence_ref,
                ocr_texts=texts,
            )

    # 默认：已登录
    return LoginStateResult(
        status="persisted",
        detail="default to logged-in (not on login/customer-service page)",
        evidence_ref=evidence_ref,
        ocr_texts=texts,
    )


def probe_login_state(
    *,
    live: bool = False,
    sock_path: str | None = None,
    out_dir: Path | None = None,
) -> LoginStateResult:
    if not live:
        return LoginStateResult(
            status="unknown",
            detail="M0b live probe not requested; do not assume persisted login",
        )

    from autophone.device.uds import request
    from autophone.device.ocr_target import run_ocr
    import time

    sock = sock_path or resolve_sock()
    out = out_dir or (STATE_ROOT / "runs" / "phase5" / "login-state-probe")
    out.mkdir(parents=True, exist_ok=True)

    # 第一次截图+OCR
    obj, _ = request({"t": "screenshot"}, sock_path=sock, timeout=5)
    img = obj.get("image")
    if not img:
        return LoginStateResult(
            status="unknown",
            detail=f"screenshot returned no image: {obj.get('error')}",
        )
    shot = out / "login_state_probe.jpg"
    shot.write_bytes(base64.b64decode(img))
    ocr = run_ocr(shot)
    texts = [str(box.get("text") or "") for box in ocr.get("boxes", []) if box.get("text")]
    result = _classify_texts(texts, evidence_ref=str(shot))

    # 如果是客服页，点返回再次检测
    if result.status == "customer_service_probe":
        # 点击返回按钮（左上角返回箭头，preview坐标）
        from autophone.device.uds import preview_to_native
        back_x, back_y = preview_to_native(24, 105)
        request({"t": "tap", "x": back_x, "y": back_y}, sock_path=sock, timeout=5)
        time.sleep(1.0)  # 等待页面切换

        # 第二次截图+OCR
        obj2, _ = request({"t": "screenshot"}, sock_path=sock, timeout=5)
        img2 = obj2.get("image")
        if not img2:
            return LoginStateResult(
                status="unknown",
                detail="customer service page: failed to screenshot after go back",
            )
        shot2 = out / "login_state_probe_after_back.jpg"
        shot2.write_bytes(base64.b64decode(img2))
        ocr2 = run_ocr(shot2)
        texts2 = [str(box.get("text") or "") for box in ocr2.get("boxes", []) if box.get("text")]
        result = _classify_texts(texts2, evidence_ref=str(shot2))

        # 如果返回后是登录页，说明未登录
        if result.status == "needs_login":
            result.detail = f"customer service → back → login page; {result.detail}"
        # 如果返回后是其它页，说明已登录
        elif result.status == "persisted":
            result.detail = f"customer service → back → business page; logged in"

    (out / "login_state_result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def ensure_login_state(*, live: bool = False, out_dir: Path | None = None) -> LoginStateResult:
    """Probe current state; login automation remains a separate adapter.

    Existing ddddocr login remains outside this module until the persisted-login
    spike proves when it is actually needed.
    """
    return probe_login_state(live=live, out_dir=out_dir)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Phase 5 login-state probe")
    ap.add_argument("--live", action="store_true", help="take a live screenshot and OCR it")
    ap.add_argument("--sock", default=None, help="vphone.sock path")
    ap.add_argument(
        "--out",
        type=Path,
        default=STATE_ROOT / "runs" / "phase5" / "login-state-probe",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = probe_login_state(live=args.live, sock_path=args.sock, out_dir=args.out)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status in {"persisted", "needs_login"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
