"""billing carts

Revision ID: 20260526_0006
Revises: 20260522_0005
Create Date: 2026-05-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260526_0006"
down_revision: str | None = "20260522_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("billing_carts"):
        op.create_table(
            "billing_carts",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column(
                "items",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_billing_carts_user_id_users")),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                name=op.f("fk_billing_carts_workspace_id_workspaces"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_carts")),
            sa.UniqueConstraint("user_id", "workspace_id", name="uq_billing_carts_user_workspace"),
        )
    if not _index_exists("billing_carts", "ix_billing_carts_user_workspace"):
        op.create_index("ix_billing_carts_user_workspace", "billing_carts", ["user_id", "workspace_id"])


def downgrade() -> None:
    if _index_exists("billing_carts", "ix_billing_carts_user_workspace"):
        op.drop_index("ix_billing_carts_user_workspace", table_name="billing_carts")
    if _table_exists("billing_carts"):
        op.drop_table("billing_carts")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
