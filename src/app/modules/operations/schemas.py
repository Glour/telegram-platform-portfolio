from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OperationSeverity = Literal["info", "warning", "error", "critical"]
OperationHealthStatus = Literal["ok", "warning", "critical"]


class OperationalEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str | None = None
    severity: OperationSeverity
    category: str
    event_type: str
    title: str
    message: str
    source: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    dedupe_key: str | None = None
    payload: dict[str, Any]
    notified_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class OperationHealthCheck(BaseModel):
    key: str
    label: str
    status: OperationHealthStatus
    message: str
    updated_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OperationHealthCounters(BaseModel):
    active_campaign_runs: int
    stale_campaign_runs: int
    events_24h: int
    critical_events_24h: int
    error_events_24h: int
    telegram_accounts_active: int
    telegram_accounts_problem: int


class OperationsHealthResponse(BaseModel):
    status: OperationHealthStatus
    generated_at: datetime
    counters: OperationHealthCounters
    checks: list[OperationHealthCheck]
    recent_events: list[OperationalEventResponse]


class OperationsMonitorRunResponse(BaseModel):
    status: str
    events_created: int
    checked_at: datetime
