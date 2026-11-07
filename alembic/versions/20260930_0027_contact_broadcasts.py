"""Private saved-contact snapshots and persistent broadcast media."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0027"
down_revision: str | None = "20260901_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, columns in (
        (
            "contact_broadcast_media",
            [
                sa.Column("filename", sa.String(80), nullable=False),
                sa.Column("content_type", sa.String(30), nullable=False),
            ],
        ),
        (
            "contact_broadcast_snapshots",
            [
                sa.Column("account_id", sa.String(), sa.ForeignKey("telegram_accounts.id"), nullable=False),
                sa.Column("kind", sa.String(20), nullable=False),
                sa.Column("encrypted_payload", sa.Text(), nullable=False),
                sa.Column("campaign_id", sa.String(), sa.ForeignKey("campaigns.id"), unique=True),
            ],
        ),
    ):
        op.create_table(
            table,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("workspace_id", sa.String(), sa.ForeignKey("workspaces.id"), nullable=False),
            *columns,
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    op.create_index(
        "uq_contact_broadcast_action",
        "campaign_actions",
        ["campaign_id", "telegram_account_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("target_type = 'saved_contact'"),
        sqlite_where=sa.text("target_type = 'saved_contact'"),
    )


def downgrade() -> None:
    op.drop_index("uq_contact_broadcast_action", table_name="campaign_actions")
    op.drop_table("contact_broadcast_snapshots")
    op.drop_table("contact_broadcast_media")
