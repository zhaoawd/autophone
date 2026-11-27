"""Strict HTTP admission for dashboard AI scenes.

Pydantic models reject coercion and extra fields.  Admission also binds the
one-shot consent receipt and checks the selected provider's declared capability
before a job (and therefore cost/artifact) can be created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from autophone.application.ai import ProviderCapabilities, provider_capabilities
from autophone.util import now_iso


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class AiAskRequest(StrictRequest):
    prompt: StrictStr = Field(min_length=1, max_length=12_000)
    mode: Literal["text_only", "snapshot", "interactive"] = "text_only"
    data_consent: Literal["current_screen", "device_session"] | None = None
    provider: Literal["claude", "codex", "auto"] | None = None
    model: StrictStr | None = Field(default=None, max_length=200)
    app: StrictStr | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=100)

    @model_validator(mode="after")
    def validate_consent(self) -> "AiAskRequest":
        required = {
            "text_only": None,
            "snapshot": "current_screen",
            "interactive": "device_session",
        }[self.mode]
        if self.data_consent != required:
            if required is None:
                raise ValueError("text_only does not accept data_consent")
            raise ValueError(f"{self.mode} requires data_consent={required} for this request")
        return self


class AiAskApproveRequest(StrictRequest):
    plan_digest: StrictStr = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    data_consent: Literal["device_session"]


class AiDiagnoseRequest(StrictRequest):
    run_id: StrictStr = Field(min_length=1, max_length=200)
    provider: Literal["claude", "codex", "auto"] | None = None
    model: StrictStr | None = Field(default=None, max_length=200)
    allow_raw: StrictBool = False


class AiDraftOperationRequest(StrictRequest):
    app: StrictStr = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=100)
    requirement: StrictStr = Field(min_length=1, max_length=12_000)
    source_text: StrictStr | None = Field(default=None, max_length=100_000)
    provider: Literal["claude", "codex", "auto"] | None = None
    model: StrictStr | None = Field(default=None, max_length=200)


class AiConvertCaseRequest(StrictRequest):
    app: StrictStr = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=100)
    case_id: StrictStr = Field(min_length=1, max_length=100)
    provider: Literal["claude", "codex", "auto"] | None = None
    model: StrictStr | None = Field(default=None, max_length=200)
    note: StrictStr | None = Field(default=None, max_length=20_000)


class AiProbeRequest(StrictRequest):
    provider: Literal["claude", "codex", "auto"] | None = None


@dataclass(frozen=True)
class AcceptedAsk:
    request: AiAskRequest
    provider: str
    capabilities: ProviderCapabilities
    actor: str
    lane: Literal["inference", "device"]
    consent_receipt: dict[str, Any] | None
    timeout_s: float
    max_budget_usd: float | None


def admit_ask(value: AiAskRequest, *, actor: str) -> AcceptedAsk:
    provider, capabilities = provider_capabilities(value.provider)
    required = ["text"]
    if value.mode in {"snapshot", "interactive"}:
        required.append("images")
    if value.mode == "interactive":
        required.extend(("structured_output", "device_tools"))
    missing = [name for name in required if not getattr(capabilities, name)]
    if missing:
        raise ValueError(
            f"provider {provider} does not support {value.mode}: missing {', '.join(missing)}"
        )
    receipt = None
    if value.data_consent:
        receipt = {"type": value.data_consent, "confirmed_at": now_iso()}
    return AcceptedAsk(
        request=value,
        provider=provider,
        capabilities=capabilities,
        actor=actor,
        lane="device" if value.mode == "interactive" else "inference",
        consent_receipt=receipt,
        timeout_s=300.0,
        max_budget_usd=5.0 if capabilities.budget_limit else None,
    )
