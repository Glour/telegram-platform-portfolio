from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class AccountTrustSnapshot(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_trust_snapshots"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    telegram_account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    score_100: Mapped[int] = mapped_column(Integer, nullable=False)
    score_10: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    safety_status: Mapped[str] = mapped_column(String(32), nullable=False)
    survival_7d: Mapped[float] = mapped_column(Float, nullable=False)
    survival_30d: Mapped[float] = mapped_column(Float, nullable=False)
    median_lifespan_days: Mapped[int | None] = mapped_column(Integer)
    similar_sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    factor_scores: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    risk_factors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    positive_factors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    hard_blocks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    recommendations: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_version: Mapped[str] = mapped_column(String(32), default="trust_v1", nullable=False)


class AccountRiskFactor(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_risk_factors"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    telegram_account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    factor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    factor_group: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    score_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    ttl_until: Mapped[str | None] = mapped_column(String(40))
    source_type: Mapped[str | None] = mapped_column(String(80))
    source_id: Mapped[str | None] = mapped_column(String(160))
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


Index(
    "ix_account_trust_snapshots_account_created",
    AccountTrustSnapshot.telegram_account_id,
    AccountTrustSnapshot.created_at,
)
Index(
    "ix_account_trust_snapshots_workspace_created",
    AccountTrustSnapshot.workspace_id,
    AccountTrustSnapshot.created_at,
)
Index(
    "ix_account_risk_factors_account_created",
    AccountRiskFactor.telegram_account_id,
    AccountRiskFactor.created_at,
)
Index("ix_account_risk_factors_key_created", AccountRiskFactor.factor_key, AccountRiskFactor.created_at)
