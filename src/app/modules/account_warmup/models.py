from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class WarmupPlan(IdMixin, TimestampMixin, Base):
    __tablename__ = "warmup_plans"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class WarmupRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "warmup_runs"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    telegram_account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    warmup_plan_id: Mapped[str] = mapped_column(ForeignKey("warmup_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    current_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    score_before: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(Text)


class WarmupStep(IdMixin, TimestampMixin, Base):
    __tablename__ = "warmup_steps"

    warmup_run_id: Mapped[str] = mapped_column(ForeignKey("warmup_runs.id"), nullable=False)
    telegram_account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)


Index("ix_warmup_plans_workspace_status", WarmupPlan.workspace_id, WarmupPlan.status)
Index("ix_warmup_runs_workspace_status_created", WarmupRun.workspace_id, WarmupRun.status, WarmupRun.created_at)
Index("ix_warmup_runs_account_status", WarmupRun.telegram_account_id, WarmupRun.status)
Index("ix_warmup_steps_run_status_planned", WarmupStep.warmup_run_id, WarmupStep.status, WarmupStep.planned_at)
Index(
    "ix_warmup_steps_account_status_planned", WarmupStep.telegram_account_id, WarmupStep.status, WarmupStep.planned_at
)
