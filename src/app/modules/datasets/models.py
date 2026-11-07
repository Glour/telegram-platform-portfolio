from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class CommentDatasetItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "comment_dataset_items"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("telegram_sources.id"), nullable=False)
    post_message_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_messages.id"))
    tg_comment_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    author_tg_id: Mapped[str | None] = mapped_column(String(160))
    post_text: Mapped[str | None] = mapped_column(Text)
    comment_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ru", nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="collected", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSON)


Index(
    "ix_comment_dataset_workspace_source_created",
    CommentDatasetItem.workspace_id,
    CommentDatasetItem.source_id,
    CommentDatasetItem.created_at,
)
Index(
    "uq_comment_dataset_source_tg_comment",
    CommentDatasetItem.source_id,
    CommentDatasetItem.tg_comment_message_id,
    unique=True,
)
