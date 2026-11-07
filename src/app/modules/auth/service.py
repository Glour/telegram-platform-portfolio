from __future__ import annotations

import asyncio
import hashlib
import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.modules.auth.models import AuthToken, OAuthAccount, OAuthState, User
from app.modules.operations.service import record_operational_event
from app.modules.plans.catalog import (
    TRIAL_PLAN_CODE,
    apply_plan_entitlements,
    free_plan_limits,
    internal_mvp_limits,
    trial_plan_limits,
)
from app.modules.plans.models import Entitlement, Plan, Subscription
from app.modules.workspaces.models import Workspace, WorkspaceMember


class GoogleOAuthError(RuntimeError):
    pass


def slugify(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    return slug or "workspace"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_internal_plan(session: AsyncSession) -> Plan:
    result = await session.execute(select(Plan).where(Plan.code == "internal_mvp"))
    plan = result.scalar_one_or_none()
    limits = internal_mvp_limits()
    if plan:
        merged_limits = {**limits, **(plan.limits or {})}
        for feature_code, value in limits.items():
            merged_limits[feature_code] = max(int(merged_limits.get(feature_code, 0) or 0), value)
        plan.limits = merged_limits
        plan.name = "Dev/Owner Unlimited"
        plan.features = {
            **(plan.features or {}),
            "internal_unlimited": True,
            "neurocommenting": True,
            "api_access": True,
            "modular_billing": True,
        }
        return plan

    plan = Plan(
        code="internal_mvp",
        name="Dev/Owner Unlimited",
        limits=limits,
        features={
            "internal_unlimited": True,
            "neurocommenting": True,
            "api_access": True,
            "modular_billing": True,
        },
    )
    session.add(plan)
    await session.flush()
    return plan


async def ensure_free_plan(session: AsyncSession) -> Plan:
    result = await session.execute(select(Plan).where(Plan.code == "free"))
    plan = result.scalar_one_or_none()
    limits = free_plan_limits()
    features = {"api_access": True, "modular_billing": True, "free_workspace": True}
    if plan:
        plan.name = "Free"
        plan.monthly_price = 0
        plan.yearly_price = 0
        plan.currency = "RUB"
        plan.status = "active"
        plan.limits = limits
        plan.features = features
        return plan

    plan = Plan(
        code="free",
        name="Free",
        monthly_price=0,
        yearly_price=0,
        currency="RUB",
        limits=limits,
        features=features,
    )
    session.add(plan)
    await session.flush()
    return plan


async def ensure_trial_plan(session: AsyncSession) -> Plan:
    result = await session.execute(select(Plan).where(Plan.code == TRIAL_PLAN_CODE))
    plan = result.scalar_one_or_none()
    limits = trial_plan_limits()
    features = {
        "api_access": True,
        "modular_billing": True,
        "trial": True,
        "trial_days": 7,
    }
    if plan:
        plan.name = "Пробный доступ"
        plan.monthly_price = 0
        plan.yearly_price = 0
        plan.currency = "RUB"
        plan.status = "active"
        plan.limits = limits
        plan.features = features
        return plan

    plan = Plan(
        code=TRIAL_PLAN_CODE,
        name="Пробный доступ",
        monthly_price=0,
        yearly_price=0,
        currency="RUB",
        limits=limits,
        features=features,
    )
    session.add(plan)
    await session.flush()
    return plan


async def create_workspace_for_user(
    session: AsyncSession,
    user: User,
    workspace_name: str,
    plan: Plan | None = None,
    start_trial: bool = True,
) -> Workspace:
    plan = plan or (await ensure_trial_plan(session) if start_trial else await ensure_free_plan(session))
    workspace = Workspace(
        owner_user_id=user.id,
        name=workspace_name,
        slug=f"{slugify(workspace_name)}-{user.id[:8]}",
        plan_id=plan.id,
    )
    session.add(workspace)
    await session.flush()
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    for entitlement in apply_plan_entitlements(workspace.id, plan):
        session.add(entitlement)
    if start_trial and plan.code == TRIAL_PLAN_CODE:
        now = datetime.now(UTC)
        subscription = Subscription(
            workspace_id=workspace.id,
            plan_id=plan.id,
            provider="trial",
            status="trialing",
            current_period_start=now,
            current_period_end=now + timedelta(days=7),
            cancel_at_period_end=True,
        )
        session.add(subscription)
        await session.flush()
        workspace.subscription_id = subscription.id
    await session.flush()
    return workspace


async def ensure_workspace_entitlements(
    session: AsyncSession,
    workspace_id: str,
    limits: dict[str, int],
) -> None:
    existing_result = await session.execute(select(Entitlement).where(Entitlement.workspace_id == workspace_id))
    existing = {entitlement.feature_code: entitlement for entitlement in existing_result.scalars().all()}
    for feature_code, limit_value in limits.items():
        entitlement = existing.get(feature_code)
        if entitlement:
            entitlement.limit_value = max(int(entitlement.limit_value or 0), int(limit_value))
        else:
            session.add(
                Entitlement(
                    workspace_id=workspace_id,
                    feature_code=feature_code,
                    limit_value=int(limit_value),
                    reset_period="month",
                )
            )
    await session.flush()


async def sync_workspace_entitlements(
    session: AsyncSession,
    workspace_id: str,
    limits: dict[str, int],
) -> None:
    existing_result = await session.execute(select(Entitlement).where(Entitlement.workspace_id == workspace_id))
    existing = {entitlement.feature_code: entitlement for entitlement in existing_result.scalars().all()}
    normalized_limits = {str(feature_code): int(limit_value) for feature_code, limit_value in limits.items()}

    for feature_code, entitlement in existing.items():
        entitlement.limit_value = normalized_limits.get(feature_code, 0)

    for feature_code, limit_value in normalized_limits.items():
        if feature_code not in existing:
            session.add(
                Entitlement(
                    workspace_id=workspace_id,
                    feature_code=feature_code,
                    limit_value=limit_value,
                    reset_period="month",
                )
            )
    await session.flush()


async def expire_workspace_trial_if_needed(session: AsyncSession, workspace_id: str) -> None:
    workspace = await session.get(Workspace, workspace_id)
    if not workspace or not workspace.plan_id:
        return

    trial_plan = await ensure_trial_plan(session)
    if workspace.plan_id != trial_plan.id:
        return

    now = datetime.now(UTC)
    active_paid_result = await session.execute(
        select(Subscription)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.workspace_id == workspace_id,
            Subscription.status == "active",
            Plan.code != TRIAL_PLAN_CODE,
        )
    )
    active_paid = [
        subscription
        for subscription in active_paid_result.scalars().all()
        if _subscription_active_at(subscription, now)
    ]
    if active_paid:
        return

    trial_result = await session.execute(
        select(Subscription).where(
            Subscription.workspace_id == workspace_id,
            Subscription.plan_id == trial_plan.id,
            Subscription.status == "trialing",
        )
    )
    trial_subscriptions = list(trial_result.scalars().all())
    has_active_trial = any(_subscription_active_at(subscription, now) for subscription in trial_subscriptions)
    if has_active_trial:
        return

    for subscription in trial_subscriptions:
        subscription.status = "expired"

    free_plan = await ensure_free_plan(session)
    workspace.plan_id = free_plan.id
    workspace.subscription_id = None
    await sync_workspace_entitlements(session, workspace.id, free_plan.limits or {})


async def expire_due_workspace_trials(session: AsyncSession, *, limit: int = 500) -> list[str]:
    trial_plan = await ensure_trial_plan(session)
    now = datetime.now(UTC)
    result = await session.execute(
        select(Workspace.id)
        .join(
            Subscription,
            (Subscription.workspace_id == Workspace.id) & (Subscription.plan_id == trial_plan.id),
        )
        .where(
            Workspace.plan_id == trial_plan.id,
            Subscription.status == "trialing",
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end <= now,
        )
        .distinct()
        .order_by(Workspace.id)
        .limit(max(1, limit))
    )
    workspace_ids = list(result.scalars().all())
    for workspace_id in workspace_ids:
        await expire_workspace_trial_if_needed(session, workspace_id)
    return workspace_ids


async def expire_due_workspace_access_periods(session: AsyncSession, *, limit: int = 500) -> list[str]:
    now = datetime.now(UTC)
    result = await session.execute(
        select(Subscription)
        .where(
            Subscription.status.in_(("active", "trialing")),
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end <= now,
        )
        .order_by(Subscription.current_period_end, Subscription.id)
        .limit(max(1, limit))
    )
    due_subscriptions = list(result.scalars().all())
    workspace_ids = list(dict.fromkeys(item.workspace_id for item in due_subscriptions))
    for subscription in due_subscriptions:
        subscription.status = "expired"
        await record_operational_event(
            session,
            workspace_id=subscription.workspace_id,
            severity="info",
            category="billing",
            event_type="subscription_expired",
            title="Период доступа завершен",
            message="Оплаченный или тестовый период завершился, права доступа пересчитываются.",
            source="auth.service",
            entity_type="subscription",
            entity_id=subscription.id,
            payload={"provider": subscription.provider, "plan_id": subscription.plan_id},
        )

    for workspace_id in workspace_ids:
        workspace = await session.get(Workspace, workspace_id)
        if not workspace:
            continue
        active_result = await session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.workspace_id == workspace_id,
                Subscription.status.in_(("active", "trialing")),
                or_(
                    Subscription.current_period_end.is_(None),
                    Subscription.current_period_end > now,
                ),
            )
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
        active_periods = list(active_result.all())
        if not active_periods:
            free_plan = await ensure_free_plan(session)
            workspace.plan_id = free_plan.id
            workspace.subscription_id = None
            await sync_workspace_entitlements(session, workspace.id, free_plan.limits or {})
            continue

        current_subscription, current_plan = active_periods[0]
        merged_limits: dict[str, int] = {}
        for _, plan in active_periods:
            for feature_code, limit_value in (plan.limits or {}).items():
                merged_limits[str(feature_code)] = max(
                    merged_limits.get(str(feature_code), 0),
                    int(limit_value or 0),
                )
        workspace.plan_id = current_plan.id
        workspace.subscription_id = current_subscription.id
        await sync_workspace_entitlements(session, workspace.id, merged_limits)
    return workspace_ids


def _subscription_active_at(subscription: Subscription, now: datetime) -> bool:
    if not subscription.current_period_end:
        return True
    period_end = subscription.current_period_end
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return period_end > now


def internal_full_access_emails() -> set[str]:
    settings = get_settings()
    return {email.strip().lower() for email in settings.internal_full_access_emails.split(",") if email.strip()}


def is_internal_full_access_user(user: User) -> bool:
    return user.email.lower() in internal_full_access_emails()


async def ensure_internal_full_access_for_user(session: AsyncSession, user: User) -> None:
    if not is_internal_full_access_user(user):
        return

    plan = await ensure_internal_plan(session)
    result = await session.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id, WorkspaceMember.status == "active")
    )
    workspaces = list(result.scalars().all())
    limits = internal_mvp_limits()

    for workspace in workspaces:
        workspace.plan_id = plan.id
        await ensure_workspace_entitlements(session, workspace.id, limits)


async def create_email_verification_token(session: AsyncSession, user: User) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    await session.execute(
        AuthToken.__table__.update()
        .where(AuthToken.user_id == user.id)
        .where(AuthToken.purpose == "email_verification")
        .where(AuthToken.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    token = secrets.token_urlsafe(48)
    session.add(
        AuthToken(
            user_id=user.id,
            purpose="email_verification",
            token_hash=hash_token(token),
            expires_at=now + timedelta(minutes=settings.auth_email_verification_ttl_minutes),
        )
    )
    await session.flush()
    return token


async def consume_email_verification_token(session: AsyncSession, token: str) -> User | None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(AuthToken)
        .where(AuthToken.purpose == "email_verification")
        .where(AuthToken.token_hash == hash_token(token))
        .where(AuthToken.consumed_at.is_(None))
        .where(AuthToken.expires_at > now)
    )
    auth_token = result.scalar_one_or_none()
    if not auth_token:
        return None
    user = await session.get(User, auth_token.user_id)
    if not user:
        return None
    auth_token.consumed_at = now
    user.email_verified_at = now
    user.status = "active"
    return user


async def send_email_verification(user: User, token: str) -> None:
    settings = get_settings()
    verification_url = f"{settings.frontend_base_url.rstrip('/')}/login?verify_email={token}"
    subject = "Подтверждение почты DemoPlatform"
    body = (
        f"Привет!\n\n"
        f"Подтверди почту для кабинета DemoPlatform:\n{verification_url}\n\n"
        f"Если ты не создавал кабинет, просто проигнорируй письмо."
    )

    if not settings.smtp_host:
        print(f"[auth] email verification for {user.email}: {verification_url}")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.auth_email_from
    message["To"] = user.email
    message.set_content(body)

    def _send() -> None:
        smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)

    await asyncio.to_thread(_send)


def build_login_response(user: User, workspace_id: str | None = None) -> dict[str, str]:
    claims = {"workspace_id": workspace_id} if workspace_id else None
    return {"access_token": create_access_token(user.id, claims), "token_type": "bearer"}


def google_authorization_url(state: str) -> str:
    settings = get_settings()
    if not settings.google_oauth_client_id or not settings.google_oauth_redirect_uri:
        raise RuntimeError("Google OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


async def create_google_oauth_state(session: AsyncSession) -> str:
    state = secrets.token_urlsafe(32)
    session.add(
        OAuthState(
            provider="google",
            state_hash=hash_token(state),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await session.flush()
    return state


async def consume_google_oauth_state(session: AsyncSession, state: str) -> bool:
    oauth_state = await get_valid_google_oauth_state(session, state)
    if not oauth_state:
        return False
    oauth_state.consumed_at = datetime.now(UTC)
    await session.flush()
    return True


async def get_valid_google_oauth_state(session: AsyncSession, state: str) -> OAuthState | None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(OAuthState)
        .where(OAuthState.provider == "google")
        .where(OAuthState.state_hash == hash_token(state))
        .where(OAuthState.consumed_at.is_(None))
        .where(OAuthState.expires_at > now)
    )
    return result.scalar_one_or_none()


async def exchange_google_code(code: str) -> dict:
    settings = get_settings()
    if (
        not settings.google_oauth_client_id
        or not settings.google_oauth_client_secret
        or not settings.google_oauth_redirect_uri
    ):
        raise RuntimeError("Google OAuth is not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        try:
            token_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GoogleOAuthError(_google_oauth_error_message(token_response)) from exc
        access_token = token_response.json()["access_token"]
        userinfo_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            userinfo_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GoogleOAuthError(_google_oauth_error_message(userinfo_response)) from exc
        return userinfo_response.json()


def _google_oauth_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = str(payload.get("error") or response.status_code)
    description = str(payload.get("error_description") or payload.get("message") or "").strip()
    if error == "invalid_grant":
        return (
            "Google отклонил одноразовый код входа. Нажмите «Войти через Google» еще раз "
            "и завершите вход в той же вкладке."
        )
    if error == "redirect_uri_mismatch":
        return "Google OAuth redirect URI не совпадает с настройками приложения. Проверьте redirect URI в Google Cloud."
    if description:
        return f"Google OAuth error: {error}: {description}"
    return f"Google OAuth error: {error}"


async def get_or_create_google_user(session: AsyncSession, profile: dict) -> tuple[User, Workspace | None]:
    provider_user_id = str(profile.get("sub") or "")
    email = str(profile.get("email") or "").lower()
    if not provider_user_id or not email:
        raise ValueError("Google profile has no verified identity")

    oauth_result = await session.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == "google",
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    oauth_account = oauth_result.scalar_one_or_none()
    if oauth_account:
        user = await session.get(User, oauth_account.user_id)
        if not user:
            raise ValueError("Linked Google user was not found")
        return user, None

    user_result = await session.execute(select(User).where(User.email == email))
    user = user_result.scalar_one_or_none()
    created_workspace: Workspace | None = None
    if not user:
        user = User(
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            full_name=profile.get("name"),
            email_verified_at=datetime.now(UTC) if profile.get("email_verified") else None,
        )
        session.add(user)
        await session.flush()
        created_workspace = await create_workspace_for_user(session, user, profile.get("name") or "DemoPlatform")
    elif profile.get("email_verified") and not user.email_verified_at:
        user.email_verified_at = datetime.now(UTC)

    session.add(
        OAuthAccount(
            user_id=user.id,
            provider="google",
            provider_user_id=provider_user_id,
            email=email,
            raw_profile=profile,
        )
    )
    await session.flush()
    return user, created_workspace
