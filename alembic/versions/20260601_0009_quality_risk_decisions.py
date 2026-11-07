"""quality checks and risk decisions

Revision ID: 20260601_0009
Revises: 20260601_0008
Create Date: 2026-06-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260601_0009"
down_revision: str | None = "20260601_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("account_quality_checks"):
        op.create_table(
            "account_quality_checks",
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("rating", sa.Float(), nullable=True),
            sa.Column("potential_30d", sa.Float(), nullable=True),
            sa.Column("label", sa.String(length=32), nullable=True),
            sa.Column("potential_label", sa.String(length=32), nullable=True),
            sa.Column(
                "raw",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("risk_decisions"):
        op.create_table(
            "risk_decisions",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("module", sa.String(length=80), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column(
                "reasons",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column(
                "limits",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    indexes = {
        "ix_quality_checks_account_created": ("account_quality_checks", ["account_id", "created_at"]),
        "ix_risk_decisions_workspace_created": ("risk_decisions", ["workspace_id", "created_at"]),
        "ix_risk_decisions_account_created": ("risk_decisions", ["account_id", "created_at"]),
        "ix_risk_decisions_module_decision": ("risk_decisions", ["module", "decision"]),
    }
    for index_name, (table_name, columns) in indexes.items():
        if not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    for index_name, table_name in [
        ("ix_risk_decisions_module_decision", "risk_decisions"),
        ("ix_risk_decisions_account_created", "risk_decisions"),
        ("ix_risk_decisions_workspace_created", "risk_decisions"),
        ("ix_quality_checks_account_created", "account_quality_checks"),
    ]:
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
    for table_name in ["risk_decisions", "account_quality_checks"]:
        if _table_exists(table_name):
            op.drop_table(table_name)


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
