from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TrackingLink(IdMixin, TimestampMixin, Base):
    __tablename__ = "tracking_links"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    destination_url: Mapped[str] = mapped_column(Text)
    placement: Mapped[str] = mapped_column(String(160), default="")
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    enabled: Mapped[bool] = mapped_column(default=True)


class TrackingClick(IdMixin, Base):
    __tablename__ = "tracking_clicks"
    link_id: Mapped[str] = mapped_column(ForeignKey("tracking_links.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    day: Mapped[str] = mapped_column(String(10))
    visitor_hash: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(24))
    __table_args__ = (Index("ix_tracking_clicks_link_time", "link_id", "occurred_at"),)
