from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field


class WarmupPlanCreate(BaseModel):
    name: str
    duration_days: int = Field(default=3, ge=1, le=30)
    account_ids: list[str] = Field(default_factory=list)
    scenario: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class WarmupPlanUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    duration_days: int | None = Field(default=None, ge=1, le=30)
    account_ids: list[str] | None = None
    scenario: str | None = None
    config: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None


class WarmupPlanResponse(BaseModel):
    id: str
    name: str
    status: str
    duration_days: int
    config: dict[str, Any]
    policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def account_ids(self) -> list[str]:
        value = self.config.get("account_ids", [])
        return value if isinstance(value, list) else []

    @computed_field
    @property
    def scenario(self) -> str | None:
        value = self.config.get("scenario")
        return value if isinstance(value, str) else None

    model_config = {"from_attributes": True}


class WarmupPresetResponse(BaseModel):
    id: str
    name: str
    description: str
    duration_days: int
    config: dict[str, Any]
    policy: dict[str, Any]


class WarmupStepResponse(BaseModel):
    id: str
    warmup_run_id: str
    telegram_account_id: str
    action_type: str
    status: str
    planned_at: datetime | None
    executed_at: datetime | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WarmupRunResponse(BaseModel):
    id: str
    workspace_id: str
    telegram_account_id: str
    warmup_plan_id: str
    status: str
    current_day: int
    score_before: int
    score_current: int
    started_at: datetime | None
    completed_at: datetime | None
    last_action_at: datetime | None
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
