from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class AiProfile(IdMixin, TimestampMixin, Base):
    __tablename__ = "ai_profiles"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    tone: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str] = mapped_column(String(16), default="ru", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    safety_prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(120), default="gpt-6-luna", nullable=False)
    temperature: Mapped[str] = mapped_column(String(16), default="0.7", nullable=False)


class CommentPromptTemplate(IdMixin, TimestampMixin, Base):
    __tablename__ = "comment_prompt_templates"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    ai_profile_id: Mapped[str | None] = mapped_column(ForeignKey("ai_profiles.id"))
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    product_context: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="ru", nullable=False)
    max_length: Mapped[int] = mapped_column(default=300, nullable=False)
    blacklist_words: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class AiGeneration(IdMixin, TimestampMixin, Base):
    __tablename__ = "ai_generations"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("ai_profiles.id"))
    comment_prompt_template_id: Mapped[str | None] = mapped_column(ForeignKey("comment_prompt_templates.id"))
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"))
    input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    moderation_status: Mapped[str] = mapped_column(String(32), default="passed", nullable=False)
    token_usage: Mapped[dict | None] = mapped_column(JSON)


Index("ix_ai_profiles_workspace", AiProfile.workspace_id)
Index("ix_comment_prompts_workspace_status", CommentPromptTemplate.workspace_id, CommentPromptTemplate.status)
Index("ix_ai_generations_workspace_created", AiGeneration.workspace_id, AiGeneration.created_at)
