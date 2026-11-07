"""comment dataset items

Revision ID: 20260517_0002
Revises: 20260516_0001
Create Date: 2026-05-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260517_0002"
down_revision: str | None = "20260516_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "comment_dataset_items"
IX_WORKSPACE_SOURCE_CREATED = "ix_comment_dataset_workspace_source_created"
UQ_SOURCE_TG_COMMENT = "uq_comment_dataset_source_tg_comment"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("source_id", sa.String(), nullable=False),
            sa.Column("post_message_id", sa.String(), nullable=True),
            sa.Column("tg_comment_message_id", sa.String(length=160), nullable=False),
            sa.Column("author_tg_id", sa.String(length=160), nullable=True),
            sa.Column("post_text", sa.Text(), nullable=True),
            sa.Column("comment_text", sa.Text(), nullable=False),
            sa.Column("language", sa.String(length=16), nullable=False),
            sa.Column("quality_score", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["post_message_id"], ["telegram_messages.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["telegram_sources.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _index_exists(TABLE_NAME, IX_WORKSPACE_SOURCE_CREATED):
        op.create_index(
            IX_WORKSPACE_SOURCE_CREATED,
            TABLE_NAME,
            ["workspace_id", "source_id", "created_at"],
        )
    if not _index_exists(TABLE_NAME, UQ_SOURCE_TG_COMMENT):
        op.create_index(
            UQ_SOURCE_TG_COMMENT,
            TABLE_NAME,
            ["source_id", "tg_comment_message_id"],
            unique=True,
        )


def downgrade() -> None:
    if not _table_exists(TABLE_NAME):
        return

    if _index_exists(TABLE_NAME, UQ_SOURCE_TG_COMMENT):
        op.drop_index(UQ_SOURCE_TG_COMMENT, table_name=TABLE_NAME)
    if _index_exists(TABLE_NAME, IX_WORKSPACE_SOURCE_CREATED):
        op.drop_index(IX_WORKSPACE_SOURCE_CREATED, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
