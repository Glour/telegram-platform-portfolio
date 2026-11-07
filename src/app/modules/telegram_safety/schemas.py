from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class SafetyOperationResponse(BaseModel):
    id: str
    method: str
    outcome: str
    error_code: str | None = None
    target: str | None = None
    outbound: bool = False
    created_at: datetime
    finished_at: datetime | None = None
    message_id: str | None = None


class SafetyStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    execution_enabled: bool = False
    fingerprint: str | None = None
    manual_hold: bool = True
    reason: str | None = None
    received_at: datetime | None = None
    wait_seconds: int | None = None
    wait_until: datetime | None = None
    restriction_status: str = "unknown"
    restriction_checked_at: datetime | None = None
    operator_approved_at: datetime | None = None
    scope: str | None = None
    operation_id: str | None = None
    first_seen_at: datetime | None = None
    evidence: str | None = None
    actor_id: str | None = None
    outbound_attempt_limit: int = 1
    rpc_attempt_limit: int = 20
    last_operation: SafetyOperationResponse | None = None
    blocking_operation: SafetyOperationResponse | None = None
    diagnosis: dict | None = None


class SafetyAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restriction_status: Literal["clear"]
    checked_at: AwareDatetime
    evidence: str = Field(min_length=1, max_length=10000)

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Operator evidence is required")
        return value.strip()

    @field_validator("checked_at")
    @classmethod
    def no_future_evidence(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC):
            raise ValueError("Restriction check cannot be in the future")
        return value


class SafetyOperationResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["accepted", "not_delivered"]
    evidence: str = Field(min_length=1, max_length=10000)
    message_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("evidence", "message_id")
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Evidence and message IDs must not be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def accepted_requires_message_id(self) -> "SafetyOperationResolutionRequest":
        if self.outcome == "accepted" and not self.message_id:
            raise ValueError("message_id is required for an accepted operation")
        return self


class OwnerAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: str = Field(min_length=1, max_length=10000)
