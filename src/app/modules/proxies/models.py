from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class Proxy(IdMixin, TimestampMixin, Base):
    __tablename__ = "proxies"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_username: Mapped[str | None] = mapped_column(Text)
    encrypted_password: Mapped[str | None] = mapped_column(Text)
    encrypted_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str | None] = mapped_column(String(120))
    city_state: Mapped[str | None] = mapped_column(String(120))
    proxy_class: Mapped[str | None] = mapped_column(String(32))
    exclusivity: Mapped[str | None] = mapped_column(String(32))
    sticky_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip_detected: Mapped[str | None] = mapped_column(String(80))
    last_country_detected: Mapped[str | None] = mapped_column(String(8))
    telegram_connectivity_status: Mapped[str | None] = mapped_column(String(32))
    max_accounts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class ProxyCheck(IdMixin, TimestampMixin, Base):
    __tablename__ = "proxy_checks"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    proxy_id: Mapped[str] = mapped_column(ForeignKey("proxies.id"), nullable=False)
    telegram_account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    telegram_latency_ms: Mapped[int | None] = mapped_column(Integer)
    ip_detected: Mapped[str | None] = mapped_column(String(80))
    geo_country: Mapped[str | None] = mapped_column(String(8))
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)


Index("ix_proxies_workspace_status", Proxy.workspace_id, Proxy.status)
Index("ix_proxy_checks_proxy_created", ProxyCheck.proxy_id, ProxyCheck.created_at)
Index("ix_proxy_checks_account_created", ProxyCheck.telegram_account_id, ProxyCheck.created_at)
