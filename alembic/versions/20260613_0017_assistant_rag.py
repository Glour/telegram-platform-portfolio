"""assistant rag support chat

Revision ID: 20260613_0017
Revises: 20260607_0016
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0017"
down_revision: str | None = "20260607_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("assistant_knowledge_sources"):
        op.create_table(
            "assistant_knowledge_sources",
            sa.Column("source_type", sa.String(length=48), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("path", sa.String(length=500), nullable=False),
            sa.Column("public_url", sa.String(length=500), nullable=True),
            sa.Column("visibility", sa.String(length=32), nullable=False),
            sa.Column("checksum", sa.String(length=96), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_metadata", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_assistant_sources_status_visibility", "assistant_knowledge_sources", ["status", "visibility"])
    _create_index("ix_assistant_sources_path", "assistant_knowledge_sources", ["path"])

    if not _table_exists("assistant_knowledge_chunks"):
        op.create_table(
            "assistant_knowledge_chunks",
            sa.Column("source_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("section_path", sa.String(length=500), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("keywords", sa.JSON(), nullable=False),
            sa.Column("visibility", sa.String(length=32), nullable=False),
            sa.Column("search_text", sa.Text(), nullable=False),
            sa.Column("chunk_metadata", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["source_id"], ["assistant_knowledge_sources.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_assistant_chunks_source", "assistant_knowledge_chunks", ["source_id"])
    _create_index("ix_assistant_chunks_visibility", "assistant_knowledge_chunks", ["visibility"])

    if not _table_exists("assistant_conversations"):
        op.create_table(
            "assistant_conversations",
            sa.Column("workspace_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.String(), nullable=True),
            sa.Column("visitor_id", sa.String(length=120), nullable=True),
            sa.Column("surface", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("initial_url", sa.String(length=500), nullable=True),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_assistant_conversations_visitor", "assistant_conversations", ["visitor_id", "created_at"])
    _create_index("ix_assistant_conversations_user", "assistant_conversations", ["user_id", "created_at"])

    if not _table_exists("assistant_messages"):
        op.create_table(
            "assistant_messages",
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("citations", sa.JSON(), nullable=False),
            sa.Column("retrieval_debug", sa.JSON(), nullable=False),
            sa.Column("token_usage", sa.JSON(), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("ip_address", sa.String(length=80), nullable=True),
            sa.Column("moderation_status", sa.String(length=32), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_assistant_messages_conversation_created", "assistant_messages", ["conversation_id", "created_at"])
    _create_index("ix_assistant_messages_ip_created", "assistant_messages", ["ip_address", "created_at"])

    if not _table_exists("assistant_feedback"):
        op.create_table(
            "assistant_feedback",
            sa.Column("message_id", sa.String(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("rating", sa.String(length=16), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["assistant_messages.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_assistant_feedback_message", "assistant_feedback", ["message_id"])


def downgrade() -> None:
    _drop_index("ix_assistant_feedback_message", "assistant_feedback")
    if _table_exists("assistant_feedback"):
        op.drop_table("assistant_feedback")
    _drop_index("ix_assistant_messages_ip_created", "assistant_messages")
    _drop_index("ix_assistant_messages_conversation_created", "assistant_messages")
    if _table_exists("assistant_messages"):
        op.drop_table("assistant_messages")
    _drop_index("ix_assistant_conversations_user", "assistant_conversations")
    _drop_index("ix_assistant_conversations_visitor", "assistant_conversations")
    if _table_exists("assistant_conversations"):
        op.drop_table("assistant_conversations")
    _drop_index("ix_assistant_chunks_visibility", "assistant_knowledge_chunks")
    _drop_index("ix_assistant_chunks_source", "assistant_knowledge_chunks")
    if _table_exists("assistant_knowledge_chunks"):
        op.drop_table("assistant_knowledge_chunks")
    _drop_index("ix_assistant_sources_path", "assistant_knowledge_sources")
    _drop_index("ix_assistant_sources_status_visibility", "assistant_knowledge_sources")
    if _table_exists("assistant_knowledge_sources"):
        op.drop_table("assistant_knowledge_sources")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _create_index(name: str, table_name: str, columns: list[str]) -> None:
    if not _index_exists(table_name, name):
        op.create_index(name, table_name, columns)


def _drop_index(name: str, table_name: str) -> None:
    if _index_exists(table_name, name):
        op.drop_index(name, table_name=table_name)
