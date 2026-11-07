"""saas auth and module entitlements

Revision ID: 20260518_0004
Revises: 20260517_0003
Create Date: 2026-05-18
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.modules.plans.catalog import internal_mvp_limits

revision: str = "20260518_0004"
down_revision: str | None = "20260517_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if _table_exists("users") and not _column_exists("users", "email_verified_at"):
        op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    if _should_alter_last_login_at():
        op.alter_column(
            "users",
            "last_login_at",
            existing_type=sa.String(length=64),
            type_=sa.DateTime(timezone=True),
            existing_nullable=True,
            postgresql_using="NULLIF(last_login_at, '')::timestamptz",
        )

    if not _table_exists("auth_tokens"):
        op.create_table(
            "auth_tokens",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("purpose", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_auth_tokens_user_id_users")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_tokens")),
            sa.UniqueConstraint("token_hash", name=op.f("uq_auth_tokens_token_hash")),
        )
    if not _index_exists("auth_tokens", "ix_auth_tokens_user_purpose"):
        op.create_index(
            "ix_auth_tokens_user_purpose",
            "auth_tokens",
            ["user_id", "purpose", "consumed_at"],
            unique=False,
        )

    if not _table_exists("oauth_accounts"):
        op.create_table(
            "oauth_accounts",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("provider_user_id", sa.String(length=160), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column(
                "raw_profile",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_oauth_accounts_user_id_users")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_accounts")),
            sa.UniqueConstraint("provider", "provider_user_id", name="uq_oauth_accounts_provider_user"),
        )
    if not _index_exists("oauth_accounts", "ix_oauth_accounts_user_provider"):
        op.create_index("ix_oauth_accounts_user_provider", "oauth_accounts", ["user_id", "provider"], unique=False)

    if not _table_exists("oauth_states"):
        op.create_table(
            "oauth_states",
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("state_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_states")),
            sa.UniqueConstraint("state_hash", name=op.f("uq_oauth_states_state_hash")),
        )
    if not _index_exists("oauth_states", "ix_oauth_states_provider_consumed"):
        op.create_index("ix_oauth_states_provider_consumed", "oauth_states", ["provider", "consumed_at"], unique=False)

    _mark_existing_users_verified()
    _seed_module_entitlements()


def downgrade() -> None:
    if _index_exists("oauth_states", "ix_oauth_states_provider_consumed"):
        op.drop_index("ix_oauth_states_provider_consumed", table_name="oauth_states")
    if _table_exists("oauth_states"):
        op.drop_table("oauth_states")
    if _index_exists("oauth_accounts", "ix_oauth_accounts_user_provider"):
        op.drop_index("ix_oauth_accounts_user_provider", table_name="oauth_accounts")
    if _table_exists("oauth_accounts"):
        op.drop_table("oauth_accounts")
    if _index_exists("auth_tokens", "ix_auth_tokens_user_purpose"):
        op.drop_index("ix_auth_tokens_user_purpose", table_name="auth_tokens")
    if _table_exists("auth_tokens"):
        op.drop_table("auth_tokens")
    if op.get_bind().dialect.name != "sqlite" and _column_exists("users", "last_login_at"):
        op.alter_column(
            "users",
            "last_login_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
    if _column_exists("users", "email_verified_at"):
        op.drop_column("users", "email_verified_at")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column.get("name") == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index.get("name") == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _should_alter_last_login_at() -> bool:
    if op.get_bind().dialect.name == "sqlite" or not _column_exists("users", "last_login_at"):
        return False
    column = next(
        column for column in sa.inspect(op.get_bind()).get_columns("users") if column.get("name") == "last_login_at"
    )
    return isinstance(column.get("type"), sa.String)


def _mark_existing_users_verified() -> None:
    if _column_exists("users", "email_verified_at"):
        op.get_bind().execute(
            sa.text("UPDATE users SET email_verified_at = COALESCE(email_verified_at, :verified_at)"),
            {"verified_at": datetime.now(UTC)},
        )


def _seed_module_entitlements() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    limits = internal_mvp_limits()

    plans = bind.execute(sa.text("SELECT id, code, limits, features FROM plans")).mappings().all()
    for plan in plans:
        if plan["code"] != "internal_mvp":
            continue
        merged_limits = {**limits, **(plan["limits"] or {})}
        for feature_code, value in limits.items():
            merged_limits[feature_code] = max(int(merged_limits.get(feature_code, 0) or 0), value)
        merged_features = {**(plan["features"] or {}), "modular_billing": True}
        if bind.dialect.name == "postgresql":
            statement = sa.text(
                "UPDATE plans SET limits = CAST(:limits AS jsonb), features = CAST(:features AS jsonb) WHERE id = :id"
            )
        else:
            statement = sa.text("UPDATE plans SET limits = :limits, features = :features WHERE id = :id")
        bind.execute(
            statement,
            {
                "id": plan["id"],
                "limits": json.dumps(merged_limits),
                "features": json.dumps(merged_features),
            },
        )

    workspaces = bind.execute(sa.text("SELECT id, plan_id FROM workspaces")).mappings().all()
    for workspace in workspaces:
        plan = (
            bind.execute(sa.text("SELECT limits FROM plans WHERE id = :id"), {"id": workspace["plan_id"]})
            .mappings()
            .first()
        )
        plan_limits = (plan["limits"] if plan else None) or limits
        for feature_code, limit_value in plan_limits.items():
            exists = bind.execute(
                sa.text(
                    "SELECT 1 FROM entitlements WHERE workspace_id = :workspace_id AND feature_code = :feature_code"
                ),
                {"workspace_id": workspace["id"], "feature_code": feature_code},
            ).first()
            if exists:
                continue
            bind.execute(
                sa.text(
                    """
                    INSERT INTO entitlements
                        (id, workspace_id, feature_code, limit_value, used_value,
                         reset_period, resets_at, created_at, updated_at)
                    VALUES
                        (:id, :workspace_id, :feature_code, :limit_value, 0, 'month', NULL, :created_at, :updated_at)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "workspace_id": workspace["id"],
                    "feature_code": feature_code,
                    "limit_value": int(limit_value),
                    "created_at": now,
                    "updated_at": now,
                },
            )
