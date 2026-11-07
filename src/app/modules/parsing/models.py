from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class ParsingJob(IdMixin, TimestampMixin, Base):
    __tablename__ = "parsing_jobs"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParsingJobLog(IdMixin, Base):
    __tablename__ = "parsing_job_logs"

    job_id: Mapped[str] = mapped_column(ForeignKey("parsing_jobs.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ParsingResult(IdMixin, TimestampMixin, Base):
    __tablename__ = "parsing_results"

    job_id: Mapped[str] = mapped_column(ForeignKey("parsing_jobs.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(String(255))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_sources.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_users.id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_messages.id"))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    matched_keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParsedTelegramComment(IdMixin, TimestampMixin, Base):
    __tablename__ = "parsed_telegram_comments"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_sources.id"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("parsing_jobs.id"))
    post_tg_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    comment_tg_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author_tg_id: Mapped[str | None] = mapped_column(String(160))
    author_username: Mapped[str | None] = mapped_column(String(160))
    author_display_name: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON)


Index("ix_parsing_jobs_workspace_created", ParsingJob.workspace_id, ParsingJob.created_at)
Index("ix_parsing_jobs_account_status", ParsingJob.account_id, ParsingJob.status)
Index("ix_parsing_job_logs_job_created", ParsingJobLog.job_id, ParsingJobLog.created_at)
Index("ix_parsing_results_job_entity", ParsingResult.job_id, ParsingResult.entity_type)
Index("ix_parsing_results_workspace_entity", ParsingResult.workspace_id, ParsingResult.entity_type)
Index(
    "uq_parsing_result_job_entity_ref",
    ParsingResult.job_id,
    ParsingResult.entity_type,
    ParsingResult.entity_ref,
    unique=True,
)
Index(
    "uq_parsed_comments_source_comment",
    ParsedTelegramComment.source_id,
    ParsedTelegramComment.comment_tg_message_id,
    unique=True,
)
