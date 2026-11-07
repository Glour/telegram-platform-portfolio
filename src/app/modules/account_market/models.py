from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class AccountMarketOffer(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_market_offers"

    seller_workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    proxy_id: Mapped[str | None] = mapped_column(ForeignKey("proxies.id"))
    offer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_rub: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    module_codes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    target_segments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    warmup_profile: Mapped[str | None] = mapped_column(String(80))
    warmup_days: Mapped[int | None] = mapped_column(Integer)
    warmup_score: Mapped[int | None] = mapped_column(Integer)
    health_score: Mapped[int | None] = mapped_column(Integer)
    quality_label: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str | None] = mapped_column(String(8))
    proxy_country: Mapped[str | None] = mapped_column(String(8))
    proxy_class: Mapped[str | None] = mapped_column(String(32))
    includes_proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ready_to_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stock_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sort_rank: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AccountMarketRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_market_requests"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    seller_workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    offer_id: Mapped[str] = mapped_column(ForeignKey("account_market_offers.id"), nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    payment_order_id: Mapped[str | None] = mapped_column(ForeignKey("payment_orders.id"))
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    contact: Mapped[str | None] = mapped_column(String(255))
    message: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    module_code: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str | None] = mapped_column(String(40))
    amount_rub: Mapped[int | None] = mapped_column(Integer)
    admin_note: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


Index(
    "ix_account_market_status_rank_created",
    AccountMarketOffer.status,
    AccountMarketOffer.sort_rank,
    AccountMarketOffer.created_at,
)
Index("ix_account_market_seller_status", AccountMarketOffer.seller_workspace_id, AccountMarketOffer.status)
Index("ix_account_market_offer_type_status", AccountMarketOffer.offer_type, AccountMarketOffer.status)
Index("ix_account_market_requests_workspace_status", AccountMarketRequest.workspace_id, AccountMarketRequest.status)
Index(
    "ix_account_market_requests_seller_status",
    AccountMarketRequest.seller_workspace_id,
    AccountMarketRequest.status,
)
Index("ix_account_market_requests_offer_created", AccountMarketRequest.offer_id, AccountMarketRequest.created_at)
Index("ix_account_market_requests_payment_order", AccountMarketRequest.payment_order_id)
