from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.config import get_settings
from app.core.errors import not_found
from app.modules.account_market.models import AccountMarketOffer, AccountMarketRequest
from app.modules.account_market.schemas import (
    AccountMarketCheckoutCreate,
    AccountMarketOfferCreate,
    AccountMarketOfferResponse,
    AccountMarketOfferUpdate,
    AccountMarketRequestCreate,
    AccountMarketRequestResponse,
    AccountMarketRequestUpdate,
    MarketSort,
    OfferStatus,
)
from app.modules.account_market.service import (
    deliver_market_request,
    ensure_offer_inventory_available,
    reserve_market_offer,
)
from app.modules.billing.products import BillingProduct
from app.modules.billing.schemas import CheckoutResponse
from app.modules.billing.service import create_checkout_for_product
from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts.models import TelegramAccount

router = APIRouter()


@router.get("", response_model=list[AccountMarketOfferResponse])
async def list_offers(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
    status_filter: str | None = Query(default=None, alias="status"),
    offer_type: str | None = Query(default=None),
    module: str | None = Query(default=None),
    country: str | None = Query(default=None),
    ready_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    sort: Annotated[MarketSort, Query()] = "recommended",
) -> list[AccountMarketOfferResponse]:
    admin = await _can_manage_market(session, workspace_id, current_user)
    statement = select(AccountMarketOffer)
    if admin and status_filter:
        statement = statement.where(AccountMarketOffer.status == status_filter)
    else:
        statement = statement.where(AccountMarketOffer.status == "published")

    if offer_type:
        statement = statement.where(AccountMarketOffer.offer_type == offer_type)
    if country:
        upper_country = country.strip().upper()
        statement = statement.where(
            or_(
                AccountMarketOffer.country == upper_country,
                AccountMarketOffer.proxy_country == upper_country,
            )
        )
    if ready_only:
        statement = statement.where(AccountMarketOffer.ready_to_run.is_(True), AccountMarketOffer.stock_count > 0)
    if search:
        like = f"%{search.strip().lower()}%"
        statement = statement.where(
            or_(
                AccountMarketOffer.title.ilike(like),
                AccountMarketOffer.description.ilike(like),
                AccountMarketOffer.warmup_profile.ilike(like),
                AccountMarketOffer.quality_label.ilike(like),
            )
        )
    if module:
        module_text = module.strip()
        statement = statement.where(AccountMarketOffer.module_codes.contains([module_text]))

    statement = statement.order_by(*_market_sort(sort))
    offers = list((await session.execute(statement.limit(200))).scalars().all())
    return await _offer_responses(session, workspace_id, offers, can_manage=admin)


@router.post("", response_model=AccountMarketOfferResponse, status_code=201)
async def create_offer(
    payload: AccountMarketOfferCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> AccountMarketOfferResponse:
    await _ensure_can_manage_market(session, workspace_id, current_user)
    await _validate_inventory_links(session, workspace_id, payload.account_id, payload.proxy_id)
    offer = AccountMarketOffer(
        seller_workspace_id=workspace_id,
        created_by_user_id=current_user.id,
        **payload.model_dump(mode="json"),
    )
    _hydrate_offer_from_inventory_defaults(
        offer,
        await _get_account(session, payload.account_id),
        await _get_proxy(session, payload.proxy_id),
    )
    session.add(offer)
    await session.commit()
    await session.refresh(offer)
    return (await _offer_responses(session, workspace_id, [offer], can_manage=True))[0]


@router.patch("/{offer_id}", response_model=AccountMarketOfferResponse)
async def update_offer(
    offer_id: str,
    payload: AccountMarketOfferUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> AccountMarketOfferResponse:
    await _ensure_can_manage_market(session, workspace_id, current_user)
    offer = await _get_manageable_offer(session, workspace_id, offer_id)
    data = payload.model_dump(exclude_unset=True, mode="json")
    account_id = data.get("account_id", offer.account_id)
    proxy_id = data.get("proxy_id", offer.proxy_id)
    await _validate_inventory_links(session, workspace_id, account_id, proxy_id)
    for key, value in data.items():
        setattr(offer, key, value)
    _hydrate_offer_from_inventory_defaults(
        offer,
        await _get_account(session, offer.account_id),
        await _get_proxy(session, offer.proxy_id),
    )
    if offer.status in {"published", "reserved"}:
        await ensure_offer_inventory_available(session, offer)
    await session.commit()
    await session.refresh(offer)
    return (await _offer_responses(session, workspace_id, [offer], can_manage=True))[0]


@router.get("/requests", response_model=list[AccountMarketRequestResponse])
async def list_requests(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[AccountMarketRequestResponse]:
    admin = await _can_manage_market(session, workspace_id, current_user)
    statement = select(AccountMarketRequest)
    if not admin:
        statement = statement.where(
            AccountMarketRequest.workspace_id == workspace_id,
            AccountMarketRequest.requested_by_user_id == current_user.id,
        )
    if status_filter:
        statement = statement.where(AccountMarketRequest.status == status_filter)
    requests = list((await session.execute(statement.order_by(AccountMarketRequest.created_at.desc()))).scalars().all())
    return await _request_responses(session, workspace_id, requests, can_manage=admin)


@router.post("/{offer_id}/requests", response_model=AccountMarketRequestResponse, status_code=201)
async def create_request(
    offer_id: str,
    payload: AccountMarketRequestCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> AccountMarketRequestResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Заявки маркета теперь создаются только через оплату.",
    )


@router.post("/{offer_id}/checkout", response_model=CheckoutResponse, status_code=201)
async def checkout_offer(
    offer_id: str,
    payload: AccountMarketCheckoutCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CheckoutResponse:
    offer = await session.get(AccountMarketOffer, offer_id)
    if not offer or offer.status != "published" or offer.stock_count <= 0:
        raise not_found("Market offer not found")
    await reserve_market_offer(session, offer, payload.quantity)
    amount = offer.price_rub * payload.quantity
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Цена оффера должна быть больше нуля.")
    request = AccountMarketRequest(
        workspace_id=workspace_id,
        seller_workspace_id=offer.seller_workspace_id,
        offer_id=offer.id,
        requested_by_user_id=current_user.id,
        status="payment_pending",
        contact=payload.contact,
        message=payload.message,
        quantity=payload.quantity,
        module_code=payload.module_code,
        provider=payload.provider,
        amount_rub=amount,
        snapshot=_offer_snapshot(offer),
    )
    session.add(request)
    await session.flush()
    product = BillingProduct(
        code=f"market.offer.{offer.id}",
        name=f"Маркет аккаунтов: {offer.title}",
        amount=amount,
        currency=offer.currency or "RUB",
        limits={},
        plan_name="Маркет аккаунтов",
        duration_days=1,
    )
    order = await create_checkout_for_product(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        product=product,
        provider=payload.provider,
        payment_method=payload.payment_method,
        return_path="/dashboard/account_market",
        raw_payload={
            "kind": "account_market",
            "market_request_id": request.id,
            "market_offer_id": offer.id,
            "seller_workspace_id": offer.seller_workspace_id,
            "quantity": payload.quantity,
        },
    )
    request.payment_order_id = order.id
    request.snapshot = {**request.snapshot, "payment_order_id": order.id}
    await session.commit()
    await session.refresh(order)
    return CheckoutResponse(
        order_id=order.id,
        provider=order.provider,
        status=order.status,
        checkout_url=order.checkout_url or "",
        amount=float(order.amount),
        currency=order.currency,
        product_code=order.product_code,
        product_name=order.product_name,
    )


@router.patch("/requests/{request_id}", response_model=AccountMarketRequestResponse)
async def update_request(
    request_id: str,
    payload: AccountMarketRequestUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> AccountMarketRequestResponse:
    await _ensure_can_manage_market(session, workspace_id, current_user)
    request = await _get_market_request(session, workspace_id, request_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"]:
        next_status = data["status"]
        offer = await session.get(AccountMarketOffer, request.offer_id)
        if offer:
            if next_status == "reserved":
                request.status = next_status
                await ensure_offer_inventory_available(session, offer)
                offer.status = "reserved"
            elif next_status == "completed":
                if request.payment_order_id and not request.paid_at:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Нельзя выдать покупку до подтверждения оплаты.",
                    )
                request.status = next_status
                await deliver_market_request(session, request, offer)
            elif next_status == "cancelled" and offer.status == "reserved":
                request.status = next_status
                offer.status = "published"
            else:
                request.status = next_status
    if "admin_note" in data:
        request.admin_note = data["admin_note"]
    await session.commit()
    await session.refresh(request)
    return (await _request_responses(session, workspace_id, [request], can_manage=True))[0]


@router.post("/{offer_id}/{next_status}", response_model=AccountMarketOfferResponse)
async def change_offer_status(
    offer_id: str,
    next_status: OfferStatus,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> AccountMarketOfferResponse:
    await _ensure_can_manage_market(session, workspace_id, current_user)
    if next_status not in {"draft", "published", "reserved", "sold", "archived"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported status")
    offer = await _get_manageable_offer(session, workspace_id, offer_id)
    if next_status in {"published", "reserved"}:
        await ensure_offer_inventory_available(session, offer)
    offer.status = next_status
    await session.commit()
    await session.refresh(offer)
    return (await _offer_responses(session, workspace_id, [offer], can_manage=True))[0]


async def _can_manage_market(session: AsyncSession, workspace_id: str, current_user) -> bool:
    configured = get_settings().market_admin_emails.split(",")
    allowed_emails = {email.strip().lower() for email in configured if email.strip()}
    return current_user.email.strip().lower() in allowed_emails


async def _ensure_can_manage_market(session: AsyncSession, workspace_id: str, current_user) -> None:
    if not await _can_manage_market(session, workspace_id, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform market admin can manage market offers",
        )


async def _get_manageable_offer(session: AsyncSession, workspace_id: str, offer_id: str) -> AccountMarketOffer:
    offer = await session.get(AccountMarketOffer, offer_id)
    if not offer:
        raise not_found("Market offer not found")
    return offer


async def _get_market_request(session: AsyncSession, workspace_id: str, request_id: str) -> AccountMarketRequest:
    request = await session.get(AccountMarketRequest, request_id)
    if not request:
        raise not_found("Market request not found")
    return request


async def _validate_inventory_links(
    session: AsyncSession,
    workspace_id: str,
    account_id: str | None,
    proxy_id: str | None,
) -> None:
    account = await _get_account(session, account_id)
    if account and account.workspace_id != workspace_id:
        raise not_found("Telegram account not found")
    proxy = await _get_proxy(session, proxy_id)
    if proxy and proxy.workspace_id != workspace_id:
        raise not_found("Proxy not found")


async def _get_account(session: AsyncSession, account_id: str | None) -> TelegramAccount | None:
    return await session.get(TelegramAccount, account_id) if account_id else None


async def _get_proxy(session: AsyncSession, proxy_id: str | None) -> Proxy | None:
    return await session.get(Proxy, proxy_id) if proxy_id else None


def _hydrate_offer_from_inventory_defaults(
    offer: AccountMarketOffer,
    account: TelegramAccount | None,
    proxy: Proxy | None,
) -> None:
    if account:
        offer.health_score = offer.health_score if offer.health_score is not None else account.health_score
        offer.warmup_score = offer.warmup_score if offer.warmup_score is not None else account.warmup_score
        if not offer.warmup_profile:
            offer.warmup_profile = account.warmup_status
    if proxy:
        offer.includes_proxy = True
        offer.proxy_country = offer.proxy_country or proxy.country
        offer.proxy_class = offer.proxy_class or proxy.proxy_class


async def _offer_responses(
    session: AsyncSession,
    workspace_id: str,
    offers: list[AccountMarketOffer],
    *,
    can_manage: bool,
) -> list[AccountMarketOfferResponse]:
    account_ids = [offer.account_id for offer in offers if offer.account_id]
    proxy_ids = [offer.proxy_id for offer in offers if offer.proxy_id]
    accounts = {}
    proxies = {}
    if account_ids:
        result = await session.execute(select(TelegramAccount).where(TelegramAccount.id.in_(account_ids)))
        accounts = {account.id: account for account in result.scalars().all()}
    if proxy_ids:
        result = await session.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
        proxies = {proxy.id: proxy for proxy in result.scalars().all()}

    responses: list[AccountMarketOfferResponse] = []
    for offer in offers:
        account = accounts.get(offer.account_id or "")
        proxy = proxies.get(offer.proxy_id or "")
        manageable = can_manage
        responses.append(
            AccountMarketOfferResponse.model_validate(offer).model_copy(
                update={
                    "account_label": _account_label(account),
                    "account_username": account.username if account else None,
                    "proxy_label": _proxy_label(proxy),
                    "can_manage": manageable,
                }
            )
        )
    return responses


async def _request_responses(
    session: AsyncSession,
    workspace_id: str,
    requests: list[AccountMarketRequest],
    *,
    can_manage: bool,
) -> list[AccountMarketRequestResponse]:
    offer_ids = [request.offer_id for request in requests]
    offers_by_id: dict[str, AccountMarketOfferResponse] = {}
    if offer_ids:
        result = await session.execute(select(AccountMarketOffer).where(AccountMarketOffer.id.in_(offer_ids)))
        offers = list(result.scalars().all())
        offers_by_id = {
            offer.id: response
            for offer, response in zip(
                offers,
                await _offer_responses(session, workspace_id, offers, can_manage=can_manage),
                strict=False,
            )
        }
    return [
        AccountMarketRequestResponse.model_validate(request).model_copy(
            update={"offer": offers_by_id.get(request.offer_id)}
        )
        for request in requests
    ]


def _account_label(account: TelegramAccount | None) -> str | None:
    if not account:
        return None
    return account.display_name or account.username or account.phone or account.id[:8]


def _proxy_label(proxy: Proxy | None) -> str | None:
    if not proxy:
        return None
    geo = " ".join(part for part in [proxy.country, proxy.city_state] if part)
    return f"{proxy.type}://{proxy.host}:{proxy.port}" + (f" · {geo}" if geo else "")


def _market_sort(sort: MarketSort):
    if sort == "newest":
        return (AccountMarketOffer.created_at.desc(),)
    if sort == "price_asc":
        return (AccountMarketOffer.price_rub.asc(), AccountMarketOffer.sort_rank.asc())
    if sort == "price_desc":
        return (AccountMarketOffer.price_rub.desc(), AccountMarketOffer.sort_rank.asc())
    if sort == "warmup_desc":
        return (AccountMarketOffer.warmup_score.desc().nullslast(), AccountMarketOffer.sort_rank.asc())
    if sort == "health_desc":
        return (AccountMarketOffer.health_score.desc().nullslast(), AccountMarketOffer.sort_rank.asc())
    return (AccountMarketOffer.sort_rank.asc(), AccountMarketOffer.created_at.desc())


def _offer_snapshot(offer: AccountMarketOffer) -> dict:
    return {
        "offer_id": offer.id,
        "title": offer.title,
        "offer_type": offer.offer_type,
        "price_rub": offer.price_rub,
        "currency": offer.currency,
        "module_codes": offer.module_codes,
        "target_segments": offer.target_segments,
        "tags": offer.tags,
        "warmup_score": offer.warmup_score,
        "health_score": offer.health_score,
        "includes_proxy": offer.includes_proxy,
    }
