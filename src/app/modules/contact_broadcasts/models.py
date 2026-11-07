from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class ContactBroadcastSnapshot(IdMixin, TimestampMixin, Base):
    __tablename__ = "contact_broadcast_snapshots"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), unique=True)


class ContactBroadcastMedia(IdMixin, TimestampMixin, Base):
    __tablename__ = "contact_broadcast_media"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(80), nullable=False)
    content_type: Mapped[str] = mapped_column(String(30), nullable=False)
