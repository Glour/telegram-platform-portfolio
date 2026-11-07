"""account trust rating foundation"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_0015"
down_revision: str | None = "20260605_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not _table_exists("account_trust_snapshots"):
        op.create_table(
            "account_trust_snapshots",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("telegram_account_id", sa.String(), nullable=False),
            sa.Column("score_100", sa.Integer(), nullable=False),
            sa.Column("score_10", sa.Float(), nullable=False),
            sa.Column("label", sa.String(length=32), nullable=False),
            sa.Column("confidence", sa.String(length=32), nullable=False),
            sa.Column("safety_status", sa.String(length=32), nullable=False),
            sa.Column("survival_7d", sa.Float(), nullable=False),
            sa.Column("survival_30d", sa.Float(), nullable=False),
            sa.Column("median_lifespan_days", sa.Integer(), nullable=True),
            sa.Column("similar_sample_size", sa.Integer(), nullable=False),
            sa.Column("factor_scores", sa.JSON(), nullable=False),
            sa.Column("risk_factors", sa.JSON(), nullable=False),
            sa.Column("positive_factors", sa.JSON(), nullable=False),
            sa.Column("hard_blocks", sa.JSON(), nullable=False),
            sa.Column("recommendations", sa.JSON(), nullable=False),
            sa.Column("source_version", sa.String(length=32), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["telegram_account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _index_exists("account_trust_snapshots", "ix_account_trust_snapshots_account_created"):
        op.create_index(
            "ix_account_trust_snapshots_account_created",
            "account_trust_snapshots",
            ["telegram_account_id", "created_at"],
        )
    if not _index_exists("account_trust_snapshots", "ix_account_trust_snapshots_workspace_created"):
        op.create_index(
            "ix_account_trust_snapshots_workspace_created",
            "account_trust_snapshots",
            ["workspace_id", "created_at"],
        )

    if not _table_exists("account_risk_factors"):
        op.create_table(
            "account_risk_factors",
            sa.Column("workspace_id", sa.String(), nullable=False),
            sa.Column("telegram_account_id", sa.String(), nullable=False),
            sa.Column("factor_key", sa.String(length=120), nullable=False),
            sa.Column("factor_group", sa.String(length=80), nullable=False),
            sa.Column("severity", sa.String(length=32), nullable=False),
            sa.Column("score_delta", sa.Integer(), nullable=False),
            sa.Column("ttl_until", sa.String(length=40), nullable=True),
            sa.Column("source_type", sa.String(length=80), nullable=True),
            sa.Column("source_id", sa.String(length=160), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["telegram_account_id"], ["telegram_accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _index_exists("account_risk_factors", "ix_account_risk_factors_account_created"):
        op.create_index(
            "ix_account_risk_factors_account_created",
            "account_risk_factors",
            ["telegram_account_id", "created_at"],
        )
    if not _index_exists("account_risk_factors", "ix_account_risk_factors_key_created"):
        op.create_index(
            "ix_account_risk_factors_key_created",
            "account_risk_factors",
            ["factor_key", "created_at"],
        )


def downgrade() -> None:
    if _index_exists("account_risk_factors", "ix_account_risk_factors_key_created"):
        op.drop_index("ix_account_risk_factors_key_created", table_name="account_risk_factors")
    if _index_exists("account_risk_factors", "ix_account_risk_factors_account_created"):
        op.drop_index("ix_account_risk_factors_account_created", table_name="account_risk_factors")
    if _table_exists("account_risk_factors"):
        op.drop_table("account_risk_factors")
    if _index_exists("account_trust_snapshots", "ix_account_trust_snapshots_workspace_created"):
        op.drop_index("ix_account_trust_snapshots_workspace_created", table_name="account_trust_snapshots")
    if _index_exists("account_trust_snapshots", "ix_account_trust_snapshots_account_created"):
        op.drop_index("ix_account_trust_snapshots_account_created", table_name="account_trust_snapshots")
    if _table_exists("account_trust_snapshots"):
        op.drop_table("account_trust_snapshots")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))
