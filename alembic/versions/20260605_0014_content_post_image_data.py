"""content post image data

Revision ID: 20260605_0014
Revises: 20260605_0013
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_0014"
down_revision: str | None = "20260605_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "content_posts"


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    if not _column_exists(TABLE, "image_data"):
        op.add_column(TABLE, sa.Column("image_data", sa.LargeBinary(), nullable=True))
    if not _column_exists(TABLE, "image_content_type"):
        op.add_column(TABLE, sa.Column("image_content_type", sa.String(length=80), nullable=True))


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    if _column_exists(TABLE, "image_content_type"):
        op.drop_column(TABLE, "image_content_type")
    if _column_exists(TABLE, "image_data"):
        op.drop_column(TABLE, "image_data")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))
