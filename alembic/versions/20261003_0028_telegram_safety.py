"""Fail-closed account safety, pending login holds and operation ledger."""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20261003_0028"
down_revision: str | None = "20260930_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hold_columns(default: bool) -> list[sa.Column]:
    return [
        sa.Column("manual_hold", sa.Boolean(), nullable=False, server_default=sa.true() if default else sa.false()),
        sa.Column("reason", sa.String(255)),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("wait_seconds", sa.Integer()),
        sa.Column("wait_until", sa.DateTime(timezone=True)),
    ]


def upgrade() -> None:
    states = op.create_table(
        "telegram_safety_states",
        sa.Column(
            "account_id", sa.String(36), sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("fingerprint", sa.String(64), unique=True),
        *_hold_columns(True),
        sa.Column("restriction_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("restriction_checked_at", sa.DateTime(timezone=True)),
        sa.Column("operator_approved_at", sa.DateTime(timezone=True)),
        sa.Column("scope", sa.String(255)),
        sa.Column("operation_id", sa.String(36)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("evidence", sa.Text()),
        sa.Column("actor_id", sa.String(36)),
        sa.Column("outbound_attempt_limit", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rpc_attempt_limit", sa.Integer(), nullable=False, server_default="20"),
    )
    op.create_table(
        "telegram_login_safety",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        *_hold_columns(False),
    )
    op.create_table(
        "telegram_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("telegram_accounts.id"), nullable=False),
        sa.Column("method", sa.String(255), nullable=False),
        sa.Column("outbound", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target", sa.Text()),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("random_id", sa.BigInteger()),
        sa.Column("message_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(255)),
        sa.Column("wait_seconds", sa.Integer()),
        sa.Column("idempotency_key", sa.String(255), unique=True),
    )
    op.create_index("ix_telegram_operations_account_created", "telegram_operations", ["account_id", "created_at"])

    # Historical timestamps do not attest to an unrestricted Telegram baseline.
    accounts = sa.table(
        "telegram_accounts",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("daily_limits", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    for account in connection.execute(sa.select(accounts)).mappings():
        limits = account["daily_limits"] if isinstance(account["daily_limits"], dict) else {}
        metadata = limits.get("restriction_status")
        metadata = metadata if isinstance(metadata, dict) else {}
        restriction = metadata.get("status")
        if restriction not in {"limited", "blocked", "frozen"}:
            restriction = {"spam_blocked": "limited", "blocked": "blocked", "frozen": "frozen"}.get(
                account["status"],
                "unknown",
            )
        connection.execute(
            states.insert().values(
                account_id=account["id"],
                manual_hold=True,
                restriction_status=restriction,
                reason="historical_account_requires_operator_admission",
                first_seen_at=account["created_at"] or datetime.now(UTC),
            )
        )


def downgrade() -> None:
    op.drop_index("ix_telegram_operations_account_created", table_name="telegram_operations")
    op.drop_table("telegram_operations")
    op.drop_table("telegram_login_safety")
    op.drop_table("telegram_safety_states")
