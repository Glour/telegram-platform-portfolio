"""Add account market requests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0022"
down_revision: str | tuple[str, str] | None = ("20260613_0020", "20260613_0021")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_market_requests",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("offer_id", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("contact", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("module_code", sa.String(length=120), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["offer_id"], ["account_market_offers.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_market_requests_workspace_status",
        "account_market_requests",
        ["workspace_id", "status"],
    )
    op.create_index(
        "ix_account_market_requests_offer_created",
        "account_market_requests",
        ["offer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_market_requests_offer_created", table_name="account_market_requests")
    op.drop_index("ix_account_market_requests_workspace_status", table_name="account_market_requests")
    op.drop_table("account_market_requests")
