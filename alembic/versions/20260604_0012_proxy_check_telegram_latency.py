"""proxy check telegram latency

Revision ID: 20260604_0012
Revises: 20260604_0011
Create Date: 2026-06-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260604_0012"
down_revision: str | None = "20260604_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proxy_checks"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    columns = [
        sa.Column("telegram_latency_ms", sa.Integer(), nullable=True),
        sa.Column("geo_country", sa.String(length=8), nullable=True),
    ]
    for column in columns:
        if not _column_exists(TABLE, column.name):
            op.add_column(TABLE, column)


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    for column_name in ["geo_country", "telegram_latency_ms"]:
        if _column_exists(TABLE, column_name):
            op.drop_column(TABLE, column_name)
