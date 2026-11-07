# ruff: noqa: E501
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class ContentFactory(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_factories"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), default="channel", nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(255))
    publisher_account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    mode: Mapped[str] = mapped_column(String(40), default="approval_required", nullable=False)
    schedule_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    image_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    quality_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class ContentFactorySource(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_factory_sources"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_sources.id"))
    source_ref: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(40), default="telegram_channel", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ContentFactoryKnowledgeItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_factory_knowledge_items"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="text", nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    file_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    item_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)


class ContentFactoryRubric(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_factory_rubrics"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    target_share_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    format_preset: Mapped[str | None] = mapped_column(String(80))
    prompt_override: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContentFactoryRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_factory_runs"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ContentPost(IdMixin, TimestampMixin, Base):
    __tablename__ = "content_posts"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("content_factory_runs.id"))
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    title: Mapped[str | None] = mapped_column(String(220))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_summary: Mapped[str | None] = mapped_column(Text)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    image_prompt: Mapped[str | None] = mapped_column(Text)
    image_attachment_id: Mapped[str | None] = mapped_column(String(255))
    image_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_content_type: Mapped[str | None] = mapped_column(String(80))
    telegram_message_id: Mapped[str | None] = mapped_column(String(160))
    telegram_link: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_score: Mapped[int | None] = mapped_column(Integer)
    quality_notes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    post_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)


class ContentFactoryEvent(IdMixin, Base):
    __tablename__ = "content_factory_events"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("content_factory_runs.id"))
    post_id: Mapped[str | None] = mapped_column(ForeignKey("content_posts.id"))
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContentMemoryItem(IdMixin, Base):
    __tablename__ = "content_memory_items"

    factory_id: Mapped[str] = mapped_column(ForeignKey("content_factories.id"), nullable=False)
    post_id: Mapped[str | None] = mapped_column(ForeignKey("content_posts.id"))
    topic: Mapped[str | None] = mapped_column(String(220))
    angle: Mapped[str | None] = mapped_column(String(220))
    rubric: Mapped[str | None] = mapped_column(String(120))
    key_claims: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    cta: Mapped[str | None] = mapped_column(Text)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    image_fingerprint: Mapped[str | None] = mapped_column(String(80))
    performance_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_content_factories_workspace_status", ContentFactory.workspace_id, ContentFactory.status)
Index("ix_content_factories_status_next_run", ContentFactory.status, ContentFactory.next_run_at)
Index("ix_content_factory_sources_factory_enabled", ContentFactorySource.factory_id, ContentFactorySource.enabled)
Index(
    "ix_content_factory_knowledge_factory_enabled",
    ContentFactoryKnowledgeItem.factory_id,
    ContentFactoryKnowledgeItem.enabled,
)
Index(
    "ix_content_factory_runs_factory_status_created",
    ContentFactoryRun.factory_id,
    ContentFactoryRun.status,
    ContentFactoryRun.created_at,
)
Index("ix_content_posts_factory_status_created", ContentPost.factory_id, ContentPost.status, ContentPost.created_at)
Index("ix_content_factory_events_factory_created", ContentFactoryEvent.factory_id, ContentFactoryEvent.created_at)
Index("ix_content_memory_factory_fingerprint", ContentMemoryItem.factory_id, ContentMemoryItem.text_fingerprint)
