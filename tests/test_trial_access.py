from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.api.entitlements import require_entitlement
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.auth.service import (
    create_workspace_for_user,
    ensure_workspace_entitlements,
    expire_due_workspace_access_periods,
    expire_due_workspace_trials,
    expire_workspace_trial_if_needed,
)
from app.modules.billing.models import PaymentOrder
from app.modules.billing.products import get_billing_product
from app.modules.billing.service import (
    activate_paid_order,
    filter_payable_cart_items,
    reconcile_pending_payment_orders,
)
from app.modules.plans.catalog import FREE_PLAN_CODE, TRIAL_PLAN_CODE
from app.modules.plans.models import Entitlement, Plan, Subscription


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_user_workspace_gets_seven_day_trial(session: AsyncSession) -> None:
    user = User(email="trial@example.com", password_hash="hash")
    session.add(user)
    await session.flush()

    workspace = await create_workspace_for_user(session, user, "Trial")
    trial_plan = await session.get(Plan, workspace.plan_id)
    subscription = (
        await session.execute(select(Subscription).where(Subscription.workspace_id == workspace.id))
    ).scalar_one()
    entitlements = list(
        (await session.execute(select(Entitlement).where(Entitlement.workspace_id == workspace.id))).scalars()
    )

    assert trial_plan is not None
    assert trial_plan.code == TRIAL_PLAN_CODE
    assert subscription.status == "trialing"
    assert subscription.provider == "trial"
    assert subscription.current_period_end is not None
    assert subscription.current_period_end - subscription.current_period_start >= timedelta(days=6, hours=23)
    assert workspace.subscription_id == subscription.id
    assert {item.feature_code for item in entitlements if item.limit_value > 0} >= {
        "module.neurocommenting",
        "module.content_factory",
        "module.warmup",
    }


@pytest.mark.asyncio
async def test_trial_entitlement_expires_to_free_plan(session: AsyncSession) -> None:
    user = User(email="expired@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Expired")
    subscription = (
        await session.execute(select(Subscription).where(Subscription.workspace_id == workspace.id))
    ).scalar_one()
    subscription.current_period_end = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()

    dependency = require_entitlement("module.neurocommenting").dependency
    assert dependency is not None

    with pytest.raises(HTTPException) as exc:
        await dependency(session=session, workspace_id=workspace.id)

    await session.refresh(workspace)
    free_plan = await session.get(Plan, workspace.plan_id)
    await session.refresh(subscription)
    neurocommenting = (
        await session.execute(
            select(Entitlement).where(
                Entitlement.workspace_id == workspace.id,
                Entitlement.feature_code == "module.neurocommenting",
            )
        )
    ).scalar_one_or_none()

    assert exc.value.status_code == 402
    assert free_plan is not None
    assert free_plan.code == FREE_PLAN_CODE
    assert subscription.status == "expired"
    assert neurocommenting is None or neurocommenting.limit_value == 0


@pytest.mark.asyncio
async def test_active_trial_allows_paid_module_entitlement(session: AsyncSession) -> None:
    user = User(email="active@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Active")

    dependency = require_entitlement("module.neurocommenting").dependency
    assert dependency is not None

    await dependency(session=session, workspace_id=workspace.id)
    await expire_workspace_trial_if_needed(session, workspace.id)
    trial_plan = await session.get(Plan, workspace.plan_id)

    assert trial_plan is not None
    assert trial_plan.code == TRIAL_PLAN_CODE


@pytest.mark.asyncio
async def test_trial_workspace_can_checkout_paid_plan_upgrade(session: AsyncSession) -> None:
    user = User(email="trial-upgrade@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Trial Upgrade")

    assert await filter_payable_cart_items(session, workspace.id, ["start"]) == ["start"]

    start_product = get_billing_product("start")
    assert start_product is not None
    await ensure_workspace_entitlements(session, workspace.id, start_product.limits)

    assert await filter_payable_cart_items(session, workspace.id, ["start"]) == []


@pytest.mark.asyncio
async def test_due_trials_expire_without_protected_request(session: AsyncSession) -> None:
    user = User(email="background-expiry@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Background Expiry")
    subscription = (
        await session.execute(
            select(Subscription).where(
                Subscription.workspace_id == workspace.id,
                Subscription.status == "trialing",
            )
        )
    ).scalar_one()
    subscription.current_period_end = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()

    assert await expire_due_workspace_trials(session) == [workspace.id]

    await session.refresh(workspace)
    await session.refresh(subscription)
    plan = await session.get(Plan, workspace.plan_id)
    assert plan is not None
    assert plan.code == FREE_PLAN_CODE
    assert subscription.status == "expired"


@pytest.mark.asyncio
async def test_paid_order_replaces_trial_and_links_workspace_subscription(session: AsyncSession) -> None:
    user = User(email="paid-period@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Paid Period")
    order = PaymentOrder(
        workspace_id=workspace.id,
        user_id=user.id,
        provider="yookassa",
        provider_payment_id="payment-1",
        product_code="core",
        product_name="Core",
        amount=9900,
        currency="RUB",
        status="pending",
        raw_payload={},
    )
    session.add(order)
    await session.flush()

    await activate_paid_order(session, order)

    await session.refresh(workspace)
    subscriptions = list(
        (
            await session.execute(
                select(Subscription).where(Subscription.workspace_id == workspace.id).order_by(Subscription.created_at)
            )
        )
        .scalars()
        .all()
    )
    paid_subscription = next(item for item in subscriptions if item.status == "active")
    assert order.status == "paid"
    assert workspace.subscription_id == paid_subscription.id
    assert all(item.status != "trialing" for item in subscriptions)

    paid_subscription.current_period_end = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    assert await expire_due_workspace_access_periods(session) == [workspace.id]

    await session.refresh(workspace)
    await session.refresh(paid_subscription)
    free_plan = await session.get(Plan, workspace.plan_id)
    assert free_plan is not None
    assert free_plan.code == FREE_PLAN_CODE
    assert paid_subscription.status == "expired"
    assert workspace.subscription_id is None


@pytest.mark.asyncio
async def test_reconciliation_activates_completed_yookassa_payment(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="reconcile@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = await create_workspace_for_user(session, user, "Reconciliation")
    product = get_billing_product("start")
    assert product is not None
    order = PaymentOrder(
        workspace_id=workspace.id,
        user_id=user.id,
        provider="yookassa",
        provider_payment_id="provider-payment-id",
        product_code=product.code,
        product_name=product.name,
        amount=product.amount,
        currency=product.currency,
        status="pending",
        raw_payload={},
    )
    session.add(order)
    await session.flush()

    async def completed_payment(_: str) -> dict:
        return {"id": "provider-payment-id", "paid": True, "status": "succeeded"}

    monkeypatch.setattr("app.modules.billing.service._fetch_yookassa_payment", completed_payment)
    result = await reconcile_pending_payment_orders(session)

    assert result == {"checked": 1, "paid": 1, "failed": 0, "unavailable": 0}
    assert order.status == "paid"
    assert workspace.subscription_id is not None
