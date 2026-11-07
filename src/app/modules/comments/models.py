from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TelegramComment(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_comments"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("telegram_sources.id"), nullable=False)
    post_message_id: Mapped[str] = mapped_column(ForeignKey("telegram_messages.id"), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"))
    campaign_action_id: Mapped[str | None] = mapped_column(ForeignKey("campaign_actions.id"))
    telegram_account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    ai_generation_id: Mapped[str | None] = mapped_column(ForeignKey("ai_generations.id"))
    tg_comment_message_id: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)


Index(
    "ix_comments_workspace_source_sent",
    TelegramComment.workspace_id,
    TelegramComment.source_id,
    TelegramComment.sent_at,
)
Index("ix_comments_campaign_status_sent", TelegramComment.campaign_id, TelegramComment.status, TelegramComment.sent_at)
Index(
    "ix_comments_account_status_sent",
    TelegramComment.telegram_account_id,
    TelegramComment.status,
    TelegramComment.sent_at,
)
