"""content factories

Revision ID: 20260604_0011
Revises: 20260603_0010
Create Date: 2026-06-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260604_0011"
down_revision: str | None = "20260603_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    if not _table_exists("content_factories"):
        op.create_table(
            "content_factories",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=180), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=False),
            sa.Column("target_ref", sa.String(length=255), nullable=True),
            sa.Column("publisher_account_id", sa.String(), nullable=True),
            sa.Column("mode", sa.String(length=40), nullable=False),
            sa.Column("schedule_config", json_type, nullable=False),
            sa.Column("content_config", json_type, nullable=False),
            sa.Column("image_config", json_type, nullable=False),
            sa.Column("quality_config", json_type, nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["publisher_account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_factory_sources"):
        op.create_table(
            "content_factory_sources",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("source_id", sa.String(), nullable=True),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            sa.Column("source_type", sa.String(length=40), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("weight", sa.Integer(), nullable=False),
            sa.Column("last_parsed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("settings", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["telegram_sources.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_factory_knowledge_items"):
        op.create_table(
            "content_factory_knowledge_items",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("content_text", sa.Text(), nullable=True),
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("metadata", json_type, nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_factory_rubrics"):
        op.create_table(
            "content_factory_rubrics",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("target_share_percent", sa.Integer(), nullable=False),
            sa.Column("format_preset", sa.String(length=80), nullable=True),
            sa.Column("prompt_override", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_factory_runs"):
        op.create_table(
            "content_factory_runs",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("trigger_type", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("metrics", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_posts"):
        op.create_table(
            "content_posts",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=220), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("source_summary", sa.Text(), nullable=True),
            sa.Column("source_refs", json_type, nullable=False),
            sa.Column("image_prompt", sa.Text(), nullable=True),
            sa.Column("image_attachment_id", sa.String(length=255), nullable=True),
            sa.Column("telegram_message_id", sa.String(length=160), nullable=True),
            sa.Column("telegram_link", sa.Text(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("quality_score", sa.Integer(), nullable=True),
            sa.Column("quality_notes", json_type, nullable=False),
            sa.Column("metadata", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["content_factory_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_factory_events"):
        op.create_table(
            "content_factory_events",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=True),
            sa.Column("post_id", sa.String(), nullable=True),
            sa.Column("level", sa.String(length=16), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["content_posts.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["content_factory_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("content_memory_items"):
        op.create_table(
            "content_memory_items",
            sa.Column("factory_id", sa.String(), nullable=False),
            sa.Column("post_id", sa.String(), nullable=True),
            sa.Column("topic", sa.String(length=220), nullable=True),
            sa.Column("angle", sa.String(length=220), nullable=True),
            sa.Column("rubric", sa.String(length=120), nullable=True),
            sa.Column("key_claims", json_type, nullable=False),
            sa.Column("cta", sa.Text(), nullable=True),
            sa.Column("source_refs", json_type, nullable=False),
            sa.Column("text_fingerprint", sa.String(length=80), nullable=False),
            sa.Column("image_fingerprint", sa.String(length=80), nullable=True),
            sa.Column("performance_snapshot", json_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["factory_id"], ["content_factories.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["content_posts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    for index_name, table_name, columns in [
        ("ix_content_factories_workspace_status", "content_factories", ["workspace_id", "status"]),
        ("ix_content_factory_sources_factory_enabled", "content_factory_sources", ["factory_id", "enabled"]),
        ("ix_content_factory_knowledge_factory_enabled", "content_factory_knowledge_items", ["factory_id", "enabled"]),
        (
            "ix_content_factory_runs_factory_status_created",
            "content_factory_runs",
            ["factory_id", "status", "created_at"],
        ),
        ("ix_content_posts_factory_status_created", "content_posts", ["factory_id", "status", "created_at"]),
        ("ix_content_factory_events_factory_created", "content_factory_events", ["factory_id", "created_at"]),
        ("ix_content_memory_factory_fingerprint", "content_memory_items", ["factory_id", "text_fingerprint"]),
    ]:
        if _table_exists(table_name) and not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    for index_name, table_name in [
        ("ix_content_memory_factory_fingerprint", "content_memory_items"),
        ("ix_content_factory_events_factory_created", "content_factory_events"),
        ("ix_content_posts_factory_status_created", "content_posts"),
        ("ix_content_factory_runs_factory_status_created", "content_factory_runs"),
        ("ix_content_factory_knowledge_factory_enabled", "content_factory_knowledge_items"),
        ("ix_content_factory_sources_factory_enabled", "content_factory_sources"),
        ("ix_content_factories_workspace_status", "content_factories"),
    ]:
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    for table_name in [
        "content_memory_items",
        "content_factory_events",
        "content_posts",
        "content_factory_runs",
        "content_factory_rubrics",
        "content_factory_knowledge_items",
        "content_factory_sources",
        "content_factories",
    ]:
        if _table_exists(table_name):
            op.drop_table(table_name)


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
