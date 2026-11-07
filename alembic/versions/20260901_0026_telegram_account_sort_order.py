"""Add persistent manual ordering for Telegram accounts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0026"
down_revision: str | None = "20260616_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_accounts",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="1000000"),
    )
    op.create_index(
        "ix_telegram_accounts_workspace_sort_order",
        "telegram_accounts",
        ["workspace_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_accounts_workspace_sort_order", table_name="telegram_accounts")
    op.drop_column("telegram_accounts", "sort_order")
