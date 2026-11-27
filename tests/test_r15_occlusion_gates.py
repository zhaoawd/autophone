"""Round 1.5 occlusion hardening gates — Items 1, 2, 3 (consumption), and 4.

Covers:
- `screens.detect_blocking_overlay` pure helper (`ocr_text`, `text`, `ax_text` as
  `str` or `list[str]`, combined modalities, confidence threshold, and regression
  coverage for `recipe._overlay_detected` with `list[str]` `ax_text`).
- Item (1): Active foreground state gate (`application_state != 0` or
  `key_window_scene_activation_state != 0`) fails closed with `overlay_occluded`,
  0 UDS taps, and no `fallback_xy` degradation — even without `screen_id`.
- Item (2): Pre-tap `blocking_overlays` check (`ocr_text`/`text` before `ax.refresh()`
  and `ax_text` after `ax.refresh()`) fails closed with `overlay_occluded`, 0 UDS
  taps, and no `fallback_xy` degradation.
- Item (3): `hittable is False` occlusion gate fails closed with `locator_occluded`,
  0 UDS taps, and no `fallback_xy` degradation; legacy `hittable is None` stays
  compatible; 1 occluded + 1 hittable taps only the hittable node; 2 hittable
  nodes fail with `ambiguous_match` and 0 taps.
- Item (4): Whole-case `suppress_overlay_dismiss=True` for symbol-using recipes
  across `run_recipe`, `runner.run_case`, and `operation_run._exec_typed_ir`
  (including follow-up non-symbol `assert_present` failures), while non-symbol
  recipes and `_run_post_case_cleanup` retain overlay-dismiss recovery.
"""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.assets import app_symbols
from autophone.assets.apps import load_app_profile
from autophone.assets.screens import detect_blocking_overlay, load_screen_asset
from autophone.device.ax_target import AxDump, Node
from autophone.execution import effects
from autophone.execution import operation_run as op_run_mod
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.engine import runner as runner_mod
from autophone.execution.engine.actions import CaseContext, StepFailure

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "food-ordering"
SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
BUNDLE_ID = "com.autophone.foodorderingdemo.v2"


# ── Helpers & Fakes ───────────────────────────────────────────────────────────


def _ocr_frame(*texts: str, confidence: float = 0.99) -> dict[str, Any]:
    return {
        "image_w": 430,
        "image_h": 932,
        "boxes": [
            {
                "text": text,
                "confidence": confidence,
                "x": 100,
                "y": 200 + i * 40,
                "w": 160,
                "h": 30,
            }
            for i, text in enumerate(texts)
        ],
    }


def _node(
    *,
    identifier: str = "home.ai_prompt.preferences",
    role: str = "button",
    label: str = "调偏好",
    enabled: bool = True,
    value: Any = None,
    native_x: int = 360,
    native_y: int = 900,
    nx: float = 0.5,
    ny: float = 0.5,
    hittable: bool | None = None,
    occluded_reason: str = "",
) -> Node:
    node = Node(
        label=label,
        role=role,
        traits=[role],
        frame=(100.0, 280.0, 44.0, 44.0),
        enabled=enabled,
        value=value,
        identifier=identifier,
        path="0/1",
        native_x=native_x,
        native_y=native_y,
        nx=nx,
        ny=ny,
    )
    if hittable is not None:
        setattr(node, "hittable", hittable)
    if occluded_reason:
        setattr(node, "occluded_reason", occluded_reason)
    return node


def _dump(
    nodes: list[Node],
    *,
    ax_backend: str | None = "frida_inproc",
    application_state: int | None = None,
    key_window_scene_activation_state: int | None = None,
    key_window_is_app_window: bool | None = None,
) -> AxDump:
    dump = AxDump(
        app=BUNDLE_ID,
        scale=3.0,
        w=430.0,
        h=932.0,
        view_controller="HomeVC",
        nodes=nodes,
        ax_backend=ax_backend,
    )
    if application_state is not None:
        setattr(dump, "application_state", application_state)
    if key_window_scene_activation_state is not None:
        setattr(dump, "key_window_scene_activation_state", key_window_scene_activation_state)
    if key_window_is_app_window is not None:
        setattr(dump, "key_window_is_app_window", key_window_is_app_window)
    return dump


class _FakeAxProvider:
    def __init__(self, dumps: list[AxDump] | AxDump, *, ax_backend: str | None = "frida_inproc") -> None:
        self._dumps = list(dumps) if isinstance(dumps, list) else [dumps]
        self.ax_backend = ax_backend
        self.refresh_calls = 0

    def refresh(self) -> AxDump:
        idx = min(self.refresh_calls, len(self._dumps) - 1)
        self.refresh_calls += 1
        return self._dumps[idx]


def _screen_doc_with_overlays(blocking_overlays: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "meta": {"schema_version": 1, "app": "food_ordering", "build": "1.0.0-bld-14282f675363"},
        "screens": [
            {
                "id": "S01_home",
                "display_name": "首页",
                "status": "active",
                "must": [{"text": "今晚，好好吃一顿"}],
                "forbid": [{"text": "登录"}],
                "any_of": {"min_hits": 0, "tokens": []},
                "blocking_overlays": blocking_overlays,
            },
            {
                "id": "S02_preferences",
                "display_name": "饮食偏好",
                "status": "active",
                "must": [{"text": "饮食偏好"}],
                "forbid": [],
                "any_of": {"min_hits": 0, "tokens": []},
            },
        ],
    }


def _patch_ctx_io(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_sequence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Patch CaseContext UDS & OCR; return (tap_payloads, counters)."""
    taps: list[dict[str, Any]] = []
    counters = {"ocr_calls": 0}
    fake_jpeg = b"\xff\xd8\xff\xe0FAKEJPEG"
    b64_jpeg = base64.b64encode(fake_jpeg).decode("ascii")

    def fake_request(msg: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], Any]:
        if msg.get("t") == "screenshot":
            return {"ok": True, "image": b64_jpeg}, SimpleNamespace(image_w=None)
        if msg.get("t") == "tap":
            taps.append(dict(msg))
            return {"ok": True}, None
        return {"ok": True}, None

    def fake_ocr(self: CaseContext, *, force: bool = False) -> dict[str, Any]:
        idx = min(counters["ocr_calls"], len(ocr_sequence) - 1)
        counters["ocr_calls"] += 1
        frame = ocr_sequence[idx]
        self._ocr = frame
        self._dirty = False
        return frame

    monkeypatch.setattr(actions_mod, "request", fake_request)
    monkeypatch.setattr(actions_mod, "GeometryGuard", lambda: SimpleNamespace(check=lambda sample: None))
    monkeypatch.setattr(CaseContext, "_settle", lambda self, **_kw: None)
    monkeypatch.setattr(CaseContext, "ocr_now", fake_ocr)
    monkeypatch.setattr(actions_mod.time, "sleep", lambda _s: None)
    return taps, counters


def _expanded_symbol_step(
    *,
    screen_id: str = "S01_home",
    identifier: str = "home.ai_prompt.preferences",
) -> dict[str, Any]:
    return {
        "action": "tap_ax",
        "identifier": identifier,
        "role": "button",
        "enabled_only": True,
        "require_unique": True,
        "screen_id": screen_id,
        "label": "打开饮食偏好页",
        effects.SYMBOL_BINDING_STEP_KEY: {
            "symbol_id": "ctrl.home.preferences",
            "binding_sha256": "a" * 64,
            "artifact_content_digest": SHA_V1,
            "screen_id": screen_id,
            "effects": {"verbs": [], "completeness": "exhaustive"},
        },
        effects.SYMBOL_EFFECTS_STEP_KEY: {"verbs": [], "completeness": "exhaustive"},
    }


# ── Shared Helper: screens.detect_blocking_overlay & recipe._overlay_detected ─


def test_detect_blocking_overlay_supports_str_and_list_across_all_modalities(tmp_path: Path) -> None:
    dismiss = [{"action": "tap_text", "query": "关闭"}]
    screen_def = {
        "id": "S01_home",
        "blocking_overlays": [
            {"name": "ocr_str", "detect": {"ocr_text": "活动弹窗"}, "dismiss": dismiss},
            {"name": "ocr_list", "detect": {"ocr_text": ["限时福利", "立即领取"]}, "dismiss": dismiss},
            {"name": "text_list", "detect": {"text": ["版本更新", "稍后再说"]}, "dismiss": dismiss},
            {"name": "ax_str", "detect": {"ax_text": "promo.modal.title"}, "dismiss": dismiss},
            {"name": "ax_list", "detect": {"ax_text": ["系统提示", "知道了"]}, "dismiss": dismiss},
            {
                "name": "combined",
                "detect": {"ocr_text": "联合弹窗", "ax_text": ["combined.title", "combined.close"]},
                "dismiss": dismiss,
            },
        ],
    }

    # ocr_text str
    hit = detect_blocking_overlay(_ocr_frame("首页", "活动弹窗"), screen_def)
    assert hit is not None and hit["name"] == "ocr_str" and hit["dismiss"] == dismiss

    # ocr_text list requires ALL entries
    assert detect_blocking_overlay(_ocr_frame("首页", "限时福利"), screen_def) is None
    hit = detect_blocking_overlay(_ocr_frame("首页", "限时福利", "立即领取"), screen_def)
    assert hit is not None and hit["name"] == "ocr_list"

    # text list requires ALL entries
    assert detect_blocking_overlay(_ocr_frame("版本更新"), screen_def) is None
    hit = detect_blocking_overlay(_ocr_frame("版本更新", "稍后再说"), screen_def)
    assert hit is not None and hit["name"] == "text_list"

    # confidence threshold respected
    assert detect_blocking_overlay(_ocr_frame("活动弹窗", confidence=0.1), screen_def, min_confidence=0.3) is None

    # ax_text str (no OCR match required)
    ax_str_dump = _dump([_node(identifier="promo.modal.title", label="", enabled=False)])
    assert detect_blocking_overlay(_ocr_frame("首页"), screen_def, ax_dump=None) is None
    hit = detect_blocking_overlay(_ocr_frame("首页"), screen_def, ax_dump=ax_str_dump)
    assert hit is not None and hit["name"] == "ax_str"

    # ax_text list fixes the legacy str(ax_text) list-stringification bug
    ax_partial_dump = _dump([_node(label="系统提示", enabled=False)])
    assert detect_blocking_overlay(_ocr_frame("首页"), screen_def, ax_dump=ax_partial_dump) is None
    ax_full_dump = _dump([
        _node(label="系统提示", enabled=False),
        _node(label="知道了", enabled=True),
    ])
    hit = detect_blocking_overlay(_ocr_frame("首页"), screen_def, ax_dump=ax_full_dump)
    assert hit is not None and hit["name"] == "ax_list"

    # Combined ocr_text + ax_text requires BOTH
    ax_combined = _dump([
        _node(identifier="combined.title", label=""),
        _node(identifier="combined.close", label=""),
    ])
    assert detect_blocking_overlay(_ocr_frame("联合弹窗"), screen_def, ax_dump=None) is None
    assert detect_blocking_overlay(_ocr_frame("首页"), screen_def, ax_dump=ax_combined) is None
    hit = detect_blocking_overlay(_ocr_frame("联合弹窗"), screen_def, ax_dump=ax_combined)
    assert hit is not None and hit["name"] == "combined"

    # recipe._overlay_detected also handles list[str] ax_text via shared helper
    ctx = CaseContext(
        case_id="R_AX_LIST",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(ax_full_dump),
    )
    assert recipe_mod._overlay_detected(ctx, _ocr_frame("首页"), {"ax_text": ["系统提示", "知道了"]}) is True
    assert recipe_mod._overlay_detected(ctx, _ocr_frame("首页"), {"ax_text": ["系统提示", "不存在"]}) is False


# ── Item 1: Active Foreground State Gate ──────────────────────────────────────


@pytest.mark.parametrize(
    "app_state,scene_state",
    [
        (1, 0),    # UIApplicationStateInactive (Control Center / Notification Center / system sheet)
        (2, 0),    # UIApplicationStateBackground
        (0, 1),    # UISceneActivationStateForegroundInactive
        (0, -1),   # UISceneActivationStateUnattached
        (0, 2),    # UISceneActivationStateBackground
        (1, None), # app_state inactive, scene_state omitted
        (None, 1), # app_state omitted, scene_state inactive
    ],
)
def test_item1_inactive_app_or_scene_blocks_tap_ax_without_screen_id_or_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_state: int | None,
    scene_state: int | None,
) -> None:
    """Item 1: inactive application_state or key_window_scene_activation_state
    fails closed with overlay_occluded, 0 UDS taps, and NO fallback_xy — even when
    screen_id is omitted and require_unique=False."""
    taps, _counters = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])
    dump = _dump(
        [_node(label="调偏好", hittable=True)],
        application_state=app_state,
        key_window_scene_activation_state=scene_state,
    )
    ctx = CaseContext(
        case_id="R_ITEM1",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(dump),
    )

    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(
            query="调偏好",
            role="button",
            fallback_xy=(120, 240),
            require_unique=False,
        )
    assert exc_info.value.result.kind == "overlay_occluded"
    assert "application_state=" in exc_info.value.result.detail
    assert "key_window_scene_activation_state=" in exc_info.value.result.detail
    assert len(taps) == 0


@pytest.mark.parametrize(
    "app_state,scene_state",
    [
        (0, 0),       # Active foreground app & scene
        (None, None), # Legacy / synthetic dump without activation state fields
    ],
)
def test_item1_active_or_legacy_state_allows_tap_ax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_state: int | None,
    scene_state: int | None,
) -> None:
    taps, _counters = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])
    dump = _dump(
        [_node(label="调偏好", native_x=410, native_y=820)],
        application_state=app_state,
        key_window_scene_activation_state=scene_state,
    )
    ctx = CaseContext(
        case_id="R_ITEM1_OK",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(dump),
    )

    res = ctx.tap_ax(query="调偏好", role="button")
    assert res.ok is True
    assert taps == [{"t": "tap", "x": 410, "y": 820}]


# ── Item 2: Pre-Tap blocking_overlays Fail-Closed Check ───────────────────────


@pytest.mark.parametrize(
    "detect_spec,matching_ocr,clean_ocr",
    [
        ({"ocr_text": "活动弹窗"}, ("今晚，好好吃一顿", "活动弹窗"), ("今晚，好好吃一顿",)),
        (
            {"ocr_text": ["限时活动", "立即关闭"]},
            ("今晚，好好吃一顿", "限时活动", "立即关闭"),
            ("今晚，好好吃一顿", "限时活动"),
        ),
        ({"text": "通用遮罩"}, ("今晚，好好吃一顿", "通用遮罩"), ("今晚，好好吃一顿",)),
        (
            {"text": ["实名认证", "去认证"]},
            ("今晚，好好吃一顿", "实名认证", "去认证"),
            ("今晚，好好吃一顿", "实名认证"),
        ),
    ],
)
def test_item2_ocr_blocking_overlays_fail_closed_before_ax_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detect_spec: dict[str, Any],
    matching_ocr: tuple[str, ...],
    clean_ocr: tuple[str, ...],
) -> None:
    """Item 2 (OCR): blocking_overlays detected on the SAME screen_id OCR frame
    fail closed with overlay_occluded, 0 UDS taps, 0 AX refreshes, 1 OCR call,
    and no fallback_xy degradation; clean frames pass."""
    screen_doc = _screen_doc_with_overlays([
        {
            "name": "promo_popup",
            "detect": detect_spec,
            "dismiss": [{"action": "tap_text", "query": "关闭"}],
        }
    ])

    # Blocked frame -> fails BEFORE ax.refresh(), ignores fallback_xy.
    taps, counters = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame(*matching_ocr)])
    ax = _FakeAxProvider(_dump([_node(hittable=True)]))
    ctx = CaseContext(
        case_id="R_ITEM2_OCR",
        shot_dir=tmp_path / "shots_block",
        ax=ax,
        screen_asset_doc=screen_doc,
    )
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(
            query="调偏好",
            role="button",
            screen_id="S01_home",
            fallback_xy=(99, 199),
            require_unique=False,
        )
    assert exc_info.value.result.kind == "overlay_occluded"
    assert "promo_popup" in exc_info.value.result.detail
    assert "S01_home" in exc_info.value.result.detail
    assert len(taps) == 0
    assert ax.refresh_calls == 0
    assert counters["ocr_calls"] == 1

    # Clean frame -> passes and dispatches 1 tap.
    taps_ok, counters_ok = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame(*clean_ocr)])
    ax_ok = _FakeAxProvider(_dump([_node(hittable=True, native_x=360, native_y=900)]))
    ctx_ok = CaseContext(
        case_id="R_ITEM2_OCR_OK",
        shot_dir=tmp_path / "shots_ok",
        ax=ax_ok,
        screen_asset_doc=screen_doc,
    )
    res = ctx_ok.tap_ax(
        identifier="home.ai_prompt.preferences",
        role="button",
        screen_id="S01_home",
        require_unique=True,
    )
    assert res.ok is True
    assert taps_ok == [{"t": "tap", "x": 360, "y": 900}]
    assert counters_ok["ocr_calls"] == 1


@pytest.mark.parametrize(
    "ax_text_spec,blocking_nodes,clean_nodes",
    [
        (
            "promo.modal.banner",
            [_node(hittable=True), _node(identifier="promo.modal.banner", label="活动横幅", enabled=False)],
            [_node(hittable=True)],
        ),
        (
            ["活动标题", "关闭弹窗"],
            [
                _node(hittable=True),
                _node(identifier="modal.title", label="活动标题", enabled=False),
                _node(identifier="modal.close", label="关闭弹窗", enabled=True),
            ],
            [
                _node(hittable=True),
                _node(identifier="modal.title", label="活动标题", enabled=False),
            ],
        ),
    ],
)
def test_item2_ax_text_blocking_overlays_fail_closed_after_ax_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ax_text_spec: str | list[str],
    blocking_nodes: list[Node],
    clean_nodes: list[Node],
) -> None:
    """Item 2 (AX): ax_text blocking_overlays (str or list[str]) fail closed after
    ax.refresh() with overlay_occluded, 0 UDS taps, and no fallback_xy."""
    screen_doc = _screen_doc_with_overlays([
        {
            "name": "ax_promo_modal",
            "detect": {"ax_text": ax_text_spec},
            "dismiss": [{"action": "tap_text", "query": "关闭"}],
        }
    ])

    taps, counters = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])
    ax = _FakeAxProvider(_dump(blocking_nodes))
    ctx = CaseContext(
        case_id="R_ITEM2_AX",
        shot_dir=tmp_path / "shots_ax_block",
        ax=ax,
        screen_asset_doc=screen_doc,
    )
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(
            query="调偏好",
            role="button",
            screen_id="S01_home",
            fallback_xy=(88, 188),
            require_unique=False,
        )
    assert exc_info.value.result.kind == "overlay_occluded"
    assert "ax_promo_modal" in exc_info.value.result.detail
    assert len(taps) == 0
    assert ax.refresh_calls == 1
    assert counters["ocr_calls"] == 1

    # Partial / absent ax_text passes and taps.
    taps_ok, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])
    ctx_ok = CaseContext(
        case_id="R_ITEM2_AX_OK",
        shot_dir=tmp_path / "shots_ax_ok",
        ax=_FakeAxProvider(_dump(clean_nodes)),
        screen_asset_doc=screen_doc,
    )
    res = ctx_ok.tap_ax(
        identifier="home.ai_prompt.preferences",
        role="button",
        screen_id="S01_home",
        require_unique=True,
    )
    assert res.ok is True
    assert len(taps_ok) == 1


# ── Item 3: hittable == False Occlusion Gate ──────────────────────────────────


def test_item3_hittable_false_blocks_tap_ax_with_locator_occluded_and_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: all matched nodes having hittable=False fails closed with
    locator_occluded, 0 UDS taps, and NO fallback_xy degradation."""
    taps, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])

    # Single occluded node (even with fallback_xy and require_unique=False).
    occluded = _node(
        label="调偏好",
        hittable=False,
        occluded_reason="hit_test_occluded",
    )
    ctx = CaseContext(
        case_id="R_ITEM3_SINGLE",
        shot_dir=tmp_path / "shots_single",
        ax=_FakeAxProvider(_dump([occluded], application_state=0, key_window_scene_activation_state=0)),
    )
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(query="调偏好", role="button", fallback_xy=(100, 200), require_unique=False)
    assert exc_info.value.result.kind == "locator_occluded"
    assert "hit_test_occluded" in exc_info.value.result.detail
    assert len(taps) == 0

    # Two matched nodes both occluded -> still locator_occluded (not ambiguous_match), 0 taps.
    occ_a = _node(identifier="dup.btn", label="A", hittable=False, occluded_reason="modal_sibling")
    occ_b = _node(identifier="dup.btn", label="B", hittable=False, occluded_reason="higher_window")
    ctx_multi = CaseContext(
        case_id="R_ITEM3_MULTI_OCC",
        shot_dir=tmp_path / "shots_multi_occ",
        ax=_FakeAxProvider(_dump([occ_a, occ_b])),
    )
    with pytest.raises(StepFailure) as multi_exc:
        ctx_multi.tap_ax(identifier="dup.btn", role="button", require_unique=True, fallback_xy=(10, 20))
    assert multi_exc.value.result.kind == "locator_occluded"
    assert "modal_sibling" in multi_exc.value.result.detail
    assert "higher_window" in multi_exc.value.result.detail
    assert len(taps) == 0


def test_item3_hittable_legacy_none_mixed_disambiguation_and_two_hittable_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3:
    - hittable=None (v8 legacy) still taps;
    - 1 hittable=False + 1 hittable=True taps ONLY the hittable node;
    - 2 hittable=True raises ambiguous_match with 0 taps."""
    taps, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿")])

    # 1) Legacy v8 dump (hittable=None) -> taps cleanly.
    ctx_legacy = CaseContext(
        case_id="R_ITEM3_LEGACY",
        shot_dir=tmp_path / "shots_legacy",
        ax=_FakeAxProvider(_dump([_node(hittable=None, native_x=311, native_y=722)])),
    )
    res_legacy = ctx_legacy.tap_ax(
        identifier="home.ai_prompt.preferences",
        role="button",
        require_unique=True,
    )
    assert res_legacy.ok is True
    assert taps == [{"t": "tap", "x": 311, "y": 722}]

    # 2) 1 occluded underlying node + 1 visible modal node -> taps ONLY the hittable node.
    taps.clear()
    under = _node(
        identifier="shared.confirm",
        label="确认",
        native_x=111,
        native_y=222,
        hittable=False,
        occluded_reason="hit_test_occluded",
    )
    modal_btn = _node(
        identifier="shared.confirm",
        label="确认提交",
        native_x=555,
        native_y=666,
        hittable=True,
    )
    ctx_mixed = CaseContext(
        case_id="R_ITEM3_MIXED",
        shot_dir=tmp_path / "shots_mixed",
        ax=_FakeAxProvider(_dump([under, modal_btn])),
    )
    res_mixed = ctx_mixed.tap_ax(identifier="shared.confirm", role="button", require_unique=True)
    assert res_mixed.ok is True
    assert taps == [{"t": "tap", "x": 555, "y": 666}]

    # 3) 2 hittable nodes -> ambiguous_match with 0 taps.
    taps.clear()
    hit_a = _node(identifier="shared.confirm", label="确认A", native_x=100, native_y=200, hittable=True)
    hit_b = _node(identifier="shared.confirm", label="确认B", native_x=300, native_y=400, hittable=True)
    ctx_ambig = CaseContext(
        case_id="R_ITEM3_AMBIG",
        shot_dir=tmp_path / "shots_ambig",
        ax=_FakeAxProvider(_dump([hit_a, hit_b])),
    )
    with pytest.raises(StepFailure) as ambig_exc:
        ctx_ambig.tap_ax(identifier="shared.confirm", role="button", require_unique=True)
    assert ambig_exc.value.result.kind == "ambiguous_match"
    assert len(taps) == 0


# ── Item 4: Whole-Case suppress_overlay_dismiss in Strict Symbol Mode ─────────


def test_item4_run_recipe_suppresses_overlay_dismiss_on_followup_non_symbol_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 via run_recipe: symbol-using recipe suppresses _dismiss_blocking_overlay_once
    even when a subsequent NON-symbol assert_present fails with an overlay present,
    restores ctx.suppress_overlay_dismiss afterward, while a non-symbol recipe still
    auto-dismisses the overlay."""
    screen_doc = _screen_doc_with_overlays([
        {
            "name": "promo_popup",
            "detect": {"ocr_text": "活动弹窗"},
            "dismiss": [{"action": "tap_text", "query": "关闭"}],
        }
    ])
    clean_home = _ocr_frame("今晚，好好吃一顿", "调偏好")
    overlay_frame = _ocr_frame("今晚，好好吃一顿", "活动弹窗", "关闭")
    after_dismiss = _ocr_frame("饮食偏好", "保存偏好")

    # Direct defense-in-depth guard on _dismiss_blocking_overlay_once itself.
    ctx_guarded = CaseContext(
        case_id="R_GUARD",
        shot_dir=tmp_path / "shots_guard",
        screen_asset_doc=screen_doc,
        suppress_overlay_dismiss=True,
    )
    taps_guard, counters_guard = _patch_ctx_io(monkeypatch, ocr_sequence=[overlay_frame])
    assert recipe_mod._dismiss_blocking_overlay_once(ctx_guarded) is None
    assert counters_guard["ocr_calls"] == 0
    assert taps_guard == []

    # 1) Symbol-using recipe via run_recipe: step 1 succeeds, step 2 (non-symbol
    #    assert_present) fails while overlay is visible -> 0 dismiss calls, 0 dismiss taps!
    dismiss_spy = pytest.MonkeyPatch()
    dismiss_calls: list[str] = []
    orig_dismiss = recipe_mod._dismiss_blocking_overlay_once

    def spy_dismiss(ctx: CaseContext) -> str | None:
        dismiss_calls.append(ctx.case_id)
        return orig_dismiss(ctx)

    dismiss_spy.setattr(recipe_mod, "_dismiss_blocking_overlay_once", spy_dismiss)
    try:
        taps_sym, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[clean_home, overlay_frame])
        ctx_sym = CaseContext(
            case_id="R_SYM_RECIPE",
            shot_dir=tmp_path / "shots_sym",
            ax=_FakeAxProvider(_dump([_node(hittable=True)])),
            screen_asset_doc=screen_doc,
            allowed_effects=set(),
        )
        assert ctx_sym.suppress_overlay_dismiss is False
        sym_steps = [
            _expanded_symbol_step(),
            {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
        ]
        status, _detail, fail_kind = recipe_mod.run_recipe(sym_steps, ctx_sym)
        assert status == "fail"
        assert fail_kind == "assertion_failed"
        assert len(taps_sym) == 1  # only the symbol tap; 0 dismiss taps
        assert dismiss_calls == []
        # Previous flag value restored in finally.
        assert ctx_sym.suppress_overlay_dismiss is False

        # 2) Non-symbol recipe via run_recipe: assert_present fails under overlay ->
        #    _dismiss_blocking_overlay_once IS called, taps "关闭", retries, and passes!
        taps_plain, _ = _patch_ctx_io(
            monkeypatch,
            ocr_sequence=[
                overlay_frame,  # initial assert_present -> fails
                overlay_frame,  # _dismiss_blocking_overlay_once detects promo_popup
                overlay_frame,  # dismiss step tap_text("关闭") locates & taps "关闭"
                after_dismiss,  # retried assert_present -> passes
            ],
        )
        ctx_plain = CaseContext(
            case_id="R_PLAIN_RECIPE",
            shot_dir=tmp_path / "shots_plain",
            screen_asset_doc=screen_doc,
            allowed_effects=set(),
        )
        plain_steps = [
            {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
        ]
        status_p, detail_p, fail_kind_p = recipe_mod.run_recipe(plain_steps, ctx_plain)
        assert status_p == "pass", detail_p
        assert fail_kind_p == ""
        assert dismiss_calls == ["R_PLAIN_RECIPE"]
        assert len(taps_plain) == 1
        assert any(r.action == "overlay_dismissed" for r in ctx_plain.results)
    finally:
        dismiss_spy.undo()


def test_item4_runner_run_case_suppresses_overlay_dismiss_for_symbol_cases_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 via runner.run_case:
    - Symbol-using case suppresses overlay dismiss on follow-up non-symbol steps;
    - Non-symbol case still allows overlay dismiss recovery;
    - _run_post_case_cleanup leaves suppress_overlay_dismiss=False."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    clean_home = _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好")
    overlay_frame = _ocr_frame("今晚，好好吃一顿", "立即下单", "活动弹窗", "关闭")
    after_dismiss = _ocr_frame("饮食偏好", "保存偏好")

    dismiss_calls: list[str] = []
    orig_dismiss = recipe_mod._dismiss_blocking_overlay_once

    def spy_dismiss(ctx: CaseContext) -> str | None:
        dismiss_calls.append(ctx.case_id)
        return orig_dismiss(ctx)

    monkeypatch.setattr(recipe_mod, "_dismiss_blocking_overlay_once", spy_dismiss)

    # 1) Symbol-using case: step 1 succeeds, step 2 (non-symbol assert_present) fails
    #    with overlay present -> 0 dismiss calls, 0 dismiss taps.
    sym_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ]
    taps_sym, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[clean_home, overlay_frame])
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R_SYM": sym_steps}):
        out_sym = runner_mod.run_case(
            {"id": "R_SYM", "module": "首页", "item": "符号用例整例禁弹层自动关闭"},
            tmp_path / "run_sym",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_dump([_node(hittable=True)])),
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_sym.status == "fail"
    assert out_sym.fail_kind == "assertion_failed"
    assert len(taps_sym) == 1
    assert dismiss_calls == []

    # 2) Non-symbol case: overlay dismiss IS invoked and recovers the failing assert_present.
    plain_steps = [
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ]
    taps_plain, _ = _patch_ctx_io(
        monkeypatch,
        ocr_sequence=[overlay_frame, overlay_frame, overlay_frame, after_dismiss],
    )
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R_PLAIN": plain_steps}):
        out_plain = runner_mod.run_case(
            {"id": "R_PLAIN", "module": "首页", "item": "非符号用例保留弹层恢复"},
            tmp_path / "run_plain",
            str(tmp_path / "absent.sock"),
            30.0,
            screen_asset_doc=screen_doc,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_plain.status == "pass", out_plain.detail
    assert dismiss_calls == ["R_PLAIN"]
    assert len(taps_plain) == 1

    # 3) _run_post_case_cleanup leaves suppress_overlay_dismiss=False.
    observed_cleanup_flags: list[bool] = []

    def cleanup_op(ctx: CaseContext) -> None:
        observed_cleanup_flags.append(ctx.suppress_overlay_dismiss)

    monkeypatch.setitem(recipe_mod.OPERATIONS, "_r15_test_cleanup", cleanup_op)
    runner_mod._run_post_case_cleanup(
        {"id": "R_SYM", "post_case_cleanup": [{"op": "_r15_test_cleanup"}]},
        shot_dir=tmp_path / "cleanup_shots",
        sock_path=str(tmp_path / "absent.sock"),
        timeout=10.0,
        screen_asset_doc=screen_doc,
    )
    assert observed_cleanup_flags == [False]


def test_item4_operation_run_exec_typed_ir_suppresses_overlay_dismiss_for_symbol_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 via operation_run._exec_typed_ir:
    - Symbol-using typed-IR steps set suppress_overlay_dismiss=True (0 dismiss calls);
    - Non-symbol typed-IR steps retain overlay-dismiss recovery."""
    screen_doc = _screen_doc_with_overlays([
        {
            "name": "promo_popup",
            "detect": {"ocr_text": "活动弹窗"},
            "dismiss": [{"action": "tap_text", "query": "关闭"}],
        }
    ])
    screen_path = tmp_path / "screens.yaml"
    screen_path.write_text(yaml.safe_dump(screen_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    clean_home = _ocr_frame("今晚，好好吃一顿", "调偏好")
    overlay_frame = _ocr_frame("今晚，好好吃一顿", "活动弹窗", "关闭")
    after_dismiss = _ocr_frame("饮食偏好", "保存偏好")

    dismiss_calls: list[str] = []
    orig_dismiss = recipe_mod._dismiss_blocking_overlay_once

    def spy_dismiss(ctx: CaseContext) -> str | None:
        dismiss_calls.append(ctx.case_id)
        return orig_dismiss(ctx)

    monkeypatch.setattr(recipe_mod, "_dismiss_blocking_overlay_once", spy_dismiss)
    monkeypatch.setattr(op_run_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(CaseContext, "_grab_raw", lambda self: b"\xff\xd8\xff\xe0FAKE")

    # 1) Symbol-using steps -> follow-up non-symbol assert_present fails with 0 dismiss calls.
    taps_sym, _ = _patch_ctx_io(monkeypatch, ocr_sequence=[clean_home, overlay_frame])
    sym_steps = [
        _expanded_symbol_step(),
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ]
    res_sym = op_run_mod._exec_typed_ir(
        tmp_path / "op_sym",
        sym_steps,
        screen_asset_path=screen_path,
        checkpoint_ax=_FakeAxProvider(_dump([_node(hittable=True)])),
        allowed_effects=set(),
    )
    assert res_sym["status"] == "failed"
    assert len(taps_sym) == 1
    assert dismiss_calls == []

    # 2) Non-symbol steps -> overlay dismiss IS called and retries to pass.
    taps_plain, _ = _patch_ctx_io(
        monkeypatch,
        ocr_sequence=[overlay_frame, overlay_frame, overlay_frame, after_dismiss],
    )
    plain_steps = [
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ]
    res_plain = op_run_mod._exec_typed_ir(
        tmp_path / "op_plain",
        plain_steps,
        screen_asset_path=screen_path,
        allowed_effects=set(),
    )
    assert res_plain["status"] == "passed", res_plain.get("detail")
    assert dismiss_calls == ["operation"]
    assert len(taps_plain) == 1
