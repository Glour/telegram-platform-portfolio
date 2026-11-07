"""assistant support requests

Revision ID: 20260613_0021
Revises: 20260613_0020
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0021"
down_revision: str | None = "20260613_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("assistant_support_requests"):
        op.create_table(
            "assistant_support_requests",
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("workspace_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.String(), nullable=True),
            sa.Column("visitor_id", sa.String(length=120), nullable=True),
            sa.Column("surface", sa.String(length=32), nullable=False),
            sa.Column("contact", sa.String(length=240), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("page_url", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("transcript", sa.JSON(), nullable=False),
            sa.Column("ip_address", sa.String(length=80), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index(
        "ix_assistant_support_requests_status_created",
        "assistant_support_requests",
        ["status", "created_at"],
    )
    _create_index(
        "ix_assistant_support_requests_conversation",
        "assistant_support_requests",
        ["conversation_id"],
    )
    _create_index(
        "ix_assistant_support_requests_workspace_created",
        "assistant_support_requests",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    _drop_index("ix_assistant_support_requests_workspace_created", "assistant_support_requests")
    _drop_index("ix_assistant_support_requests_conversation", "assistant_support_requests")
    _drop_index("ix_assistant_support_requests_status_created", "assistant_support_requests")
    if _table_exists("assistant_support_requests"):
        op.drop_table("assistant_support_requests")


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
