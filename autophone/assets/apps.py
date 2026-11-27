from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from autophone.paths import APP_ASSET_ROOT, STATE_ROOT, refuse_code_root_app_assets
from autophone.assets.policy import assert_assets_writable
from autophone.contracts import validate_or_raise

#: App 资产根（包含 `apps/<app>/` 的目录）。默认等于 `CODE_ROOT`；
#: `AUTOPHONE_APP_ASSET_DIR` 开启时整体指向另一个目录，见 `autophone.paths`。
ROOT = APP_ASSET_ROOT

# app 标识符同时是目录名和 URL path 段（/api/apps/{app}/...），约束成保守 slug
# 挡住空格/斜杠/大写等会污染路径或路由的字符。
_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

AxBackend = Literal["frida_inproc", "guest_axruntime"]
TrialDataIsolation = Literal["unisolated", "isolated"]

DEFAULT_AX_BACKEND: AxBackend = "guest_axruntime"
DEFAULT_TRIAL_DATA_ISOLATION: TrialDataIsolation = "unisolated"
VALID_AX_BACKENDS: frozenset[str] = frozenset({"frida_inproc", "guest_axruntime"})
VALID_TRIAL_DATA_ISOLATIONS: frozenset[str] = frozenset({"unisolated", "isolated"})


@dataclass(frozen=True)
class AxCapability:
    """Typed view of ``capabilities.ax`` from ``app-profile.v1.json``."""

    enabled: bool = False
    backend: AxBackend = DEFAULT_AX_BACKEND


def _dedupe_slug(slug: str, *, root: Path) -> str:
    existing = {p.app for p in list_app_profiles(root=root)}
    if slug not in existing:
        return slug
    for n in range(2, 100):
        candidate = f"{slug}-{n}"
        if candidate not in existing:
            return candidate
    return slug


def suggest_app_slug(bundle_id: str | None, *, root: Path = ROOT) -> str:
    """Best-effort app slug from a bundle id's last segment, sanitized to match
    _APP_SLUG and de-duplicated against existing apps (demo, demo-2, ...).

    Returns "" when nothing usable can be derived (caller asks the user to type
    one). The slug is only a default — the onboarding form lets the user edit it.
    """
    tail = (bundle_id or "").rsplit(".", 1)[-1].strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", tail).strip("-")
    cleaned = re.sub(r"^[^a-z0-9]+", "", cleaned)
    if not cleaned:
        return ""
    return _dedupe_slug(cleaned, root=root)


def suggest_profile_slug(
    product_key: str | None,
    env: str | None,
    *,
    root: Path = ROOT,
) -> str:
    """Generate a new Profile id using the canonical ``product-env`` join.

    Existing App ids are only checked for collisions; they are never rewritten.
    """
    product = re.sub(r"[^a-z0-9_-]+", "-", str(product_key or "").strip().lower()).strip("-")
    environment = re.sub(r"[^a-z0-9_-]+", "-", str(env or "").strip().lower()).strip("-")
    base = "-".join(part for part in (product, environment) if part)
    base = re.sub(r"^[^a-z0-9]+", "", base)
    if not base:
        return ""
    return _dedupe_slug(base, root=root)


@dataclass(frozen=True)
class AppProfile:
    raw: dict[str, Any]
    root: Path
    state_root: Path | None = None

    @property
    def host_state_root(self) -> Path:
        return self.state_root or self.root

    @property
    def app(self) -> str:
        return str(self.raw["app"])

    @property
    def bundle_id(self) -> str:
        return str(self.raw["bundle_id"])

    @property
    def display_name(self) -> str:
        return str(self.raw["display_name"])

    @property
    def product_key(self) -> str:
        """Stable product family key; legacy profiles remain their own family."""
        return str(self.raw.get("product_key") or self.app)

    @property
    def env(self) -> str | None:
        value = self.raw.get("env")
        return str(value) if value is not None else None

    @property
    def lifecycle(self) -> str:
        """App Profile lifecycle. Missing is backward-compatible active."""
        return str(self.raw.get("lifecycle") or "active")

    @property
    def reset(self) -> str | None:
        """Per-case/run reset policy declared in ``profile.yaml`` (``ui_home|relaunch|none``)."""
        value = self.raw.get("reset")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @property
    def build_label(self) -> str | None:
        """当前在测 build 标识（区分力证据 staleness 比对基准）。缺省 None=不做比对。"""
        value = self.raw.get("build_label")
        return str(value) if value is not None else None

    @property
    def artifact_store(self) -> dict[str, Any]:
        """Optional byte store owned by the product project."""
        value = self.raw.get("artifact_store")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def require_discrimination(self) -> bool:
        """转正是否默认强制区分力闸（ADR-0001）。缺省 True（default-on，可在 profile 设 false 退回）。"""
        return bool(self.raw.get("require_discrimination", True))

    @property
    def allowed_effects(self) -> list[str]:
        """Irreversible/outbound effect verbs explicitly allowed for this app.

        Missing means default-deny for the app-aware effect gate. The first
        migration phase keeps enforcement opt-in/report-only so existing assets
        can be audited before this list is populated.
        """
        raw = self.raw.get("allowed_effects") or []
        return [str(item) for item in raw]

    @property
    def trial_data_isolation(self) -> TrialDataIsolation:
        """Draft trial data-isolation posture (D7); defaults to ``unisolated``."""
        value = self.raw.get("trial_data_isolation")
        if value is None:
            return DEFAULT_TRIAL_DATA_ISOLATION
        if not isinstance(value, str) or value not in VALID_TRIAL_DATA_ISOLATIONS:
            raise ValueError(
                f"invalid trial_data_isolation {value!r}: expected unisolated|isolated"
            )
        return cast(TrialDataIsolation, value)

    @property
    def ax_capability(self) -> AxCapability:
        """Typed ``capabilities.ax`` configuration; defaults to disabled + ``guest_axruntime``."""
        caps = self.raw.get("capabilities")
        ax_raw = caps.get("ax") if isinstance(caps, dict) else None
        if not isinstance(ax_raw, dict):
            return AxCapability()
        raw_backend = ax_raw.get("backend")
        if raw_backend is None:
            backend: AxBackend = DEFAULT_AX_BACKEND
        else:
            if not isinstance(raw_backend, str) or raw_backend not in VALID_AX_BACKENDS:
                raise ValueError(
                    f"invalid capabilities.ax.backend {raw_backend!r}: "
                    "expected frida_inproc|guest_axruntime"
                )
            backend = cast(AxBackend, raw_backend)
        return AxCapability(enabled=bool(ax_raw.get("enabled", False)), backend=backend)

    @property
    def ax_enabled(self) -> bool:
        """Whether ``capabilities.ax.enabled`` is enabled on this profile."""
        return self.ax_capability.enabled

    @property
    def ax_backend(self) -> AxBackend:
        """Statically declared AX observation backend (D8); defaults to ``guest_axruntime``."""
        return self.ax_capability.backend

    @property
    def store_catalog(self) -> dict[str, str]:
        """Store data binding for this app; missing means store-agnostic."""
        raw = self.raw.get("store_catalog")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if value not in (None, "")
        }

    @property
    def location(self) -> dict[str, Any]:
        """App-specific location target declaration; missing means unsupported."""
        value = self.raw.get("location")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def core_location_spawn(self) -> dict[str, Any]:
        """Explicit first-read CoreLocation spawn-gate declaration."""
        value = self.location.get("core_location_spawn")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def store_brand(self) -> str | None:
        return self.store_catalog.get("brand")

    @property
    def default_store_code(self) -> str | None:
        return self.store_catalog.get("default_store_code")

    @property
    def requirements(self) -> dict[str, str]:
        """Optional profile-wide capability floor; recipe declarations may only raise it."""
        raw = self.raw.get("requirements") or {}
        return {str(capability): str(mode) for capability, mode in raw.items()} if isinstance(raw, dict) else {}

    @property
    def im(self) -> dict[str, Any]:
        """IM 域配置段（B1 硬前提）。缺省空 dict → 零会话白名单 → 一切发送 abort。"""
        value = self.raw.get("im")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def im_allowed_conversations(self) -> list[dict[str, Any]]:
        """发送允许的测试会话白名单条目（含 chat_id + 可选 display_name）。

        缺省空 → IM 发送闸对该 app 一律拒（fail-closed，红线『错误接收人率=0』）。
        """
        raw = self.im.get("allowed_conversations") or []
        return [dict(item) for item in raw if isinstance(item, dict)]

    @property
    def profile_dir(self) -> Path:
        return self.root / "apps" / self.app

    def cases_path(self, filename: str = "cases.yaml") -> Path:
        raw_dir = self.raw.get("cases_dir") or "cases"
        return self.profile_dir / str(raw_dir) / filename

    def scripts_dir(self) -> Path:
        raw_dir = self.raw.get("scripts_dir") or "scripts"
        return self.profile_dir / str(raw_dir)

    def operations_path(self, filename: str = "operations.yaml") -> Path:
        """Return this App's operation registry path."""
        raw_dir = self.raw.get("operations_dir") or "operations"
        return self.profile_dir / str(raw_dir) / filename

    def screens_path(self, build: str | None = None) -> Path:
        """Screen skeleton asset path for the given build label."""
        raw_dir = self.raw.get("screens_dir") or "screens"
        build_label = build or self.build_label
        if not build_label:
            raise ValueError(f"app={self.app} 缺少 build_label，无法定位 screens/<build>.yaml")
        return self.profile_dir / str(raw_dir) / f"{build_label}.yaml"

    def recipes_dir(self) -> Path:
        """该 app 的 recipe 目录（每条用例一个 `<case_id>.yaml`）。triage 派生靠它判
        ui_ready 用例名实是否相符（有声明无 recipe = misconfigured）。"""
        raw_dir = self.raw.get("recipes_dir") or "recipes"
        return self.profile_dir / str(raw_dir)

    def has_recipe(self, case_id: str) -> bool:
        return (self.recipes_dir() / f"{str(case_id).lower()}.yaml").exists()

    def dispositions_path(self) -> Path:
        """disposition 叠加层（ADR-0003 唯一落库的东西：人对 derived exception 的态度）。
        运行期改写——见 [[skip-worktree-live-registries]]。"""
        return self.host_state_root / "apps" / self.app / "dispositions.json"

    def discrimination_vocab_path(self) -> Path:
        """区分力维度词汇（ADR-0002 twin 归一化签名的真源）。缺省 None 视作该 app 未声明任何
        判据轴 → 不派生 twin exception。"""
        return self.profile_dir / "discrimination" / "dimensions.yaml"

    def fixtures_path(self) -> Path:
        """预置态供给登记（ADR-0004 抽取需求 demand-pull 的真源）：预置条件 token → 已供给该态
        的 operation 名。缺文件视作零登记 → 一切 needs_fixture 需求都浮现为抽取需求。"""
        return self.profile_dir / "discrimination" / "fixtures.yaml"

    def api_expectations_path(self) -> Path:
        """app 级 API 断言资产（§七）：meta + expectations[]，走 asset_store 生命周期。
        api_oracle 读它 × capture.jsonl 产三态。缺文件视作零断言（advisory 旁路）。"""
        return self.profile_dir / "api_expectations.yaml"

    def api_fixtures_path(self) -> Path:
        """API 断言的外部真值 fixture（§四）：CASE_ID → {var: value}（满减门槛/优惠金额等
        不在响应里的活动配置真值）。经通用扁平 fixture-context 装载器注入 ${var}。
        与 fixtures_path()（ADR-0004 token→operation）语义不同，故另立文件。"""
        return self.profile_dir / "api_fixtures.yaml"


def load_app_profile(
    app: str, *, root: Path = ROOT, state_root: Path | None = None,
) -> AppProfile:
    path = root / "apps" / app / "profile.yaml"
    refuse_code_root_app_assets(root, "load app profile", path=path)
    if not path.exists():
        raise FileNotFoundError(f"app profile not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate_or_raise("app-profile.v1.json", raw)
    resolved_state_root = state_root or (STATE_ROOT if root == ROOT else root)
    return AppProfile(raw=raw, root=root, state_root=resolved_state_root)


def set_app_lifecycle(
    app: str,
    lifecycle: str,
    *,
    root: Path = ROOT,
) -> AppProfile:
    """Archive or restore an App Profile without deleting any owned assets.

    The write is surgical and atomic: existing comments/order/security policy
    remain byte-for-byte unchanged except for the top-level lifecycle line.
    """
    refuse_code_root_app_assets(root, "update app profile lifecycle", path=root / "apps" / app / "profile.yaml")
    assert_assets_writable("update app profile lifecycle", path=root / "apps" / app / "profile.yaml")
    if lifecycle not in {"active", "archived"}:
        raise ValueError("invalid app lifecycle: expected active|archived")
    profile = load_app_profile(app, root=root)
    path = profile.profile_dir / "profile.yaml"
    original_mode = path.stat().st_mode & 0o777
    source = path.read_text(encoding="utf-8")
    line = f"lifecycle: {lifecycle}"
    updated, count = re.subn(
        r"(?m)^lifecycle:[^\n]*(?:\n|$)",
        line + "\n",
        source,
        count=1,
    )
    if count == 0:
        anchor = re.search(r"(?m)^(?:env|display_name):[^\n]*(?:\n|$)", source)
        if anchor is None:
            raise ValueError(f"app profile {app!r} has no lifecycle insertion anchor")
        pos = anchor.end()
        updated = source[:pos] + line + "\n" + source[pos:]

    raw = yaml.safe_load(updated) or {}
    validate_or_raise("app-profile.v1.json", raw)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".profile-",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(updated)
            tmp_name = fh.name
        tmp_path = Path(tmp_name)
        tmp_path.chmod(original_mode)
        tmp_path.replace(path)
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
    return load_app_profile(app, root=root)


def default_app(*, root: Path = ROOT) -> str | None:
    """Derive the default app from the registry instead of hardcoding one.

    Only an unambiguous registry (exactly one installed profile) yields a
    default; with zero or multiple apps the caller must require an explicit
    choice (roadmap §五: the platform layer stays app-agnostic).
    """
    profiles = list_app_profiles(root=root)
    if len(profiles) == 1:
        return profiles[0].app
    return None


def create_app_profile(
    app: str,
    bundle_id: str,
    display_name: str,
    *,
    product_key: str | None = None,
    env: str | None = None,
    login_method: str = "manual",
    reset: str = "ui_home",
    root: Path = ROOT,
) -> AppProfile:
    """注册一个新被测 App：写 apps/<app>/profile.yaml + scaffold 子目录与空注册表。

    只固化身份字段（capabilities 留空 → 各观测面降级 not_configured，见 roadmap §五
    「缺什么降级什么」），资产到位后再手改 profile.yaml 补能力。

    fail-closed（任一不过抛 ValueError，不留半成品）：
      - app 必须是合法 slug（既当目录名又当路由段）；
      - apps/<app>/ 已存在则拒绝（不覆盖既有 App）；
      - bundle_id / display_name 非空。
    落盘前先 validate_or_raise(app-profile.v1.json)，与 load_app_profile 同一把尺子兜底。
    """
    refuse_code_root_app_assets(root, "create app profile", path=root / "apps" / str(app or ""))
    assert_assets_writable("create app profile", path=root / "apps" / str(app or ""))
    app = (app or "").strip()
    product_key = (product_key or app).strip()
    bundle_id = (bundle_id or "").strip()
    display_name = (display_name or "").strip()
    if not _APP_SLUG.match(app):
        raise ValueError(
            f"invalid app id {app!r}: must match {_APP_SLUG.pattern} "
            "(小写字母/数字开头，仅含小写字母/数字/下划线/连字符)"
        )
    if not _APP_SLUG.match(product_key):
        raise ValueError(
            f"invalid product_key {product_key!r}: must match {_APP_SLUG.pattern}"
        )
    if not bundle_id:
        raise ValueError("bundle_id is required")
    if not display_name:
        raise ValueError("display_name is required")
    if login_method not in ("script", "manual"):
        raise ValueError(f"invalid login method {login_method!r}: expected script|manual")
    if reset not in ("ui_home", "relaunch", "none"):
        raise ValueError(f"invalid reset {reset!r}: expected ui_home|relaunch|none")

    app_dir = root / "apps" / app
    if app_dir.exists():
        raise ValueError(f"app already exists: {app_dir}")

    raw: dict[str, Any] = {
        "schema_version": "v1",
        "app": app,
        "product_key": product_key,
        "bundle_id": bundle_id,
        "display_name": display_name,
        "lifecycle": "active",
    }
    if env:
        raw["env"] = env
    raw["login"] = {"method": login_method}
    raw["reset"] = reset
    # 契约兜底：不让任何不合 schema 的 profile 落盘。
    validate_or_raise("app-profile.v1.json", raw)

    # scaffold：profile + 三个标准子目录 + 两个空注册表。
    # 空 cases 文件是必须的——load_cases 无存在性兜底，缺文件会让 cases tab 500。
    # cases 与 operations 均为 App 级通用资产，统一使用 cases.yaml 与 operations.yaml。
    app_dir.mkdir(parents=True)
    for sub in ("cases", "scripts", "operations", "screens"):
        (app_dir / sub).mkdir()
    (app_dir / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # meta 必须带 schema_version + total + next_seq：asset_store 读取时校验
    # schema_version、append 时定点 patch total/next_seq 行（缺行即 fail-loud）。
    # 缺了它们，新 app 一加用例/起草操作就报「不是 schema_version N」。用例库 v2、
    # 操作库 v1（与 cases_store/operations_store 的 AssetSpec 对齐）。
    (app_dir / "cases" / "cases.yaml").write_text(
        yaml.safe_dump(
            {"meta": {"title": f"{app} 用例库", "app": app, "schema_version": 2,
                      "total": 0, "next_seq": 1}, "cases": []},
            allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (app_dir / "operations" / "operations.yaml").write_text(
        yaml.safe_dump(
            {"meta": {"title": f"{app} 操作库", "app": app, "schema_version": 1,
                      "total": 0, "next_seq": 1}, "operations": []},
            allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 复读一遍，保证返回值与磁盘一致。
    return load_app_profile(app, root=root)


def list_app_profiles(
    *, root: Path = ROOT, state_root: Path | None = None,
) -> list[AppProfile]:
    apps_dir = root / "apps"
    refuse_code_root_app_assets(root, "list app profiles", path=apps_dir)
    out: list[AppProfile] = []
    if not apps_dir.exists():
        return out
    for path in sorted(apps_dir.glob("*/profile.yaml")):
        out.append(load_app_profile(path.parent.name, root=root, state_root=state_root))
    return out
