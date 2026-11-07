"""Add account market delivery fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0023"
down_revision: str | None = "20260613_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account_market_requests", sa.Column("seller_workspace_id", sa.String(), nullable=True))
    op.add_column("account_market_requests", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("account_market_requests", sa.Column("delivery_metadata", sa.JSON(), nullable=True))
    op.execute(
        """
        UPDATE account_market_requests
        SET seller_workspace_id = (
            SELECT seller_workspace_id
            FROM account_market_offers
            WHERE account_market_offers.id = account_market_requests.offer_id
        )
        WHERE seller_workspace_id IS NULL
        """
    )
    op.execute("UPDATE account_market_requests SET delivery_metadata = '{}' WHERE delivery_metadata IS NULL")
    op.create_index(
        "ix_account_market_requests_seller_status",
        "account_market_requests",
        ["seller_workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_market_requests_seller_status", table_name="account_market_requests")
    op.drop_column("account_market_requests", "delivery_metadata")
    op.drop_column("account_market_requests", "delivered_at")
    op.drop_column("account_market_requests", "seller_workspace_id")
