"""Add 7 day trial plan."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

from app.modules.plans.catalog import TRIAL_PLAN_CODE, trial_plan_limits

revision: str = "20260616_0025"
down_revision: str | None = "20260613_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    limits = trial_plan_limits()
    features = {
        "api_access": True,
        "modular_billing": True,
        "trial": True,
        "trial_days": 7,
    }
    existing = (
        bind.execute(
            sa.text("SELECT id FROM plans WHERE code = :code"),
            {"code": TRIAL_PLAN_CODE},
        )
        .mappings()
        .first()
    )

    if bind.dialect.name == "postgresql":
        limits_expr = "CAST(:limits AS jsonb)"
        features_expr = "CAST(:features AS jsonb)"
    else:
        limits_expr = ":limits"
        features_expr = ":features"

    if existing:
        bind.execute(
            sa.text(
                f"""
                UPDATE plans
                SET name = :name,
                    status = 'active',
                    monthly_price = 0,
                    yearly_price = 0,
                    currency = 'RUB',
                    limits = {limits_expr},
                    features = {features_expr},
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                "id": existing["id"],
                "name": "Пробный доступ",
                "limits": json.dumps(limits),
                "features": json.dumps(features),
                "updated_at": now,
            },
        )
        return

    bind.execute(
        sa.text(
            f"""
            INSERT INTO plans
                (id, code, name, status, monthly_price, yearly_price, currency,
                 limits, features, created_at, updated_at)
            VALUES
                (:id, :code, :name, 'active', 0, 0, 'RUB',
                 {limits_expr}, {features_expr}, :created_at, :updated_at)
            """
        ),
        {
            "id": str(uuid4()),
            "code": TRIAL_PLAN_CODE,
            "name": "Пробный доступ",
            "limits": json.dumps(limits),
            "features": json.dumps(features),
            "created_at": now,
            "updated_at": now,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM plans WHERE code = :code"), {"code": TRIAL_PLAN_CODE})
