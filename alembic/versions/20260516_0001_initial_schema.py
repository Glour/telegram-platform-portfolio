"""initial schema

Revision ID: 20260516_0001
Revises:
Create Date: 2026-05-16
"""

from collections.abc import Sequence

from alembic import op

from app.db import import_models  # noqa: F401
from app.db.base import Base

revision: str = "20260516_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
