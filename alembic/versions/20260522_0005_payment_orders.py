"""payment orders

Revision ID: 20260522_0005
Revises: 20260518_0004
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260522_0005"
down_revision: str | None = "20260518_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("payment_orders"):
        op.create_table(
            "payment_orders",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("product_code", sa.String(length=80), nullable=False),
            sa.Column("product_name", sa.String(length=160), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("provider_payment_id", sa.String(length=160), nullable=True),
            sa.Column("checkout_url", sa.String(length=1000), nullable=True),
            sa.Column(
                "raw_payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_payment_orders_user_id_users")),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                name=op.f("fk_payment_orders_workspace_id_workspaces"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_orders")),
        )
    if not _index_exists("payment_orders", "ix_payment_orders_workspace_created"):
        op.create_index("ix_payment_orders_workspace_created", "payment_orders", ["workspace_id", "created_at"])
    if not _index_exists("payment_orders", "ix_payment_orders_provider_payment"):
        op.create_index("ix_payment_orders_provider_payment", "payment_orders", ["provider", "provider_payment_id"])
    if not _index_exists("payment_orders", "ix_payment_orders_status_created"):
        op.create_index("ix_payment_orders_status_created", "payment_orders", ["status", "created_at"])


def downgrade() -> None:
    if _index_exists("payment_orders", "ix_payment_orders_status_created"):
        op.drop_index("ix_payment_orders_status_created", table_name="payment_orders")
    if _index_exists("payment_orders", "ix_payment_orders_provider_payment"):
        op.drop_index("ix_payment_orders_provider_payment", table_name="payment_orders")
    if _index_exists("payment_orders", "ix_payment_orders_workspace_created"):
        op.drop_index("ix_payment_orders_workspace_created", table_name="payment_orders")
    if _table_exists("payment_orders"):
        op.drop_table("payment_orders")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
