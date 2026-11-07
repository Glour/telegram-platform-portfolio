"""parsing jobs and persisted results

Revision ID: 20260603_0010
Revises: 20260601_0009
Create Date: 2026-06-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260603_0010"
down_revision: str | None = "20260601_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    if not _table_exists("parsing_jobs"):
        op.create_table(
            "parsing_jobs",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("type", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("input", json_type, nullable=False),
            sa.Column("filters", json_type, nullable=False),
            sa.Column("limits", json_type, nullable=False),
            sa.Column("progress_total", sa.Integer(), nullable=False),
            sa.Column("progress_done", sa.Integer(), nullable=False),
            sa.Column("found_count", sa.Integer(), nullable=False),
            sa.Column("saved_count", sa.Integer(), nullable=False),
            sa.Column("skipped_count", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("parsing_job_logs"):
        op.create_table(
            "parsing_job_logs",
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("level", sa.String(length=16), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload", json_type, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["parsing_jobs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("parsing_results"):
        op.create_table(
            "parsing_results",
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("entity_type", sa.String(length=32), nullable=False),
            sa.Column("entity_ref", sa.String(length=255), nullable=True),
            sa.Column("source_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.String(), nullable=True),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("payload", json_type, nullable=False),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("matched_keywords", json_type, nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["parsing_jobs.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["telegram_messages.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["telegram_sources.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["telegram_users.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("parsed_telegram_comments"):
        op.create_table(
            "parsed_telegram_comments",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("source_id", sa.String(), nullable=True),
            sa.Column("job_id", sa.String(), nullable=True),
            sa.Column("post_tg_message_id", sa.String(length=160), nullable=False),
            sa.Column("comment_tg_message_id", sa.String(length=160), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("author_tg_id", sa.String(length=160), nullable=True),
            sa.Column("author_username", sa.String(length=160), nullable=True),
            sa.Column("author_display_name", sa.String(length=255), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("matched_keywords", json_type, nullable=False),
            sa.Column("raw", json_type, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["parsing_jobs.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["telegram_sources.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    _add_column("telegram_sources", "participants_count", sa.Column("participants_count", sa.Integer(), nullable=True))
    _add_column("telegram_sources", "language_code", sa.Column("language_code", sa.String(length=16), nullable=True))
    _add_column("telegram_sources", "country_code", sa.Column("country_code", sa.String(length=16), nullable=True))
    _add_column("telegram_sources", "engagement_score", sa.Column("engagement_score", sa.Integer(), nullable=True))
    _add_column(
        "telegram_sources",
        "spam_suitability_score",
        sa.Column("spam_suitability_score", sa.Integer(), nullable=True),
    )
    _add_column(
        "telegram_sources",
        "last_message_at",
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column("telegram_sources", "description", sa.Column("description", sa.Text(), nullable=True))
    _add_column(
        "telegram_sources",
        "is_private",
        sa.Column("is_private", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "telegram_sources",
        "is_join_request_required",
        sa.Column("is_join_request_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    _add_column(
        "telegram_users",
        "first_seen_at",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column("telegram_users", "last_seen_at", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    _add_column(
        "telegram_users",
        "last_activity_at",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column(
        "telegram_users",
        "comments_count",
        sa.Column("comments_count", sa.Integer(), server_default="0", nullable=False),
    )
    _add_column(
        "telegram_users",
        "messages_count",
        sa.Column("messages_count", sa.Integer(), server_default="0", nullable=False),
    )
    _add_column(
        "telegram_users",
        "matched_keywords",
        sa.Column("matched_keywords", json_type, nullable=False, server_default="[]"),
    )
    _add_column(
        "telegram_users",
        "source_refs",
        sa.Column("source_refs", json_type, nullable=False, server_default="[]"),
    )
    _add_column(
        "telegram_users",
        "is_deleted",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _add_column(
        "telegram_users",
        "is_restricted",
        sa.Column("is_restricted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    for index_name, table_name, columns, unique in [
        ("ix_parsing_jobs_workspace_created", "parsing_jobs", ["workspace_id", "created_at"], False),
        ("ix_parsing_jobs_account_status", "parsing_jobs", ["account_id", "status"], False),
        ("ix_parsing_job_logs_job_created", "parsing_job_logs", ["job_id", "created_at"], False),
        ("ix_parsing_results_job_entity", "parsing_results", ["job_id", "entity_type"], False),
        ("ix_parsing_results_workspace_entity", "parsing_results", ["workspace_id", "entity_type"], False),
        ("uq_parsing_result_job_entity_ref", "parsing_results", ["job_id", "entity_type", "entity_ref"], True),
        ("uq_parsed_comments_source_comment", "parsed_telegram_comments", ["source_id", "comment_tg_message_id"], True),
    ]:
        if _table_exists(table_name) and not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, columns, unique=unique)


def downgrade() -> None:
    for index_name, table_name in [
        ("uq_parsed_comments_source_comment", "parsed_telegram_comments"),
        ("uq_parsing_result_job_entity_ref", "parsing_results"),
        ("ix_parsing_results_workspace_entity", "parsing_results"),
        ("ix_parsing_results_job_entity", "parsing_results"),
        ("ix_parsing_job_logs_job_created", "parsing_job_logs"),
        ("ix_parsing_jobs_account_status", "parsing_jobs"),
        ("ix_parsing_jobs_workspace_created", "parsing_jobs"),
    ]:
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    for table_name in ["parsed_telegram_comments", "parsing_results", "parsing_job_logs", "parsing_jobs"]:
        if _table_exists(table_name):
            op.drop_table(table_name)


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def _add_column(table_name: str, column_name: str, column: sa.Column) -> None:
    if _table_exists(table_name) and not _column_exists(table_name, column_name):
        op.add_column(table_name, column)
