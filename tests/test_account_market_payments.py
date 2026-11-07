from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.account_market.models import AccountMarketOffer, AccountMarketRequest
from app.modules.account_market.service import deliver_market_request
from app.modules.auth.models import User
from app.modules.billing.models import PaymentOrder
from app.modules.billing.service import activate_paid_order, mark_order_failed, release_failed_order
from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.workspaces.models import Workspace


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _market_fixture(session: AsyncSession) -> tuple[Workspace, Workspace, User, AccountMarketOffer]:
    seller = User(email="market-admin@example.com", password_hash="hash", status="active")
    buyer = User(email="buyer@example.com", password_hash="hash", status="active")
    session.add_all([seller, buyer])
    await session.flush()

    seller_workspace = Workspace(owner_user_id=seller.id, name="Platform stock", slug="platform-stock")
    buyer_workspace = Workspace(owner_user_id=buyer.id, name="Buyer", slug="buyer")
    session.add_all([seller_workspace, buyer_workspace])
    await session.flush()

    proxy = Proxy(
        workspace_id=seller_workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        country="DE",
    )
    account = TelegramAccount(
        workspace_id=seller_workspace.id,
        username="ready_account",
        display_name="Ready Account",
        status="active",
        import_type="session",
        proxy_required=True,
        health_score=90,
        warmup_score=85,
        warmup_status="ready",
    )
    session.add_all([proxy, account])
    await session.flush()
    account.proxy_id = proxy.id

    offer = AccountMarketOffer(
        seller_workspace_id=seller_workspace.id,
        created_by_user_id=seller.id,
        account_id=account.id,
        proxy_id=proxy.id,
        offer_type="bundle",
        status="reserved",
        title="Ready bundle",
        description="Account with proxy",
        price_rub=9900,
        currency="RUB",
        module_codes=["module.neurocommenting"],
        target_segments=["comments"],
        tags=["ready"],
        warmup_profile="ready",
        warmup_days=14,
        warmup_score=85,
        health_score=90,
        quality_label="trusted",
        country="RU",
        proxy_country="DE",
        proxy_class="static_residential",
        includes_proxy=True,
        ready_to_run=True,
        stock_count=1,
        sort_rank=100,
        metadata_json={},
    )
    session.add(offer)
    await session.flush()
    return seller_workspace, buyer_workspace, buyer, offer


async def _market_order(
    session: AsyncSession,
    buyer_workspace: Workspace,
    buyer: User,
    offer: AccountMarketOffer,
    *,
    status: str = "payment_pending",
) -> tuple[AccountMarketRequest, PaymentOrder]:
    order = PaymentOrder(
        workspace_id=buyer_workspace.id,
        user_id=buyer.id,
        provider="yookassa",
        product_code=f"market.offer.{offer.id}",
        product_name=f"Маркет аккаунтов: {offer.title}",
        amount=offer.price_rub,
        currency="RUB",
        status="pending",
        provider_payment_id="provider-payment-id",
        checkout_url="https://pay.example/checkout",
        raw_payload={"kind": "account_market", "market_offer_id": offer.id},
    )
    session.add(order)
    await session.flush()
    request = AccountMarketRequest(
        workspace_id=buyer_workspace.id,
        seller_workspace_id=offer.seller_workspace_id,
        offer_id=offer.id,
        requested_by_user_id=buyer.id,
        payment_order_id=order.id,
        status=status,
        contact="@buyer",
        quantity=1,
        module_code="module.neurocommenting",
        provider="yookassa",
        amount_rub=offer.price_rub,
        snapshot={"offer_id": offer.id, "title": offer.title},
        delivery_metadata={},
    )
    session.add(request)
    await session.flush()
    return request, order


@pytest.mark.asyncio
async def test_paid_market_order_transfers_bundle_to_buyer_workspace(session: AsyncSession) -> None:
    seller_workspace, buyer_workspace, buyer, offer = await _market_fixture(session)
    request, order = await _market_order(session, buyer_workspace, buyer, offer)

    await activate_paid_order(session, order)
    await session.refresh(request)
    await session.refresh(order)
    await session.refresh(offer)
    account = await session.get(TelegramAccount, offer.account_id)
    proxy = await session.get(Proxy, offer.proxy_id)

    assert order.status == "paid"
    assert request.status == "completed"
    assert request.paid_at is not None
    assert request.delivered_at is not None
    assert request.delivery_metadata["buyer_workspace_id"] == buyer_workspace.id
    assert request.delivery_metadata["seller_workspace_id"] == seller_workspace.id
    assert account is not None
    assert account.workspace_id == buyer_workspace.id
    assert account.proxy_id == proxy.id
    assert proxy is not None
    assert proxy.workspace_id == buyer_workspace.id
    assert offer.status == "sold"
    assert offer.stock_count == 0


@pytest.mark.asyncio
async def test_paid_market_order_activation_is_idempotent(session: AsyncSession) -> None:
    _, buyer_workspace, buyer, offer = await _market_fixture(session)
    request, order = await _market_order(session, buyer_workspace, buyer, offer)

    await activate_paid_order(session, order)
    first_delivered_at = request.delivered_at
    await activate_paid_order(session, order)
    await session.refresh(request)
    await session.refresh(offer)

    assert request.status == "completed"
    assert first_delivered_at is not None
    assert request.delivered_at is not None
    assert request.delivered_at.replace(tzinfo=None) == first_delivered_at.replace(tzinfo=None)
    assert offer.stock_count == 0


@pytest.mark.asyncio
async def test_failed_market_payment_releases_reserved_offer(session: AsyncSession) -> None:
    _, buyer_workspace, buyer, offer = await _market_fixture(session)
    request, order = await _market_order(session, buyer_workspace, buyer, offer)

    await release_failed_order(session, order)
    await session.refresh(request)
    await session.refresh(offer)

    assert request.status == "cancelled"
    assert request.delivered_at is None
    assert request.admin_note == "Платеж не подтвержден, резерв снят автоматически."
    assert offer.status == "published"
    assert offer.stock_count == 1


@pytest.mark.asyncio
async def test_unpaid_market_request_cannot_be_delivered(session: AsyncSession) -> None:
    _, buyer_workspace, buyer, offer = await _market_fixture(session)
    request, _ = await _market_order(session, buyer_workspace, buyer, offer)

    with pytest.raises(HTTPException) as exc_info:
        await deliver_market_request(session, request, offer)

    assert exc_info.value.status_code == 409
    assert request.delivered_at is None


@pytest.mark.asyncio
async def test_late_failed_webhook_does_not_revert_paid_market_order(session: AsyncSession) -> None:
    _, buyer_workspace, buyer, offer = await _market_fixture(session)
    request, order = await _market_order(session, buyer_workspace, buyer, offer)

    await activate_paid_order(session, order)
    await mark_order_failed(session, order)
    await session.refresh(order)
    await session.refresh(request)
    await session.refresh(offer)

    assert order.status == "paid"
    assert request.status == "completed"
    assert request.delivered_at is not None
    assert offer.status == "sold"
    assert offer.stock_count == 0
