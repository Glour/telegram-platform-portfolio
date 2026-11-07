from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class Campaign(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class CampaignTarget(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaign_targets"

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_sources.id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_messages.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_users.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)


class CampaignAction(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaign_actions"

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    telegram_account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)


class CampaignRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaign_runs"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_stop_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    stop_conditions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    report: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)


class CampaignEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaign_events"

    run_id: Mapped[str] = mapped_column(ForeignKey("campaign_runs.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class CampaignInviteLinkMetric(IdMixin, TimestampMixin, Base):
    __tablename__ = "campaign_invite_link_metrics"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    telegram_account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    invite_link: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(String(120))
    baseline_usage_count: Mapped[int | None] = mapped_column(Integer)
    usage_count: Mapped[int | None] = mapped_column(Integer)
    requested_count: Mapped[int | None] = mapped_column(Integer)
    revoked: Mapped[bool | None] = mapped_column(Boolean)
    expire_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


Index("ix_campaigns_workspace_status_starts", Campaign.workspace_id, Campaign.status, Campaign.starts_at)
Index(
    "uq_contact_broadcast_action",
    CampaignAction.campaign_id,
    CampaignAction.telegram_account_id,
    CampaignAction.target_id,
    unique=True,
    postgresql_where=text("target_type = 'saved_contact'"),
    sqlite_where=text("target_type = 'saved_contact'"),
)
Index(
    "ix_campaign_actions_campaign_status_planned",
    CampaignAction.campaign_id,
    CampaignAction.status,
    CampaignAction.planned_at,
)
Index("ix_campaign_runs_campaign_status_created", CampaignRun.campaign_id, CampaignRun.status, CampaignRun.created_at)
Index("ix_campaign_runs_workspace_status_created", CampaignRun.workspace_id, CampaignRun.status, CampaignRun.created_at)
Index("ix_campaign_events_run_created", CampaignEvent.run_id, CampaignEvent.created_at)
Index("ix_campaign_events_campaign_created", CampaignEvent.campaign_id, CampaignEvent.created_at)
Index("ix_campaign_invite_metrics_campaign", CampaignInviteLinkMetric.campaign_id)
Index(
    "ix_campaign_invite_metrics_workspace_checked",
    CampaignInviteLinkMetric.workspace_id,
    CampaignInviteLinkMetric.last_checked_at,
)
Index(
    "ix_campaign_actions_account_status_planned",
    CampaignAction.telegram_account_id,
    CampaignAction.status,
    CampaignAction.planned_at,
)
