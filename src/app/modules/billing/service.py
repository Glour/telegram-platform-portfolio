from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import bad_request
from app.modules.auth.models import User
from app.modules.auth.service import ensure_workspace_entitlements
from app.modules.billing.models import BillingCart, PaymentOrder
from app.modules.billing.products import (
    BillingProduct,
    build_cart_product,
    get_billing_product,
    normalize_product_codes,
    product_from_dict,
    product_to_dict,
)
from app.modules.operations.service import record_operational_event
from app.modules.plans.models import Entitlement, Plan, Subscription
from app.modules.workspaces.models import Workspace


def configured_providers() -> dict[str, bool]:
    settings = get_settings()
    return {
        "yookassa": bool(settings.yookassa_shop_id and settings.yookassa_secret_key),
        "nowpayments": bool(settings.nowpayments_api_key and settings.nowpayments_ipn_secret),
    }


async def create_checkout(
    session: AsyncSession,
    workspace_id: str,
    user_id: str,
    product_code: str | None,
    provider: str,
    payment_method: str | None = None,
    items: list[str] | None = None,
) -> PaymentOrder:
    try:
        requested_items = items or ([product_code] if product_code else [])
        payable_items = await filter_payable_cart_items(session, workspace_id, requested_items)
        if not payable_items:
            raise bad_request("Все выбранные товары уже подключены к этому кабинету.")
        product = build_cart_product(payable_items) if len(payable_items) > 1 else get_billing_product(payable_items[0])
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if not product:
        raise bad_request("Этот тариф или модуль пока недоступен для оплаты.")

    order = await create_checkout_for_product(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        product=product,
        provider=provider,
        payment_method=payment_method,
    )
    if product.code.startswith("cart."):
        order.raw_payload["cart_product"] = product_to_dict(product)
    await session.flush()
    return order


async def create_checkout_for_product(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    product: BillingProduct,
    provider: str,
    payment_method: str | None = None,
    raw_payload: dict | None = None,
    return_path: str = "/dashboard/billing",
) -> PaymentOrder:
    providers = configured_providers()
    if not providers.get(provider):
        raise bad_request("Провайдер оплаты еще не настроен.")

    order = PaymentOrder(
        workspace_id=workspace_id,
        user_id=user_id,
        provider=provider,
        product_code=product.code,
        product_name=product.name,
        amount=product.amount,
        currency=product.currency,
        status="pending",
        raw_payload=raw_payload or {},
    )
    session.add(order)
    await session.flush()
    user = await session.get(User, user_id)

    if provider == "yookassa":
        payload = await _create_yookassa_payment(
            order,
            product,
            payment_method,
            customer_email=user.email if user else None,
            return_path=return_path,
        )
    else:
        payload = await _create_nowpayments_invoice(order, product, return_path=return_path)

    order.provider_payment_id = _provider_payment_id(provider, payload)
    order.checkout_url = _checkout_url(provider, payload)
    previous_raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    order.raw_payload = {**previous_raw, "provider_payload": payload}
    await _record_billing_event(
        session,
        order,
        event_type="checkout_created",
        title="Создан платеж",
        message=f"Создан checkout {provider} для продукта {product.code}.",
        payload={"amount": str(product.amount), "currency": product.currency, "product_code": product.code},
    )
    await session.flush()
    return order


async def handle_yookassa_webhook(session: AsyncSession, payload: dict) -> PaymentOrder | None:
    payment_id = str((payload.get("object") or {}).get("id") or payload.get("id") or "")
    if not payment_id:
        return None

    order = await _find_order(session, "yookassa", payment_id)
    if not order:
        return None

    previous_raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    payment = await _fetch_yookassa_payment(payment_id)
    paid = bool(payment.get("paid")) and payment.get("status") == "succeeded"
    order.raw_payload = {**previous_raw, "webhook": payload, "verified_payment": payment}
    if previous_raw.get("cart_product"):
        order.raw_payload["cart_product"] = previous_raw["cart_product"]
    if paid:
        await activate_paid_order(session, order)
    elif payment.get("status") in {"canceled", "failed"}:
        await mark_order_failed(session, order)
    await session.flush()
    return order


async def handle_nowpayments_ipn(session: AsyncSession, payload: dict, signature: str | None) -> PaymentOrder | None:
    if not _valid_nowpayments_signature(payload, signature):
        raise bad_request("Некорректная подпись crypto webhook.")

    payment_id = str(payload.get("payment_id") or payload.get("invoice_id") or "")
    order_id = str(payload.get("order_id") or "")
    order = await _find_order(session, "nowpayments", payment_id)
    if not order and order_id:
        order = await session.get(PaymentOrder, order_id)
    if not order:
        return None

    previous_raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    status = str(payload.get("payment_status") or "").lower()
    order.raw_payload = {**previous_raw, "ipn": payload}
    if status in {"finished", "confirmed", "sending"}:
        await activate_paid_order(session, order)
    elif status in {"failed", "refunded", "expired"}:
        await mark_order_failed(session, order)
    await session.flush()
    return order


async def activate_paid_order(session: AsyncSession, order: PaymentOrder) -> None:
    if order.status == "paid":
        return

    raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    if raw_payload.get("kind") == "account_market":
        from app.modules.account_market.service import activate_paid_market_order

        await activate_paid_market_order(session, order.id)
        order.status = "paid"
        await session.flush()
        return

    product = get_billing_product(order.product_code)
    if not product:
        product = product_from_dict(raw_payload.get("cart_product"))
    if not product:
        order.status = "failed"
        return

    plan = await _ensure_plan(session, product)
    workspace = await session.get(Workspace, order.workspace_id)
    if workspace:
        workspace.plan_id = plan.id
    await ensure_workspace_entitlements(session, order.workspace_id, product.limits)

    now = datetime.now(UTC)
    subscription = Subscription(
        workspace_id=order.workspace_id,
        plan_id=plan.id,
        provider=order.provider,
        provider_subscription_id=order.provider_payment_id,
        status="active",
        current_period_start=now,
        current_period_end=now + timedelta(days=product.duration_days),
    )
    session.add(subscription)
    await session.flush()
    if workspace:
        workspace.subscription_id = subscription.id
    trial_result = await session.execute(
        select(Subscription).where(
            Subscription.workspace_id == order.workspace_id,
            Subscription.status == "trialing",
        )
    )
    for trial_subscription in trial_result.scalars().all():
        trial_subscription.status = "expired"
    order.status = "paid"
    await _send_payment_confirmation(session, order, subscription.current_period_end)
    await _record_billing_event(
        session,
        order,
        event_type="payment_succeeded",
        title="Оплата подтверждена",
        message=f"Оплата {order.provider} подтверждена, доступ к {order.product_name} выдан.",
        payload={"amount": str(order.amount), "currency": order.currency, "product_code": order.product_code},
    )
    await session.flush()


async def release_failed_order(session: AsyncSession, order: PaymentOrder) -> None:
    raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    if raw_payload.get("kind") != "account_market":
        return
    from app.modules.account_market.service import release_failed_market_order

    await release_failed_market_order(session, order.id)
    await session.flush()


async def mark_order_failed(session: AsyncSession, order: PaymentOrder) -> None:
    if order.status == "paid":
        return
    await release_failed_order(session, order)
    order.status = "failed"
    await _record_billing_event(
        session,
        order,
        event_type="payment_failed",
        title="Оплата не завершена",
        message=f"Платеж {order.provider} для {order.product_name} завершился без оплаты.",
        payload={"amount": str(order.amount), "currency": order.currency, "product_code": order.product_code},
    )


async def reconcile_pending_payment_orders(session: AsyncSession, *, limit: int = 100) -> dict[str, int]:
    """Verify pending payments when a provider webhook was delayed or lost."""
    orders = list(
        (
            await session.execute(
                select(PaymentOrder)
                .where(PaymentOrder.status == "pending", PaymentOrder.provider_payment_id.is_not(None))
                .order_by(PaymentOrder.created_at.asc())
                .limit(max(1, limit))
            )
        ).scalars()
    )
    checked = paid = failed = unavailable = 0
    checked_at = datetime.now(UTC).isoformat()

    for order in orders:
        checked += 1
        try:
            if order.provider == "yookassa":
                provider_payload = await _fetch_yookassa_payment(str(order.provider_payment_id))
                provider_status = str(provider_payload.get("status") or "").lower()
                is_paid = bool(provider_payload.get("paid")) and provider_status == "succeeded"
                is_failed = provider_status in {"canceled", "failed"}
            elif order.provider == "nowpayments":
                # Hosted invoices return an invoice id, while NOWPayments status lookup requires
                # a payment id created after the customer selects a currency. The signed IPN
                # includes the merchant order_id and is therefore the reliable settlement path.
                provider_payload = {"state": "awaiting_signed_ipn", "invoice_id": order.provider_payment_id}
                provider_status = "awaiting_ipn"
                is_paid = is_failed = False
                unavailable += 1
            else:
                unavailable += 1
                continue
        except httpx.HTTPError as exc:
            provider_payload = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            provider_status = ""
            is_paid = is_failed = False
            unavailable += 1

        raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
        order.raw_payload = {
            **raw_payload,
            "reconciliation": {
                "checked_at": checked_at,
                "provider_status": provider_status or None,
                "provider_payload": provider_payload,
            },
        }
        if is_paid:
            await activate_paid_order(session, order)
            paid += 1
        elif is_failed:
            await mark_order_failed(session, order)
            failed += 1

    await session.flush()
    return {"checked": checked, "paid": paid, "failed": failed, "unavailable": unavailable}


async def _ensure_plan(session: AsyncSession, product: BillingProduct) -> Plan:
    plan_code = f"paid_{product.code.replace('.', '_')}"
    result = await session.execute(select(Plan).where(Plan.code == plan_code))
    plan = result.scalar_one_or_none()
    features = {"paid": True, "source_product_code": product.code, "duration_days": product.duration_days}
    if plan:
        plan.name = product.plan_name
        plan.status = "active"
        plan.monthly_price = product.amount if product.duration_days <= 31 else 0
        plan.yearly_price = product.amount if product.duration_days > 31 else product.yearly_amount
        plan.currency = product.currency
        plan.limits = product.limits
        plan.features = features
        return plan

    plan = Plan(
        code=plan_code,
        name=product.plan_name,
        status="active",
        monthly_price=product.amount if product.duration_days <= 31 else 0,
        yearly_price=product.amount if product.duration_days > 31 else product.yearly_amount,
        currency=product.currency,
        limits=product.limits,
        features=features,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _create_yookassa_payment(
    order: PaymentOrder,
    product: BillingProduct,
    payment_method: str | None,
    *,
    customer_email: str | None = None,
    return_path: str = "/dashboard/billing",
) -> dict:
    settings = get_settings()
    return_url = f"{settings.frontend_base_url.rstrip('/')}{return_path}?order_id={order.id}"
    payload: dict = {
        "amount": {"value": f"{product.amount:.2f}", "currency": product.currency},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": f"demo-platform telegram agents: {product.name}",
        "metadata": {
            "order_id": order.id,
            "workspace_id": order.workspace_id,
            "product_code": product.code,
        },
    }
    if payment_method == "sbp":
        payload["payment_method_data"] = {"type": "sbp"}
    if customer_email:
        payload["receipt"] = {
            "customer": {"email": customer_email},
            "items": [
                {
                    "description": product.name[:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{product.amount:.2f}", "currency": product.currency},
                    "vat_code": settings.yookassa_vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.yookassa_api_url.rstrip('/')}/payments",
            auth=(settings.yookassa_shop_id or "", settings.yookassa_secret_key or ""),
            headers={"Idempotence-Key": str(uuid4())},
            json=payload,
        )
        if response.status_code >= 400:
            raise bad_request("ЮKassa не создала платеж. Проверьте настройки магазина.")
        return response.json()


async def _fetch_yookassa_payment(payment_id: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{settings.yookassa_api_url.rstrip('/')}/payments/{payment_id}",
            auth=(settings.yookassa_shop_id or "", settings.yookassa_secret_key or ""),
        )
        if response.status_code >= 400:
            return {}
        return response.json()


async def _create_nowpayments_invoice(
    order: PaymentOrder,
    product: BillingProduct,
    *,
    return_path: str = "/dashboard/billing",
) -> dict:
    settings = get_settings()
    webhook_base_url = (settings.payment_webhook_base_url or settings.frontend_base_url).rstrip("/")
    return_url = f"{settings.frontend_base_url.rstrip('/')}{return_path}?order_id={order.id}"
    payload = {
        "price_amount": product.amount,
        "price_currency": product.currency.lower(),
        "order_id": order.id,
        "order_description": f"demo-platform telegram agents: {product.name}",
        "ipn_callback_url": f"{webhook_base_url}/api/v1/billing/webhooks/nowpayments",
        "success_url": return_url,
        "cancel_url": f"{settings.frontend_base_url.rstrip('/')}/pricing#pricing-plans",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.nowpayments_api_url.rstrip('/')}/v1/invoice",
            headers={
                "x-api-key": settings.nowpayments_api_key or "",
                "user-agent": "Mozilla/5.0 demo-platform/1.0",
            },
            json=payload,
        )
        if response.status_code >= 400:
            raise bad_request("Crypto invoice не создан. Проверьте ключ NOWPayments.")
        return response.json()


def _provider_payment_id(provider: str, payload: dict) -> str | None:
    if provider == "yookassa":
        return payload.get("id")
    return str(payload.get("id") or payload.get("invoice_id") or "")


def _checkout_url(provider: str, payload: dict) -> str | None:
    if provider == "yookassa":
        return (payload.get("confirmation") or {}).get("confirmation_url")
    return payload.get("invoice_url")


async def _find_order(session: AsyncSession, provider: str, payment_id: str) -> PaymentOrder | None:
    result = await session.execute(
        select(PaymentOrder).where(
            PaymentOrder.provider == provider,
            PaymentOrder.provider_payment_id == payment_id,
        )
    )
    return result.scalar_one_or_none()


def _valid_nowpayments_signature(payload: dict, signature: str | None) -> bool:
    settings = get_settings()
    if not settings.nowpayments_ipn_secret:
        return True
    if not signature:
        return False
    normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    expected = hmac.new(
        settings.nowpayments_ipn_secret.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _send_payment_confirmation(
    session: AsyncSession,
    order: PaymentOrder,
    period_end: datetime | None,
) -> None:
    settings = get_settings()
    user = await session.get(User, order.user_id)
    if not user or not settings.smtp_host:
        return

    period_text = period_end.astimezone(UTC).strftime("%d.%m.%Y") if period_end else "без ограничения"
    message = EmailMessage()
    message["Subject"] = "Оплата demo-platform подтверждена"
    message["From"] = settings.auth_email_from
    message["To"] = user.email
    message.set_content(
        "\n".join(
            [
                "Оплата подтверждена.",
                "",
                f"Продукт: {order.product_name}",
                f"Сумма: {order.amount} {order.currency}",
                f"Доступ действует до: {period_text}",
                f"Кабинет: {settings.frontend_base_url.rstrip('/')}/dashboard/billing",
                "",
                "Автоматических списаний нет. Следующий период оплачивается отдельно.",
            ]
        )
    )

    def _send() -> None:
        smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)

    try:
        await asyncio.to_thread(_send)
    except Exception as exc:  # Payment access must not be rolled back by email delivery.
        raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
        order.raw_payload = {**raw_payload, "confirmation_email_error": f"{type(exc).__name__}: {exc}"[:500]}


async def _record_billing_event(
    session: AsyncSession,
    order: PaymentOrder,
    *,
    event_type: str,
    title: str,
    message: str,
    payload: dict,
) -> None:
    await record_operational_event(
        session,
        workspace_id=order.workspace_id,
        severity="info",
        category="billing",
        event_type=event_type,
        title=title,
        message=message,
        source="billing.service",
        entity_type="payment_order",
        entity_id=order.id,
        payload={"provider": order.provider, "payment_order_id": order.id, **payload},
    )


def normalize_cart_items(items: list[str] | None) -> list[str]:
    raw_codes = [str(raw or "").strip() for raw in items or []]
    for code in raw_codes:
        if code and not get_billing_product(code):
            raise bad_request("Один из товаров пока недоступен для оплаты.")
    return normalize_product_codes(raw_codes)


async def _enabled_feature_limits(session: AsyncSession, workspace_id: str) -> dict[str, int]:
    result = await session.execute(
        select(Entitlement.feature_code, Entitlement.limit_value).where(
            Entitlement.workspace_id == workspace_id,
            Entitlement.limit_value > 0,
        )
    )
    return {str(feature_code): int(limit_value or 0) for feature_code, limit_value in result.all()}


def _product_limits_already_covered(product: BillingProduct, enabled_limits: dict[str, int]) -> bool:
    if not product.limits:
        return False
    return all(
        enabled_limits.get(str(feature_code), 0) >= int(limit_value)
        for feature_code, limit_value in product.limits.items()
    )


async def filter_payable_cart_items(
    session: AsyncSession,
    workspace_id: str,
    items: list[str] | None,
) -> list[str]:
    normalized = normalize_cart_items(items)
    if not normalized:
        return []
    enabled_limits = await _enabled_feature_limits(session, workspace_id)
    payable: list[str] = []
    for code in normalized:
        product = get_billing_product(code)
        if product and _product_limits_already_covered(product, enabled_limits):
            continue
        payable.append(code)
    return payable


async def get_or_create_cart(session: AsyncSession, *, workspace_id: str, user_id: str) -> BillingCart:
    result = await session.execute(
        select(BillingCart).where(BillingCart.workspace_id == workspace_id, BillingCart.user_id == user_id)
    )
    cart = result.scalar_one_or_none()
    if cart:
        return cart
    cart = BillingCart(workspace_id=workspace_id, user_id=user_id, items=[], provider="yookassa")
    session.add(cart)
    await session.flush()
    return cart


async def update_cart(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    items: list[str],
    provider: str = "yookassa",
) -> BillingCart:
    cart = await get_or_create_cart(session, workspace_id=workspace_id, user_id=user_id)
    cart.items = await filter_payable_cart_items(session, workspace_id, items)
    cart.provider = provider
    await session.flush()
    return cart


async def add_cart_item(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    product_code: str,
) -> BillingCart:
    cart = await get_or_create_cart(session, workspace_id=workspace_id, user_id=user_id)
    items = await filter_payable_cart_items(session, workspace_id, [*(cart.items or []), product_code])
    cart.items = items
    await session.flush()
    return cart


async def remove_cart_item(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    product_code: str,
) -> BillingCart:
    cart = await get_or_create_cart(session, workspace_id=workspace_id, user_id=user_id)
    cart.items = [item for item in normalize_cart_items(cart.items or []) if item != product_code]
    await session.flush()
    return cart


def cart_summary(cart: BillingCart) -> dict:
    items = normalize_cart_items(cart.items or [])
    products = [get_billing_product(code) for code in items]
    valid_products = [product for product in products if product]
    currency = valid_products[0].currency if valid_products else "RUB"
    return {
        "user_id": cart.user_id,
        "workspace_id": cart.workspace_id,
        "items": items,
        "provider": cart.provider or "yookassa",
        "products": [
            {
                "code": product.code,
                "name": product.name,
                "amount": float(product.amount),
                "currency": product.currency,
                "duration_days": product.duration_days,
            }
            for product in valid_products
        ],
        "total": float(sum(product.amount for product in valid_products)),
        "currency": currency,
    }


async def create_checkout_from_cart(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    provider: str,
    payment_method: str | None = None,
) -> PaymentOrder:
    cart = await get_or_create_cart(session, workspace_id=workspace_id, user_id=user_id)
    items = await filter_payable_cart_items(session, workspace_id, cart.items or [])
    if not items:
        raise bad_request("Корзина пустая.")
    cart.items = items
    cart.provider = provider
    return await create_checkout(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        product_code=None,
        items=items,
        provider=provider,
        payment_method=payment_method,
    )
