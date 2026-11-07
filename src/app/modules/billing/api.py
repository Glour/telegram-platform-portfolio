from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.errors import not_found
from app.modules.billing.models import PaymentOrder
from app.modules.billing.schemas import (
    CartCheckoutCreate,
    CartItemUpdate,
    CartResponse,
    CartUpdate,
    CheckoutCreate,
    CheckoutResponse,
    PaymentOrderResponse,
)
from app.modules.billing.service import (
    add_cart_item,
    cart_summary,
    create_checkout,
    create_checkout_from_cart,
    get_or_create_cart,
    handle_nowpayments_ipn,
    handle_yookassa_webhook,
    remove_cart_item,
    update_cart,
)

router = APIRouter()


@router.get("/cart", response_model=CartResponse)
async def get_cart(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CartResponse:
    cart = await get_or_create_cart(session, workspace_id=workspace_id, user_id=current_user.id)
    await session.commit()
    return CartResponse(**cart_summary(cart))


@router.put("/cart", response_model=CartResponse)
async def put_cart(
    payload: CartUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CartResponse:
    cart = await update_cart(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        items=payload.items,
        provider=payload.provider,
    )
    await session.commit()
    return CartResponse(**cart_summary(cart))


@router.post("/cart/items", response_model=CartResponse, status_code=201)
async def post_cart_item(
    payload: CartItemUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CartResponse:
    cart = await add_cart_item(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        product_code=payload.product_code,
    )
    await session.commit()
    return CartResponse(**cart_summary(cart))


@router.delete("/cart/items/{product_code}", response_model=CartResponse)
async def delete_cart_item(
    product_code: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CartResponse:
    cart = await remove_cart_item(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        product_code=product_code,
    )
    await session.commit()
    return CartResponse(**cart_summary(cart))


@router.delete("/cart", response_model=CartResponse)
async def clear_cart(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CartResponse:
    cart = await update_cart(session, workspace_id=workspace_id, user_id=current_user.id, items=[])
    await session.commit()
    return CartResponse(**cart_summary(cart))


@router.post("/cart/checkout", response_model=CheckoutResponse, status_code=201)
async def checkout_cart(
    payload: CartCheckoutCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CheckoutResponse:
    order = await create_checkout_from_cart(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        provider=payload.provider,
        payment_method=payload.payment_method,
    )
    await session.commit()
    await session.refresh(order)
    return _checkout_response(order)


@router.post("/checkout", response_model=CheckoutResponse, status_code=201)
async def checkout(
    payload: CheckoutCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CheckoutResponse:
    order = await create_checkout(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        product_code=payload.product_code,
        items=payload.items,
        provider=payload.provider,
        payment_method=payload.payment_method,
    )
    await session.commit()
    await session.refresh(order)
    return _checkout_response(order)


@router.get("/orders/{order_id}", response_model=PaymentOrderResponse)
async def get_order(order_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> PaymentOrderResponse:
    order = await session.get(PaymentOrder, order_id)
    if not order or order.workspace_id != workspace_id:
        raise not_found("Платеж не найден.")
    return _order_response(order)


@router.get("/orders", response_model=list[PaymentOrderResponse])
async def list_orders(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[PaymentOrderResponse]:
    result = await session.execute(
        select(PaymentOrder).where(PaymentOrder.workspace_id == workspace_id).order_by(PaymentOrder.created_at.desc())
    )
    return [_order_response(order) for order in result.scalars().all()]


@router.post("/webhooks/yookassa")
async def yookassa_webhook(request: Request, session: SessionDep) -> dict:
    payload = await request.json()
    order = await handle_yookassa_webhook(session, payload)
    await session.commit()
    return {"ok": True, "order_id": order.id if order else None}


@router.post("/webhooks/nowpayments")
async def nowpayments_webhook(
    request: Request,
    session: SessionDep,
    x_nowpayments_sig: str | None = Header(default=None),
) -> dict:
    payload = await request.json()
    order = await handle_nowpayments_ipn(session, payload, x_nowpayments_sig)
    await session.commit()
    return {"ok": True, "order_id": order.id if order else None}


def _checkout_response(order: PaymentOrder) -> CheckoutResponse:
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


def _order_response(order: PaymentOrder) -> PaymentOrderResponse:
    return PaymentOrderResponse(
        id=order.id,
        provider=order.provider,
        product_code=order.product_code,
        product_name=order.product_name,
        amount=float(order.amount),
        currency=order.currency,
        status=order.status,
        checkout_url=order.checkout_url,
        provider_payment_id=order.provider_payment_id,
    )
