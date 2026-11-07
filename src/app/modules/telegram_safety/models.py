from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    false,
    func,
    inspect,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramSafetyState(Base):
    __tablename__ = "telegram_safety_states"

    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    manual_hold: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_seconds: Mapped[int | None] = mapped_column(Integer)
    wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restriction_status: Mapped[str] = mapped_column(String(32), default="unknown", server_default="unknown")
    restriction_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str | None] = mapped_column(String(255))
    operation_id: Mapped[str | None] = mapped_column(String(36))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    evidence: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    outbound_attempt_limit: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    rpc_attempt_limit: Mapped[int] = mapped_column(Integer, default=20, server_default="20", nullable=False)


class TelegramLoginSafety(Base):
    __tablename__ = "telegram_login_safety"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    manual_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_seconds: Mapped[int | None] = mapped_column(Integer)
    wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramOperation(Base):
    __tablename__ = "telegram_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("telegram_accounts.id"), nullable=False)
    method: Mapped[str] = mapped_column(String(255), nullable=False)
    outbound: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending", nullable=False)
    random_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(255))
    wait_seconds: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)


Index("ix_telegram_operations_account_created", TelegramOperation.account_id, TelegramOperation.created_at)


@event.listens_for(TelegramSafetyState, "before_update")
def _preserve_first_seen(_mapper: object, _connection: object, state: TelegramSafetyState) -> None:
    if inspect(state).attrs.first_seen_at.history.has_changes():
        raise ValueError("first_seen_at is immutable historical metadata, not a verified clear baseline")


SafetyState = TelegramSafetyState
Operation = TelegramOperation
