"""operational events

Revision ID: 20260528_0007
Revises: 20260526_0006
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260528_0007"
down_revision: str | None = "20260526_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "operational_events"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists(TABLE):
        op.create_table(
            TABLE,
            sa.Column("workspace_id", sa.String(), nullable=True),
            sa.Column("severity", sa.String(length=24), nullable=False),
            sa.Column("category", sa.String(length=80), nullable=False),
            sa.Column("event_type", sa.String(length=120), nullable=False),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=120), nullable=True),
            sa.Column("entity_type", sa.String(length=80), nullable=True),
            sa.Column("entity_id", sa.String(length=120), nullable=True),
            sa.Column("dedupe_key", sa.String(length=240), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    indexes = {
        "ix_operational_events_workspace_created": ["workspace_id", "created_at"],
        "ix_operational_events_severity_created": ["severity", "created_at"],
        "ix_operational_events_type_created": ["event_type", "created_at"],
        "ix_operational_events_dedupe_created": ["dedupe_key", "created_at"],
    }
    for index_name, columns in indexes.items():
        if not _index_exists(TABLE, index_name):
            op.create_index(index_name, TABLE, columns)


def downgrade() -> None:
    if not _table_exists(TABLE):
        return
    for index_name in [
        "ix_operational_events_dedupe_created",
        "ix_operational_events_type_created",
        "ix_operational_events_severity_created",
        "ix_operational_events_workspace_created",
    ]:
        if _index_exists(TABLE, index_name):
            op.drop_index(index_name, table_name=TABLE)
    op.drop_table(TABLE)
