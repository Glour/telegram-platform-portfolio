from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class AssistantKnowledgeSource(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_knowledge_sources"

    source_type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    public_url: Mapped[str | None] = mapped_column(String(500))
    visibility: Mapped[str] = mapped_column(String(32), default="public", nullable=False)
    checksum: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AssistantKnowledgeChunk(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_knowledge_chunks"

    source_id: Mapped[str] = mapped_column(ForeignKey("assistant_knowledge_sources.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), default="public", nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AssistantConversation(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_conversations"

    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    visitor_id: Mapped[str | None] = mapped_column(String(120))
    surface: Mapped[str] = mapped_column(String(32), default="landing", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    initial_url: Mapped[str | None] = mapped_column(String(500))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssistantMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_messages"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("assistant_conversations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    retrieval_debug: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(80))
    moderation_status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)


class AssistantFeedback(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_feedback"

    message_id: Mapped[str] = mapped_column(ForeignKey("assistant_messages.id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("assistant_conversations.id"), nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)


class AssistantSupportRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "assistant_support_requests"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("assistant_conversations.id"), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    visitor_id: Mapped[str | None] = mapped_column(String(120))
    surface: Mapped[str] = mapped_column(String(32), default="landing", nullable=False)
    contact: Mapped[str] = mapped_column(String(240), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    page_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    transcript: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(80))
    user_agent: Mapped[str | None] = mapped_column(String(500))


Index("ix_assistant_sources_status_visibility", AssistantKnowledgeSource.status, AssistantKnowledgeSource.visibility)
Index("ix_assistant_sources_path", AssistantKnowledgeSource.path)
Index("ix_assistant_chunks_source", AssistantKnowledgeChunk.source_id)
Index("ix_assistant_chunks_visibility", AssistantKnowledgeChunk.visibility)
Index("ix_assistant_conversations_visitor", AssistantConversation.visitor_id, AssistantConversation.created_at)
Index("ix_assistant_conversations_user", AssistantConversation.user_id, AssistantConversation.created_at)
Index("ix_assistant_messages_conversation_created", AssistantMessage.conversation_id, AssistantMessage.created_at)
Index("ix_assistant_messages_ip_created", AssistantMessage.ip_address, AssistantMessage.created_at)
Index("ix_assistant_feedback_message", AssistantFeedback.message_id)
Index(
    "ix_assistant_support_requests_status_created",
    AssistantSupportRequest.status,
    AssistantSupportRequest.created_at,
)
Index("ix_assistant_support_requests_conversation", AssistantSupportRequest.conversation_id)
Index(
    "ix_assistant_support_requests_workspace_created",
    AssistantSupportRequest.workspace_id,
    AssistantSupportRequest.created_at,
)
