from sqlalchemy import JSON, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class PaymentOrder(IdMixin, TimestampMixin, Base):
    __tablename__ = "payment_orders"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    product_code: Mapped[str] = mapped_column(String(80), nullable=False)
    product_name: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(160))
    checkout_url: Mapped[str | None] = mapped_column(String(1000))
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BillingCart(IdMixin, TimestampMixin, Base):
    __tablename__ = "billing_carts"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", name="uq_billing_carts_user_workspace"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    items: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="yookassa", nullable=False)


Index("ix_billing_carts_user_workspace", BillingCart.user_id, BillingCart.workspace_id)

Index("ix_payment_orders_workspace_created", PaymentOrder.workspace_id, PaymentOrder.created_at)
Index("ix_payment_orders_provider_payment", PaymentOrder.provider, PaymentOrder.provider_payment_id)
Index("ix_payment_orders_status_created", PaymentOrder.status, PaymentOrder.created_at)
