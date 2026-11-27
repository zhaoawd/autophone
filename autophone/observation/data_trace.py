"""用户行为 readback（data trace）的数据契约与可插拔查询客户端。

平台只定义查询请求、事件与批次的数据结构，以及错误分类。具体的后端协议（鉴权、
查询字段集、响应解析）由部署方提供的 provider 实现，平台不内置任何后端。

provider 通过环境变量 ``AUTOPHONE_DATA_TRACE_PROVIDER`` 指定，格式为
``package.module:factory``。``factory(config)`` 返回一个对象，至少实现
``query(DataTraceQuery) -> DataTraceBatch``；可选实现 ``health() -> None``，供
``autophone telemetry doctor --provider data_trace --probe-token`` 探测。

未配置 provider 时，构造 ``DataTraceClient`` 会抛 ``DataTraceConfigurationError``，
上层把它记为 telemetry gap，不影响 Run 裁决。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

PROVIDER_NAME = "data_trace"
COLUMN_SET_VERSION = "data-trace-v1"
MAX_QUERY_DAYS = 3
MAX_LIMIT = 10_000
DEFAULT_LIMIT = 10_000
DEFAULT_TIMEOUT_S = 10.0


class DataTraceError(RuntimeError):
    """data trace 失败基类；``kind`` 可直接映射 telemetry gap。"""

    kind = "data_trace_error"


class DataTraceConfigurationError(DataTraceError):
    kind = "configuration_error"


class DataTraceQueryError(DataTraceError):
    kind = "query_error"


class DataTraceAdapterError(DataTraceError):
    kind = "adapter_error"


class DataTraceBackendError(DataTraceError):
    kind = "backend_error"


class DataTraceContractError(DataTraceError):
    kind = "contract_error"


EndpointScope = Literal["dev", "prod"]


@dataclass(frozen=True)
class DataTraceConfig:
    endpoint_scope: EndpointScope
    provider: str
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def configured(self) -> bool:
        return bool(self.provider.strip())


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def config_from_env(endpoint: EndpointScope = "dev") -> DataTraceConfig:
    if endpoint not in ("dev", "prod"):
        raise DataTraceConfigurationError("data trace endpoint must be dev or prod")
    return DataTraceConfig(
        endpoint_scope=endpoint,
        provider=(os.environ.get("AUTOPHONE_DATA_TRACE_PROVIDER") or "").strip(),
        timeout_s=_float_env("AUTOPHONE_DATA_TRACE_TIMEOUT_S", DEFAULT_TIMEOUT_S),
    )


class FilterOperator(str, Enum):
    EQ = "eq"
    IN = "in"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    CONTAINS = "contains"


@dataclass(frozen=True)
class DataTraceFilter:
    field: str
    op: FilterOperator | str
    value: str | tuple[str, ...] | None = None
    json_field_key: str | None = None


@dataclass(frozen=True)
class DataTraceIdentity:
    field: Literal["phone_no", "user_code", "device_id", "session_id", "sid"]
    value: str


@dataclass(frozen=True)
class DataTraceQuery:
    platform_id: int
    brand_id: int
    channel: str
    started_at: datetime
    ended_at: datetime
    identity: DataTraceIdentity
    filters: tuple[DataTraceFilter, ...] = ()
    limit: int = DEFAULT_LIMIT


IdentityAssurance = Literal["condition_only", "response_confirmed", "response_conflict"]


@dataclass(frozen=True)
class BehaviorEvent:
    event_id: str
    event_id_source: Literal["kafka_position", "fallback_fingerprint"]
    identity_assurance: IdentityAssurance
    occurred_at: datetime | None
    ingested_at: datetime | None
    kind: str
    name: str
    category: str | None
    classify: str | None
    content: Any
    content_cache: Any
    i_ext: Any
    request_params: Any
    request_url: str | None
    request_status: str | None
    request_error: str | None
    page_name: str | None
    bundle_id: str | None
    app_version: str | None
    brand: str | None
    channel: str | None
    env: str | None
    device_type: str | None
    os_version: str | None
    city: str | None
    device_id: str | None
    user_code: str | None
    session_id: str | None
    sid: str | None
    kafka_partition: str | None
    kafka_offset: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class DataTraceBatch:
    events: tuple[BehaviorEvent, ...]
    raw_count: int
    malformed: int
    possibly_truncated: bool
    effective_limit: int
    queried_at: datetime
    query_fingerprint: str
    column_set_version: str = COLUMN_SET_VERSION


class DataTraceBackend(Protocol):
    def query(self, query: DataTraceQuery) -> DataTraceBatch: ...


def _load_backend(config: DataTraceConfig) -> DataTraceBackend:
    if not config.configured:
        raise DataTraceConfigurationError(
            "data trace provider missing: set AUTOPHONE_DATA_TRACE_PROVIDER=module:factory"
        )
    module_name, _, attr = config.provider.partition(":")
    if not module_name or not attr:
        raise DataTraceConfigurationError("AUTOPHONE_DATA_TRACE_PROVIDER must be module:factory")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise DataTraceConfigurationError(
            f"data trace provider cannot be loaded: {type(exc).__name__}"
        ) from exc
    backend = factory(config)
    if not callable(getattr(backend, "query", None)):
        raise DataTraceConfigurationError("data trace provider does not implement query()")
    return backend


class DataTraceClient:
    """data trace 查询入口；具体后端由 provider 实现。"""

    def __init__(self, config: DataTraceConfig, *, backend: DataTraceBackend | None = None) -> None:
        self._config = config
        self._backend = backend if backend is not None else _load_backend(config)

    def query(self, query: DataTraceQuery) -> DataTraceBatch:
        if query.started_at > query.ended_at:
            raise DataTraceQueryError("data trace window start must not be after end")
        if (query.ended_at - query.started_at).total_seconds() > MAX_QUERY_DAYS * 86_400:
            raise DataTraceQueryError(f"data trace window must not exceed {MAX_QUERY_DAYS} days")
        batch = self._backend.query(query)
        if not isinstance(batch, DataTraceBatch):
            raise DataTraceContractError("data trace provider must return DataTraceBatch")
        return batch

    def health(self) -> None:
        probe = getattr(self._backend, "health", None)
        if callable(probe):
            probe()


def data_trace_doctor(
    *,
    endpoint: EndpointScope = "dev",
    probe_token: bool = False,
    timeout_s: float | None = None,
    backend: DataTraceBackend | None = None,
) -> dict[str, Any]:
    """能力探测：默认只报配置，显式 probe 才调用 provider 的 health()。"""
    config = config_from_env(endpoint)
    if timeout_s is not None:
        config = replace(config, timeout_s=max(0.1, float(timeout_s)))
    configured = config.configured or backend is not None
    result: dict[str, Any] = {
        "schema_version": "v1",
        "provider": PROVIDER_NAME,
        "endpoint": endpoint,
        "configured": configured,
        "probe_token": probe_token,
        "probed": False,
        "status": "configured" if configured else "not_configured",
        "column_set_version": COLUMN_SET_VERSION,
    }
    if not configured or not probe_token:
        return result
    try:
        DataTraceClient(config, backend=backend).health()
    except (DataTraceError, OSError) as exc:
        result.update({"probed": True, "status": "probe_failed", "detail": type(exc).__name__})
        return result
    result.update({"probed": True, "status": "healthy"})
    return result
