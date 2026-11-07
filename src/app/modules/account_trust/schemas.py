from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AccountTrustFactorResponse(BaseModel):
    key: str
    group: str
    label: str
    delta: int
    severity: str
    details: dict[str, Any] = Field(default_factory=dict)


class AccountTrustRecommendationsResponse(BaseModel):
    next_best_action: str
    allowed_modules: list[str] = Field(default_factory=list)
    blocked_modules: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)


class AccountTrustSnapshotResponse(BaseModel):
    id: str
    workspace_id: str
    telegram_account_id: str
    score_100: int
    score_10: float
    label: str
    confidence: str
    safety_status: str
    survival_7d: float
    survival_30d: float
    median_lifespan_days: int | None = None
    similar_sample_size: int = 0
    factor_scores: dict[str, int] = Field(default_factory=dict)
    risk_factors: list[AccountTrustFactorResponse] = Field(default_factory=list)
    positive_factors: list[AccountTrustFactorResponse] = Field(default_factory=list)
    hard_blocks: list[AccountTrustFactorResponse] = Field(default_factory=list)
    recommendations: AccountTrustRecommendationsResponse
    source_version: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountTrustSummaryResponse(BaseModel):
    total: int
    safety_statuses: dict[str, int]
    labels: dict[str, int]
    confidence: dict[str, int]
    avg_score: float | None = None
    outbound_blocked: int = 0
    needs_runtime_review: int = 0


class AccountTechnicalCheckResponse(BaseModel):
    account_id: str
    technical_status: str
    safety_status: str
    trust_score: int
    trust_label: str
    confidence: str
    next_best_action: str
    factors: list[AccountTrustFactorResponse] = Field(default_factory=list)
    snapshot: AccountTrustSnapshotResponse
