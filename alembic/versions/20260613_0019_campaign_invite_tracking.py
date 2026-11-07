"""Add campaign invite link tracking metrics."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0019"
down_revision: str | None = "20260613_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_invite_link_metrics",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("telegram_account_id", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("invite_link", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("baseline_usage_count", sa.Integer(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=True),
        sa.Column("requested_count", sa.Integer(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=True),
        sa.Column("expire_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["telegram_account_id"], ["telegram_accounts.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_invite_metrics_campaign", "campaign_invite_link_metrics", ["campaign_id"])
    op.create_index(
        "ix_campaign_invite_metrics_workspace_checked",
        "campaign_invite_link_metrics",
        ["workspace_id", "last_checked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_invite_metrics_workspace_checked", table_name="campaign_invite_link_metrics")
    op.drop_index("ix_campaign_invite_metrics_campaign", table_name="campaign_invite_link_metrics")
    op.drop_table("campaign_invite_link_metrics")
