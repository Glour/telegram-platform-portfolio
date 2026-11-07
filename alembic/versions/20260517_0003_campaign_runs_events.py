"""campaign runs and events

Revision ID: 20260517_0003
Revises: 20260517_0002
Create Date: 2026-05-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260517_0003"
down_revision: str | None = "20260517_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS_TABLE = "campaign_runs"
EVENTS_TABLE = "campaign_events"

IX_RUNS_CAMPAIGN_STATUS_CREATED = "ix_campaign_runs_campaign_status_created"
IX_RUNS_WORKSPACE_STATUS_CREATED = "ix_campaign_runs_workspace_status_created"
IX_EVENTS_RUN_CREATED = "ix_campaign_events_run_created"
IX_EVENTS_CAMPAIGN_CREATED = "ix_campaign_events_campaign_created"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists(RUNS_TABLE):
        op.create_table(
            RUNS_TABLE,
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("campaign_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("requested_stop_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("stop_conditions", sa.JSON(), nullable=False),
            sa.Column("report", sa.JSON(), nullable=False),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists(EVENTS_TABLE):
        op.create_table(
            EVENTS_TABLE,
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("campaign_id", sa.String(), nullable=False),
            sa.Column("level", sa.String(length=16), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["campaign_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _index_exists(RUNS_TABLE, IX_RUNS_CAMPAIGN_STATUS_CREATED):
        op.create_index(IX_RUNS_CAMPAIGN_STATUS_CREATED, RUNS_TABLE, ["campaign_id", "status", "created_at"])
    if not _index_exists(RUNS_TABLE, IX_RUNS_WORKSPACE_STATUS_CREATED):
        op.create_index(IX_RUNS_WORKSPACE_STATUS_CREATED, RUNS_TABLE, ["workspace_id", "status", "created_at"])
    if not _index_exists(EVENTS_TABLE, IX_EVENTS_RUN_CREATED):
        op.create_index(IX_EVENTS_RUN_CREATED, EVENTS_TABLE, ["run_id", "created_at"])
    if not _index_exists(EVENTS_TABLE, IX_EVENTS_CAMPAIGN_CREATED):
        op.create_index(IX_EVENTS_CAMPAIGN_CREATED, EVENTS_TABLE, ["campaign_id", "created_at"])


def downgrade() -> None:
    if _table_exists(EVENTS_TABLE):
        if _index_exists(EVENTS_TABLE, IX_EVENTS_CAMPAIGN_CREATED):
            op.drop_index(IX_EVENTS_CAMPAIGN_CREATED, table_name=EVENTS_TABLE)
        if _index_exists(EVENTS_TABLE, IX_EVENTS_RUN_CREATED):
            op.drop_index(IX_EVENTS_RUN_CREATED, table_name=EVENTS_TABLE)
        op.drop_table(EVENTS_TABLE)

    if _table_exists(RUNS_TABLE):
        if _index_exists(RUNS_TABLE, IX_RUNS_WORKSPACE_STATUS_CREATED):
            op.drop_index(IX_RUNS_WORKSPACE_STATUS_CREATED, table_name=RUNS_TABLE)
        if _index_exists(RUNS_TABLE, IX_RUNS_CAMPAIGN_STATUS_CREATED):
            op.drop_index(IX_RUNS_CAMPAIGN_STATUS_CREATED, table_name=RUNS_TABLE)
        op.drop_table(RUNS_TABLE)
