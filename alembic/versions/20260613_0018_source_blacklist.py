"""source blacklist

Revision ID: 20260613_0018
Revises: 20260613_0017
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0018"
down_revision: str | None = "20260613_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("telegram_source_blacklist"):
        op.create_table(
            "telegram_source_blacklist",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("source_id", sa.String(), nullable=False),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("source_type", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=96), nullable=False),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("campaign_id", sa.String(), nullable=True),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("blocked_by", sa.String(length=32), nullable=False),
            sa.Column("first_blocked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["telegram_sources.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index(
        "uq_source_blacklist_workspace_source",
        "telegram_source_blacklist",
        ["workspace_id", "source_id"],
        unique=True,
    )
    _create_index(
        "ix_source_blacklist_workspace_status",
        "telegram_source_blacklist",
        ["workspace_id", "status"],
    )
    _create_index("ix_source_blacklist_reason", "telegram_source_blacklist", ["reason"])


def downgrade() -> None:
    _drop_index("ix_source_blacklist_reason", "telegram_source_blacklist")
    _drop_index("ix_source_blacklist_workspace_status", "telegram_source_blacklist")
    _drop_index("uq_source_blacklist_workspace_source", "telegram_source_blacklist")
    if _table_exists("telegram_source_blacklist"):
        op.drop_table("telegram_source_blacklist")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _create_index(name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    if not _index_exists(table_name, name):
        op.create_index(name, table_name, columns, unique=unique)


def _drop_index(name: str, table_name: str) -> None:
    if _index_exists(table_name, name):
        op.drop_index(name, table_name=table_name)
