"""Add account market offers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0020"
down_revision: str | None = "20260613_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_market_offers",
        sa.Column("seller_workspace_id", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("proxy_id", sa.String(), nullable=True),
        sa.Column("offer_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_rub", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("module_codes", sa.JSON(), nullable=False),
        sa.Column("target_segments", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("warmup_profile", sa.String(length=80), nullable=True),
        sa.Column("warmup_days", sa.Integer(), nullable=True),
        sa.Column("warmup_score", sa.Integer(), nullable=True),
        sa.Column("health_score", sa.Integer(), nullable=True),
        sa.Column("quality_label", sa.String(length=80), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("proxy_country", sa.String(length=8), nullable=True),
        sa.Column("proxy_class", sa.String(length=32), nullable=True),
        sa.Column("includes_proxy", sa.Boolean(), nullable=False),
        sa.Column("ready_to_run", sa.Boolean(), nullable=False),
        sa.Column("stock_count", sa.Integer(), nullable=False),
        sa.Column("sort_rank", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["proxy_id"], ["proxies.id"]),
        sa.ForeignKeyConstraint(["seller_workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_market_status_rank_created",
        "account_market_offers",
        ["status", "sort_rank", "created_at"],
    )
    op.create_index("ix_account_market_seller_status", "account_market_offers", ["seller_workspace_id", "status"])
    op.create_index("ix_account_market_offer_type_status", "account_market_offers", ["offer_type", "status"])


def downgrade() -> None:
    op.drop_index("ix_account_market_offer_type_status", table_name="account_market_offers")
    op.drop_index("ix_account_market_seller_status", table_name="account_market_offers")
    op.drop_index("ix_account_market_status_rank_created", table_name="account_market_offers")
    op.drop_table("account_market_offers")
