from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class Plan(IdMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    monthly_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    yearly_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    features: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Subscription(IdMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(160))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False, nullable=False)


class Entitlement(IdMixin, TimestampMixin, Base):
    __tablename__ = "entitlements"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    feature_code: Mapped[str] = mapped_column(String(80), nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reset_period: Mapped[str] = mapped_column(String(32), default="month", nullable=False)
    resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "usage_events"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    feature_code: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(80))
    source_id: Mapped[str | None] = mapped_column(String(80))


Index("ix_subscriptions_workspace_status", Subscription.workspace_id, Subscription.status)
Index("ix_subscriptions_provider_id", Subscription.provider, Subscription.provider_subscription_id)
Index("ix_entitlements_workspace_feature", Entitlement.workspace_id, Entitlement.feature_code)
Index(
    "ix_usage_events_workspace_feature_created", UsageEvent.workspace_id, UsageEvent.feature_code, UsageEvent.created_at
)
