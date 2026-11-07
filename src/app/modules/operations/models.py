from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class OperationalEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "operational_events"

    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    dedupe_key: Mapped[str | None] = mapped_column(String(240))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_operational_events_workspace_created", OperationalEvent.workspace_id, OperationalEvent.created_at)
Index("ix_operational_events_severity_created", OperationalEvent.severity, OperationalEvent.created_at)
Index("ix_operational_events_type_created", OperationalEvent.event_type, OperationalEvent.created_at)
Index("ix_operational_events_dedupe_created", OperationalEvent.dedupe_key, OperationalEvent.created_at)
