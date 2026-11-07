"""Add account market payment fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260613_0024"
down_revision: str | None = "20260613_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account_market_requests", sa.Column("payment_order_id", sa.String(), nullable=True))
    op.add_column("account_market_requests", sa.Column("provider", sa.String(length=40), nullable=True))
    op.add_column("account_market_requests", sa.Column("amount_rub", sa.Integer(), nullable=True))
    op.add_column("account_market_requests", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_account_market_requests_payment_order",
        "account_market_requests",
        ["payment_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_market_requests_payment_order", table_name="account_market_requests")
    op.drop_column("account_market_requests", "paid_at")
    op.drop_column("account_market_requests", "amount_rub")
    op.drop_column("account_market_requests", "provider")
    op.drop_column("account_market_requests", "payment_order_id")
