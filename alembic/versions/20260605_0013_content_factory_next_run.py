"""content factory next run

Revision ID: 20260605_0013
Revises: 20260604_0012
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_0013"
down_revision: str | None = "20260604_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "content_factories"
INDEX = "ix_content_factories_status_next_run"


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    if not _column_exists(TABLE, "next_run_at"):
        op.add_column(TABLE, sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    if not _index_exists(TABLE, INDEX):
        op.create_index(INDEX, TABLE, ["status", "next_run_at"])


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    if _index_exists(TABLE, INDEX):
        op.drop_index(INDEX, table_name=TABLE)
    if _column_exists(TABLE, "next_run_at"):
        op.drop_column(TABLE, "next_run_at")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
