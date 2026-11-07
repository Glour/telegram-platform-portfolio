from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found
from app.modules.account_market.models import AccountMarketOffer, AccountMarketRequest
from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts.models import TelegramAccount


async def ensure_offer_inventory_available(session: AsyncSession, offer: AccountMarketOffer) -> None:
    conflict_conditions = []
    if offer.account_id:
        conflict_conditions.append(AccountMarketOffer.account_id == offer.account_id)
    if offer.proxy_id:
        conflict_conditions.append(AccountMarketOffer.proxy_id == offer.proxy_id)
    if not conflict_conditions:
        return
    result = await session.execute(
        select(AccountMarketOffer)
        .where(
            AccountMarketOffer.id != offer.id,
            AccountMarketOffer.status.in_(["published", "reserved"]),
            or_(*conflict_conditions),
        )
        .limit(1)
    )
    conflict = result.scalar_one_or_none()
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот аккаунт или прокси уже опубликован в другом активном оффере.",
        )


async def reserve_market_offer(session: AsyncSession, offer: AccountMarketOffer, quantity: int) -> None:
    if offer.status != "published" or offer.stock_count < quantity or quantity <= 0:
        raise not_found("Market offer not found")
    await ensure_offer_inventory_available(session, offer)
    offer.status = "reserved"


async def deliver_market_request(
    session: AsyncSession,
    request: AccountMarketRequest,
    offer: AccountMarketOffer,
) -> None:
    if request.delivered_at:
        return
    if request.status == "payment_pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя выдать сборку до подтверждения оплаты.",
        )
    delivered: dict[str, str] = {}
    if offer.proxy_id:
        proxy = await session.get(Proxy, offer.proxy_id)
        if proxy and proxy.workspace_id == offer.seller_workspace_id:
            proxy.workspace_id = request.workspace_id
            delivered["proxy_id"] = proxy.id
    if offer.account_id:
        account = await session.get(TelegramAccount, offer.account_id)
        if account and account.workspace_id == offer.seller_workspace_id:
            account.workspace_id = request.workspace_id
            delivered["account_id"] = account.id
            if offer.proxy_id:
                account.proxy_id = offer.proxy_id
    request.delivered_at = datetime.now(UTC)
    request.delivery_metadata = {
        "mode": "workspace_transfer",
        "buyer_workspace_id": request.workspace_id,
        "seller_workspace_id": offer.seller_workspace_id,
        **delivered,
    }
    offer.stock_count = max(0, offer.stock_count - request.quantity)
    if offer.account_id or offer.proxy_id or offer.stock_count <= 0:
        offer.status = "sold"
    else:
        offer.status = "published"


async def activate_paid_market_order(session: AsyncSession, payment_order_id: str) -> AccountMarketRequest | None:
    result = await session.execute(
        select(AccountMarketRequest).where(AccountMarketRequest.payment_order_id == payment_order_id)
    )
    request = result.scalar_one_or_none()
    if not request:
        return None
    offer = await session.get(AccountMarketOffer, request.offer_id)
    if not offer:
        return None
    now = datetime.now(UTC)
    request.paid_at = request.paid_at or now
    request.status = "paid"
    await deliver_market_request(session, request, offer)
    request.status = "completed"
    return request


async def release_failed_market_order(session: AsyncSession, payment_order_id: str) -> AccountMarketRequest | None:
    result = await session.execute(
        select(AccountMarketRequest).where(AccountMarketRequest.payment_order_id == payment_order_id)
    )
    request = result.scalar_one_or_none()
    if not request or request.delivered_at:
        return request
    offer = await session.get(AccountMarketOffer, request.offer_id)
    request.status = "cancelled"
    request.admin_note = request.admin_note or "Платеж не подтвержден, резерв снят автоматически."
    if offer and offer.status == "reserved":
        offer.status = "published"
    return request
