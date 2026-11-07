from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class AuditLog(IdMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(80))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict | None] = mapped_column(JSON)


Index("ix_audit_logs_workspace_created", AuditLog.workspace_id, AuditLog.created_at)
