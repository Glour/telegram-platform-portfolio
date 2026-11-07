from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.modules.auth.service import (
    ensure_free_plan,
    ensure_internal_full_access_for_user,
    expire_workspace_trial_if_needed,
    is_internal_full_access_user,
    sync_workspace_entitlements,
)
from app.modules.plans.catalog import BUSINESS_MODEL, FULL_ACCESS_PRODUCT, MODULE_PRODUCTS
from app.modules.plans.models import Entitlement, Plan, Subscription, UsageEvent
from app.modules.workspaces.models import Workspace

router = APIRouter()


@router.get("")
async def list_plans(session: SessionDep) -> dict:
    result = await session.execute(select(Plan).where(Plan.status == "active").order_by(Plan.monthly_price))
    return {"data": [plan for plan in result.scalars().all()]}


@router.get("/entitlements")
async def entitlements(session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    result = await session.execute(
        select(Entitlement).where(Entitlement.workspace_id == workspace_id).order_by(Entitlement.feature_code)
    )
    return {"data": list(result.scalars().all())}


@router.get("/access")
async def access(session: SessionDep, workspace_id: WorkspaceIdDep, current_user: CurrentUserDep) -> dict:
    await ensure_internal_full_access_for_user(session, current_user)
    await expire_workspace_trial_if_needed(session, workspace_id)
    workspace = await session.get(Workspace, workspace_id)
    plan = await session.get(Plan, workspace.plan_id) if workspace and workspace.plan_id else None
    if workspace and not is_internal_full_access_user(current_user) and (not plan or plan.code == "free"):
        plan = await ensure_free_plan(session)
        workspace.plan_id = plan.id
        await sync_workspace_entitlements(session, workspace.id, plan.limits or {})
    await session.commit()
    entitlement_result = await session.execute(
        select(Entitlement).where(Entitlement.workspace_id == workspace_id).order_by(Entitlement.feature_code)
    )
    subscription_result = await session.execute(
        select(Subscription).where(Subscription.workspace_id == workspace_id).order_by(Subscription.created_at.desc())
    )
    subscriptions_list = list(subscription_result.scalars().all())
    plan_ids = {item.plan_id for item in subscriptions_list}
    if workspace and workspace.plan_id:
        plan_ids.add(workspace.plan_id)
    plan_result = await session.execute(select(Plan).where(Plan.id.in_(plan_ids))) if plan_ids else None
    plans_by_id = {item.id: item for item in plan_result.scalars().all()} if plan_result else {}
    entitlements_list = list(entitlement_result.scalars().all())
    enabled_features = {entitlement.feature_code for entitlement in entitlements_list if entitlement.limit_value > 0}
    return {
        "plan": _plan_response(plan) if plan else None,
        "subscriptions": [_subscription_response(item) for item in subscriptions_list],
        "entitlements": [_entitlement_response(item) for item in entitlements_list],
        "enabled_features": sorted(enabled_features),
        "feature_periods": _feature_periods(subscriptions_list, plans_by_id),
        "plan_period": _plan_period(workspace.plan_id if workspace else None, subscriptions_list, plans_by_id),
        "modules": MODULE_PRODUCTS,
        "full_access_product": FULL_ACCESS_PRODUCT,
        "business_model": BUSINESS_MODEL,
    }


def _feature_periods(subscriptions: list[Subscription], plans_by_id: dict[str, Plan]) -> dict:
    now = datetime.now(UTC)
    periods: dict[str, dict] = {}
    for subscription in subscriptions:
        if subscription.status not in {"active", "trialing"}:
            continue
        if subscription.current_period_end and subscription.current_period_end <= now:
            continue
        plan = plans_by_id.get(subscription.plan_id)
        if not plan:
            continue
        for feature_code, limit_value in (plan.limits or {}).items():
            if int(limit_value or 0) <= 0:
                continue
            current = periods.get(feature_code)
            current_end = current.get("current_period_end") if current else None
            if current_end and subscription.current_period_end and current_end >= subscription.current_period_end:
                continue
            periods[feature_code] = {
                "subscription_id": subscription.id,
                "plan_id": plan.id,
                "plan_code": plan.code,
                "plan_name": plan.name,
                "product_code": (plan.features or {}).get("source_product_code") or plan.code,
                "current_period_start": subscription.current_period_start,
                "current_period_end": subscription.current_period_end,
                "remaining_seconds": _remaining_seconds(subscription.current_period_end, now),
            }
    return periods


def _plan_period(
    workspace_plan_id: str | None,
    subscriptions: list[Subscription],
    plans_by_id: dict[str, Plan],
) -> dict | None:
    if not workspace_plan_id:
        return None
    now = datetime.now(UTC)
    matching = [
        subscription
        for subscription in subscriptions
        if subscription.plan_id == workspace_plan_id
        and subscription.status in {"active", "trialing"}
        and (not subscription.current_period_end or subscription.current_period_end > now)
    ]
    if not matching:
        return None
    subscription = max(matching, key=lambda item: item.current_period_end or datetime.max.replace(tzinfo=UTC))
    plan = plans_by_id.get(workspace_plan_id)
    return {
        "subscription_id": subscription.id,
        "plan_id": workspace_plan_id,
        "plan_code": plan.code if plan else None,
        "plan_name": plan.name if plan else None,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "remaining_seconds": _remaining_seconds(subscription.current_period_end, now),
    }


def _remaining_seconds(period_end: datetime | None, now: datetime) -> int | None:
    if not period_end:
        return None
    return max(0, int((period_end - now).total_seconds()))


def _plan_response(plan: Plan) -> dict:
    return {
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
        "status": plan.status,
        "monthly_price": float(plan.monthly_price or 0),
        "yearly_price": float(plan.yearly_price or 0),
        "currency": plan.currency,
        "limits": plan.limits or {},
        "features": plan.features or {},
    }


def _subscription_response(subscription: Subscription) -> dict:
    return {
        "id": subscription.id,
        "workspace_id": subscription.workspace_id,
        "plan_id": subscription.plan_id,
        "provider": subscription.provider,
        "status": subscription.status,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": subscription.cancel_at_period_end,
    }


def _entitlement_response(entitlement: Entitlement) -> dict:
    return {
        "id": entitlement.id,
        "workspace_id": entitlement.workspace_id,
        "feature_code": entitlement.feature_code,
        "limit_value": entitlement.limit_value,
        "used_value": entitlement.used_value,
        "reset_period": entitlement.reset_period,
        "resets_at": entitlement.resets_at,
    }


@router.get("/usage")
async def usage(session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    result = await session.execute(
        select(UsageEvent)
        .where(UsageEvent.workspace_id == workspace_id)
        .order_by(UsageEvent.created_at.desc())
        .limit(100)
    )
    return {"data": list(result.scalars().all())}


@router.get("/modules")
async def modules() -> dict:
    return {"data": MODULE_PRODUCTS, "full_access_product": FULL_ACCESS_PRODUCT}


@router.get("/business-model")
async def business_model() -> dict:
    return BUSINESS_MODEL
