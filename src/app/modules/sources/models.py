from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TelegramSource(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_sources"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_source_folders.id"))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    tg_id: Mapped[str | None] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    comments_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    linked_discussion_tg_id: Mapped[str | None] = mapped_column(String(160))
    linked_discussion_username: Mapped[str | None] = mapped_column(String(160))
    access_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    participants_count: Mapped[int | None] = mapped_column(Integer)
    language_code: Mapped[str | None] = mapped_column(String(16))
    country_code: Mapped[str | None] = mapped_column(String(16))
    engagement_score: Mapped[int | None] = mapped_column(Integer)
    spam_suitability_score: Mapped[int | None] = mapped_column(Integer)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_join_request_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def source_risk_level(self) -> str:
        from app.modules.sources.safety import source_safety

        return str(source_safety(self)["source_risk_level"])

    @property
    def safe_for_new_accounts(self) -> bool:
        from app.modules.sources.safety import source_safety

        return bool(source_safety(self)["safe_for_new_accounts"])

    @property
    def safe_for_trusted_accounts(self) -> bool:
        from app.modules.sources.safety import source_safety

        return bool(source_safety(self)["safe_for_trusted_accounts"])

    @property
    def source_risk_reasons(self) -> list[str]:
        from app.modules.sources.safety import source_safety

        return list(source_safety(self)["reasons"])

    @property
    def source_recommended_action(self) -> str | None:
        from app.modules.sources.safety import source_safety

        action = source_safety(self)["recommended_action"]
        return str(action) if action else None


class TelegramSourceFolder(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_source_folders"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class TelegramSourceBlacklistEntry(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_source_blacklist"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("telegram_sources.id"), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    reason: Mapped[str] = mapped_column(String(96), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    blocked_by: Mapped[str] = mapped_column(String(32), default="system", nullable=False)
    first_blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class TelegramMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_messages"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("telegram_sources.id"), nullable=False)
    tg_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    author_tg_id: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    views_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    forwards_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replies_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON)


class TelegramUser(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_users"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    tg_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    username: Mapped[str | None] = mapped_column(String(160))
    first_name: Mapped[str | None] = mapped_column(String(160))
    last_name: Mapped[str | None] = mapped_column(String(160))
    is_bot: Mapped[bool] = mapped_column(default=False, nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(16))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comments_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_restricted: Mapped[bool] = mapped_column(default=False, nullable=False)
    user_metadata: Mapped[dict | None] = mapped_column(JSON)


Index("ix_sources_workspace_type_username", TelegramSource.workspace_id, TelegramSource.type, TelegramSource.username)
Index("ix_sources_workspace_comments", TelegramSource.workspace_id, TelegramSource.comments_enabled)
Index("ix_sources_workspace_folder", TelegramSource.workspace_id, TelegramSource.folder_id)
Index("ix_source_folders_workspace_name", TelegramSourceFolder.workspace_id, TelegramSourceFolder.name, unique=True)
Index(
    "uq_source_blacklist_workspace_source",
    TelegramSourceBlacklistEntry.workspace_id,
    TelegramSourceBlacklistEntry.source_id,
    unique=True,
)
Index(
    "ix_source_blacklist_workspace_status",
    TelegramSourceBlacklistEntry.workspace_id,
    TelegramSourceBlacklistEntry.status,
)
Index("ix_source_blacklist_reason", TelegramSourceBlacklistEntry.reason)
Index(
    "ix_messages_workspace_source_published",
    TelegramMessage.workspace_id,
    TelegramMessage.source_id,
    TelegramMessage.published_at,
)
Index("uq_messages_source_tg_message", TelegramMessage.source_id, TelegramMessage.tg_message_id, unique=True)
Index("ix_telegram_users_workspace_username", TelegramUser.workspace_id, TelegramUser.username)
