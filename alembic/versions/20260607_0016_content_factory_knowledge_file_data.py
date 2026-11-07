"""content factory knowledge file data

Revision ID: 20260607_0016
Revises: 20260605_0015
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260607_0016"
down_revision: str | None = "20260605_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "content_factory_knowledge_items"


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    if not _column_exists(TABLE, "file_data"):
        op.add_column(TABLE, sa.Column("file_data", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    if _column_exists(TABLE, "file_data"):
        op.drop_column(TABLE, "file_data")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))
