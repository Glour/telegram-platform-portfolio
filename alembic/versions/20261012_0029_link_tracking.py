"""Workspace-scoped redirect links and privacy-preserving transition events."""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0029"
down_revision = "20261003_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tracking_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("code", sa.String(32), unique=True, nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("destination_url", sa.Text(), nullable=False),
        sa.Column("placement", sa.String(160), nullable=False),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id")),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("telegram_accounts.id")),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tracking_links_workspace_id", "tracking_links", ["workspace_id"])
    op.create_table(
        "tracking_clicks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("link_id", sa.String(36), sa.ForeignKey("tracking_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("visitor_hash", sa.String(64), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
    )
    op.create_index("ix_tracking_clicks_link_time", "tracking_clicks", ["link_id", "occurred_at"])


def downgrade():
    op.drop_table("tracking_clicks")
    op.drop_table("tracking_links")
