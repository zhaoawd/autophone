"""Phase 5 fixture contract primitives.

The contract is deliberately small and JSON/YAML-serializable. It is the
boundary between mined cases and the adapters that create real test
state. Adapters may fail, but they should fail with a FixtureContext instead of
letting later lifecycle runners start from an unproven world.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from autophone.observation.frida.governance import SENSITIVE_KEYS as _GOVERNANCE_SENSITIVE_KEYS
from autophone.observation.frida.governance import mask_phone

FIXTURE_SCHEMA = "autophone.fixture/1"
FIXTURE_CONTEXT_SCHEMA = "autophone.fixture-context/1"

ORDER_TYPES = {"resident_normal", "resident_enterprise", "district"}
DISTANCE_BANDS = {"under_200m", "over_200m", "unknown"}
ACTOR_STATES = {"on_duty", "off_duty", "break", "unknown"}
AUTH_STATES = {"logged_in", "logged_out", "unknown"}

# —— 演示环境默认绑定（fixture 侧单一出处，勿在别处再写字面量）——
# 这些值是占位示例，不对应任何真实商户或账号。接入真实测试环境时，由 App 插件或
# fixture spec 显式提供商户与账号，不要改成真实数据后入库。
DEFAULT_BRAND = "DEMO"
DEFAULT_STORE_CODE = "DEMO001"
DEFAULT_SHOP_ID = 1
DEFAULT_ACTOR_PHONE = "10000000000"

# 子串匹配的键名模式；精确键名清单复用 phase4 governance（password/mobile/lat/lng 等），
# 避免两份敏感键表分歧——phase4 抓包侧打码了的字段，phase5 evidence 不能再明文落盘。
SENSITIVE_KEY_PATTERNS = (
    "authorization",
    "auth",
    "cookie",
    "phone",
    "secret",
    "signature",
    "sign",
    "token",
    "password",
    "idcard",
    "address",
)
SENSITIVE_KEYS_EXACT = frozenset(_GOVERNANCE_SENSITIVE_KEYS)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<prefix>^|[?&;])(?P<key>[^=?&;\s]+)(?P<separator>=)(?P<value>[^&;]*)",
    flags=re.IGNORECASE,
)


def _redact_sensitive_assignments(text: str) -> str:
    """Redact form/query-style secrets embedded in otherwise opaque text."""

    def replace(match: re.Match[str]) -> str:
        key = match.group("key")
        lowered = key.lower()
        value = match.group("value")
        if lowered in SENSITIVE_KEYS_EXACT or any(
            pattern in lowered for pattern in SENSITIVE_KEY_PATTERNS
        ):
            value = "<redacted>"
        return (
            match.group("prefix") + key + match.group("separator") + value
        )

    return _SENSITIVE_ASSIGNMENT.sub(replace, text)


class FixtureContractError(ValueError):
    """Raised when a fixture spec cannot be trusted enough to run."""


@dataclass
class ActorSpec:
    phone: str
    desired_state: str = "on_duty"
    auth_state: str = "logged_in"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActorSpec":
        return cls(
            phone=str(data.get("phone") or ""),
            desired_state=str(data.get("desired_state") or "on_duty"),
            auth_state=str(data.get("auth_state") or "logged_in"),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.phone:
            errors.append("actor.phone is required")
        if self.desired_state not in ACTOR_STATES:
            errors.append(f"actor.desired_state must be one of {sorted(ACTOR_STATES)}")
        if self.auth_state not in AUTH_STATES:
            errors.append(f"actor.auth_state must be one of {sorted(AUTH_STATES)}")
        return errors


@dataclass
class StoreSpec:
    brand: str = DEFAULT_BRAND
    store_code: str = DEFAULT_STORE_CODE
    shop_id: int | None = DEFAULT_SHOP_ID
    store_latitude: str | None = None
    store_longitude: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoreSpec":
        shop_id = data.get("shop_id", DEFAULT_SHOP_ID)
        return cls(
            brand=str(data.get("brand") or DEFAULT_BRAND),
            store_code=str(data.get("store_code") or DEFAULT_STORE_CODE),
            shop_id=int(shop_id) if shop_id not in (None, "") else None,
            store_latitude=(
                str(data["store_latitude"])
                if data.get("store_latitude") not in (None, "") else None
            ),
            store_longitude=(
                str(data["store_longitude"])
                if data.get("store_longitude") not in (None, "") else None
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.brand:
            errors.append("store.brand is required")
        if not self.store_code:
            errors.append("store.store_code is required")
        if (self.store_latitude is None) != (self.store_longitude is None):
            errors.append("store.store_latitude and store_longitude must be set together")
        for name, value, limit in (
            ("store_latitude", self.store_latitude, 90),
            ("store_longitude", self.store_longitude, 180),
        ):
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"store.{name} must be a number")
                continue
            if not -limit <= number <= limit:
                errors.append(f"store.{name} must be between {-limit} and {limit}")
        return errors


@dataclass
class OrderSpec:
    type: str = "resident_normal"
    lifecycle_state: str = "waiting_pickup"
    distance_band: str = "unknown"
    actor_position_profile: str | None = None
    booking: int = 0
    customer_latitude: str | None = None
    customer_longitude: str | None = None
    promise_time: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderSpec":
        return cls(
            type=str(data.get("type") or "resident_normal"),
            lifecycle_state=str(data.get("lifecycle_state") or "waiting_pickup"),
            distance_band=str(data.get("distance_band") or "unknown"),
            actor_position_profile=data.get("actor_position_profile"),
            booking=int(data.get("booking", 0)),
            customer_latitude=(
                str(data["customer_latitude"])
                if data.get("customer_latitude") not in (None, "") else None
            ),
            customer_longitude=(
                str(data["customer_longitude"])
                if data.get("customer_longitude") not in (None, "") else None
            ),
            promise_time=(
                int(data["promise_time"])
                if data.get("promise_time") not in (None, "") else None
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.type not in ORDER_TYPES:
            errors.append(f"order.type must be one of {sorted(ORDER_TYPES)}")
        if self.distance_band not in DISTANCE_BANDS:
            errors.append(f"order.distance_band must be one of {sorted(DISTANCE_BANDS)}")
        if self.distance_band != "unknown" and not self.actor_position_profile:
            errors.append("order.actor_position_profile is required when distance_band is not unknown")
        if self.distance_band == "unknown" and self.actor_position_profile:
            errors.append("order.actor_position_profile must be unset when distance_band is unknown")
        if self.booking not in {0, 1}:
            errors.append("order.booking must be 0 or 1")
        if (self.customer_latitude is None) != (self.customer_longitude is None):
            errors.append("order.customer_latitude and customer_longitude must be set together")
        for name, value, limit in (
            ("customer_latitude", self.customer_latitude, 90),
            ("customer_longitude", self.customer_longitude, 180),
        ):
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"order.{name} must be a number")
                continue
            if not -limit <= number <= limit:
                errors.append(f"order.{name} must be between {-limit} and {limit}")
        if self.promise_time is not None and self.promise_time < 1_000_000_000_000:
            errors.append("order.promise_time must be a Unix timestamp in milliseconds")
        return errors


@dataclass
class TogglesSpec:
    realname_required: bool = False
    health_report_required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TogglesSpec":
        data = data or {}
        return cls(
            realname_required=bool(data.get("realname_required", False)),
            health_report_required=bool(data.get("health_report_required", False)),
        )


@dataclass
class ReadbackSpec:
    required: list[str] = field(default_factory=lambda: [
        "order_visible_in_app",
        "api_capture_enabled",
    ])

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ReadbackSpec":
        data = data or {}
        required = data.get("required")
        if required is None:
            return cls()
        return cls(required=[str(x) for x in required])


@dataclass
class FixtureSpec:
    case_id: str
    fixture_id: str
    actor: ActorSpec
    store: StoreSpec = field(default_factory=StoreSpec)
    order: OrderSpec = field(default_factory=OrderSpec)
    toggles: TogglesSpec = field(default_factory=TogglesSpec)
    readback: ReadbackSpec = field(default_factory=ReadbackSpec)
    schema: str = FIXTURE_SCHEMA
    env: str = "SIT"
    app: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FixtureSpec":
        return cls(
            schema=str(data.get("schema") or FIXTURE_SCHEMA),
            case_id=str(data.get("case_id") or ""),
            fixture_id=str(data.get("fixture_id") or ""),
            env=str(data.get("env") or "SIT"),
            app=str(data.get("app") or "").strip(),
            actor=ActorSpec.from_dict(data.get("actor") or {}),
            store=StoreSpec.from_dict(data.get("store") or {}),
            order=OrderSpec.from_dict(data.get("order") or {}),
            toggles=TogglesSpec.from_dict(data.get("toggles")),
            readback=ReadbackSpec.from_dict(data.get("readback")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        errors: list[str] = []
        if self.schema != FIXTURE_SCHEMA:
            errors.append(f"schema must be {FIXTURE_SCHEMA}")
        if not self.case_id:
            errors.append("case_id is required")
        if not self.fixture_id:
            errors.append("fixture_id is required")
        if not self.app.strip():
            errors.append("app is required")
        errors.extend(self.actor.validate())
        errors.extend(self.store.validate())
        errors.extend(self.order.validate())
        if errors:
            raise FixtureContractError("; ".join(errors))


@dataclass
class EvidenceRef:
    type: str
    name: str
    status: str
    ref: str
    detail: str = ""


@dataclass
class FixtureContext:
    fixture_id: str
    case_id: str
    status: str
    env: str
    order: dict[str, Any]
    actor: dict[str, Any]
    acquire: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    schema: str = FIXTURE_CONTEXT_SCHEMA
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FixtureContext":
        evidence = [
            EvidenceRef(
                type=str(item.get("type") or ""),
                name=str(item.get("name") or ""),
                status=str(item.get("status") or ""),
                ref=str(item.get("ref") or ""),
                detail=str(item.get("detail") or ""),
            )
            for item in data.get("evidence", []) or []
            if isinstance(item, dict)
        ]
        return cls(
            schema=str(data.get("schema") or FIXTURE_CONTEXT_SCHEMA),
            fixture_id=str(data.get("fixture_id") or ""),
            case_id=str(data.get("case_id") or ""),
            status=str(data.get("status") or ""),
            env=str(data.get("env") or ""),
            order=dict(data.get("order") or {}),
            actor=dict(data.get("actor") or {}),
            acquire=dict(data.get("acquire") or {}),
            evidence=evidence,
            gaps=[str(x) for x in data.get("gaps", []) or []],
            created_at=str(data.get("created_at") or datetime.now().astimezone().isoformat(timespec="seconds")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [asdict(e) for e in self.evidence]
        return data

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def default_fixture_spec(
    *,
    app: str,
    case_id: str,
    fixture_id: str | None = None,
    order_type: str = "resident_normal",
    lifecycle_state: str = "waiting_pickup",
    distance_band: str = "unknown",
    booking: int = 0,
    customer_latitude: str | None = None,
    customer_longitude: str | None = None,
    promise_time: int | None = None,
) -> FixtureSpec:
    """Return a demo fixture spec that is safe for M1 dry-run by default."""
    fid = fixture_id or f"{case_id}_{order_type}_{lifecycle_state}_{distance_band}"
    return FixtureSpec(
        case_id=case_id,
        fixture_id=fid,
        app=app,
        actor=ActorSpec(phone=DEFAULT_ACTOR_PHONE),
        order=OrderSpec(
            type=order_type,
            lifecycle_state=lifecycle_state,
            distance_band=distance_band,
            actor_position_profile=None if distance_band == "unknown" else f"demo_{distance_band}",
            booking=booking,
            customer_latitude=customer_latitude,
            customer_longitude=customer_longitude,
            promise_time=promise_time,
        ),
    )


def load_fixture_spec(path: Path) -> FixtureSpec:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise FixtureContractError(
                "YAML fixture specs require PyYAML; use JSON or run `uv sync`"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise FixtureContractError("fixture spec root must be an object")
    spec = FixtureSpec.from_dict(data)
    spec.validate()
    return spec


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def redact_sensitive(value: Any) -> Any:
    """Return a display-safe copy for console/HTML summaries.

    Raw run artifacts may still contain full local payloads for debugging. This
    helper is for summaries that could be pasted into reports.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lk = str(key).lower()
            if lk in SENSITIVE_KEYS_EXACT or any(
                pattern in lk for pattern in SENSITIVE_KEY_PATTERNS
            ):
                out[key] = "<redacted>"
            else:
                out[key] = redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return redact_sensitive(parsed)
        # governance.mask_phone：手机号保留前3后4，顺带覆盖身份证号
        return _redact_sensitive_assignments(mask_phone(value))
    return value


def load_fixture_context(path: Path) -> FixtureContext:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fixture context root must be an object")
    return FixtureContext.from_dict(data)
