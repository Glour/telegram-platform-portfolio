"""proxy safety fields

Revision ID: 20260601_0008
Revises: 20260528_0007
Create Date: 2026-06-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260601_0008"
down_revision: str | None = "20260528_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proxies"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    columns = [
        sa.Column("city_state", sa.String(length=120), nullable=True),
        sa.Column("proxy_class", sa.String(length=32), nullable=True),
        sa.Column("exclusivity", sa.String(length=32), nullable=True),
        sa.Column("sticky_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ip_detected", sa.String(length=80), nullable=True),
        sa.Column("last_country_detected", sa.String(length=8), nullable=True),
        sa.Column("telegram_connectivity_status", sa.String(length=32), nullable=True),
    ]
    for column in columns:
        if not _column_exists(TABLE, column.name):
            op.add_column(TABLE, column)


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    for column_name in [
        "telegram_connectivity_status",
        "last_country_detected",
        "last_ip_detected",
        "sticky_until",
        "exclusivity",
        "proxy_class",
        "city_state",
    ]:
        if _column_exists(TABLE, column_name):
            op.drop_column(TABLE, column_name)
