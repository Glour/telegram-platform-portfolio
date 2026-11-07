from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.sentry import capture_exception
from app.modules.campaigns.models import Campaign, CampaignRun
from app.modules.operations.alerts import send_telegram_alert
from app.modules.operations.models import OperationalEvent
from app.modules.operations.schemas import (
    OperationHealthCheck,
    OperationHealthCounters,
    OperationsHealthResponse,
)
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount

ACTIVE_RUN_STATUSES = {"pending", "running", "stopping"}
PROBLEM_ACCOUNT_STATUSES = {"blocked", "banned", "spam_blocked", "failed", "error", "restricted", "quarantined"}
LIVE_LISTENER_CAMPAIGN_TYPES = {"comment_posts", "neuro_chat", "neuro_dialog"}
MONITOR_EVENT_BATCH_SIZE = 100
MONITOR_EVENT_LOOKBACK = timedelta(days=7)


async def record_operational_event(
    session: AsyncSession,
    *,
    severity: str,
    category: str,
    event_type: str,
    title: str,
    message: str,
    workspace_id: str | None = None,
    source: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    alert: bool = False,
    commit: bool = False,
) -> OperationalEvent:
    event = OperationalEvent(
        workspace_id=workspace_id,
        severity=severity,
        category=category,
        event_type=event_type,
        title=title,
        message=message,
        source=source,
        entity_type=entity_type,
        entity_id=entity_id,
        dedupe_key=dedupe_key,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()

    if alert and await _can_alert(session, dedupe_key):
        try:
            if await send_telegram_alert(severity=severity, title=title, message=message, payload=event.payload):
                event.notified_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001 - alert transport must not break business flow.
            capture_exception(exc, category="operations", event_type="telegram_alert_failed")

    if commit:
        await session.commit()
        await session.refresh(event)
    return event


async def run_operations_monitor(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(UTC)
    settings = get_settings()
    stale_before = now - timedelta(minutes=settings.campaign_run_stale_minutes)
    events_created = 0

    result = await session.execute(
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(
            CampaignRun.status.in_(ACTIVE_RUN_STATUSES),
            or_(CampaignRun.heartbeat_at.is_(None), CampaignRun.heartbeat_at < stale_before),
        )
        .order_by(CampaignRun.created_at.asc())
        .limit(50)
    )
    stale_run_ids: set[str] = set()
    for run, campaign in result.all():
        stale_run_ids.add(run.id)
        existing_event = await _get_unresolved_event_id(session, f"campaign_run_stale:{run.id}")
        if existing_event:
            continue
        await record_operational_event(
            session,
            workspace_id=run.workspace_id,
            severity="critical",
            category="campaigns",
            event_type="campaign_run_stale",
            title=f"Кампания зависла: {campaign.name}",
            message=(
                "Кампания числится запущенной, но давно не обновляла heartbeat. "
                "Нужно проверить worker/live-listener и аккаунт кампании."
            ),
            source="operations.monitor",
            entity_type="campaign_run",
            entity_id=run.id,
            dedupe_key=f"campaign_run_stale:{run.id}",
            payload={
                "campaign_id": campaign.id,
                "run_id": run.id,
                "campaign_name": campaign.name,
                "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
                "threshold_minutes": settings.campaign_run_stale_minutes,
            },
            alert=True,
        )
        events_created += 1

    await _resolve_campaign_run_stale_events(session, stale_run_ids, now)
    await _resolve_live_listener_failed_events(session, now)
    await _resolve_archived_source_error_events(session, now)
    await session.commit()
    return {"status": "completed", "events_created": events_created, "checked_at": now}


async def build_operations_health(session: AsyncSession, workspace_id: str) -> OperationsHealthResponse:
    now = datetime.now(UTC)
    settings = get_settings()
    day_ago = now - timedelta(hours=24)
    stale_before = now - timedelta(minutes=settings.campaign_run_stale_minutes)
    listener_stale_before = now - timedelta(minutes=settings.live_listener_stale_minutes)

    active_runs = await _count(
        session,
        select(func.count(CampaignRun.id)).where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.status.in_(ACTIVE_RUN_STATUSES),
        ),
    )
    stale_runs = await _count(
        session,
        select(func.count(CampaignRun.id)).where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.status.in_(ACTIVE_RUN_STATUSES),
            or_(CampaignRun.heartbeat_at.is_(None), CampaignRun.heartbeat_at < stale_before),
        ),
    )
    live_run_result = await session.execute(
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.status.in_(ACTIVE_RUN_STATUSES),
            Campaign.type.in_(LIVE_LISTENER_CAMPAIGN_TYPES),
        )
    )
    live_listener_runs = [run for run, campaign in live_run_result.all() if _campaign_requires_live_listener(campaign)]
    events_24h = await _count(
        session,
        select(func.count(OperationalEvent.id)).where(
            _workspace_event_filter(workspace_id),
            OperationalEvent.created_at >= day_ago,
        ),
    )
    critical_events_24h = await _count(
        session,
        select(func.count(OperationalEvent.id)).where(
            _workspace_event_filter(workspace_id),
            OperationalEvent.created_at >= day_ago,
            OperationalEvent.severity == "critical",
            OperationalEvent.resolved_at.is_(None),
        ),
    )
    error_events_24h = await _count(
        session,
        select(func.count(OperationalEvent.id)).where(
            _workspace_event_filter(workspace_id),
            OperationalEvent.created_at >= day_ago,
            OperationalEvent.severity == "error",
            OperationalEvent.resolved_at.is_(None),
        ),
    )
    active_accounts = await _count(
        session,
        select(func.count(TelegramAccount.id)).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status == "active",
        ),
    )
    problem_accounts = await _count(
        session,
        select(func.count(TelegramAccount.id)).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status.in_(PROBLEM_ACCOUNT_STATUSES),
        ),
    )

    recent_result = await session.execute(
        select(OperationalEvent)
        .where(_workspace_event_filter(workspace_id))
        .order_by(OperationalEvent.created_at.desc())
        .limit(10)
    )
    recent_events = list(recent_result.scalars().all())
    listener_result = await session.execute(
        select(OperationalEvent.created_at)
        .where(
            OperationalEvent.workspace_id == workspace_id,
            OperationalEvent.category == "live_listener",
            OperationalEvent.event_type == "live_listener_heartbeat",
        )
        .order_by(OperationalEvent.created_at.desc())
        .limit(1)
    )
    listener_heartbeat_at = listener_result.scalar_one_or_none()
    listener_stale = bool(
        live_listener_runs and (not listener_heartbeat_at or listener_heartbeat_at < listener_stale_before)
    )

    checks = [
        OperationHealthCheck(
            key="campaign_runner",
            label="Запуски кампаний",
            status="critical" if stale_runs else "ok",
            message="Есть зависшие кампании без heartbeat." if stale_runs else "Активные кампании обновляются.",
        ),
        OperationHealthCheck(
            key="live_listener",
            label="Live-listener",
            status="critical" if listener_stale else "ok",
            message=(
                "Есть активные кампании, но live-listener давно не присылал heartbeat."
                if listener_stale
                else "Heartbeat live-listener в норме."
                if live_listener_runs
                else "Активных live-кампаний сейчас нет."
            ),
            updated_at=listener_heartbeat_at,
            payload={
                "active_live_campaign_runs": len(live_listener_runs),
                "threshold_minutes": settings.live_listener_stale_minutes,
            },
        ),
        OperationHealthCheck(
            key="telegram_accounts",
            label="Telegram-аккаунты",
            status="warning" if problem_accounts else "ok",
            message=(
                f"Проблемных аккаунтов: {problem_accounts}."
                if problem_accounts
                else f"Активных аккаунтов: {active_accounts}."
            ),
        ),
        OperationHealthCheck(
            key="error_budget",
            label="Ошибки за сутки",
            status="critical" if critical_events_24h else ("warning" if error_events_24h else "ok"),
            message=f"Critical: {critical_events_24h}, errors: {error_events_24h}.",
        ),
        OperationHealthCheck(
            key="telegram_alerts",
            label="Telegram-алерты",
            status="ok" if get_settings().telegram_alerts_enabled else "warning",
            message="Алерты включены." if get_settings().telegram_alerts_enabled else "Алерты выключены в настройках.",
        ),
    ]
    status = (
        "critical"
        if any(check.status == "critical" for check in checks)
        else ("warning" if any(check.status == "warning" for check in checks) else "ok")
    )

    return OperationsHealthResponse(
        status=status,
        generated_at=now,
        counters=OperationHealthCounters(
            active_campaign_runs=active_runs,
            stale_campaign_runs=stale_runs,
            events_24h=events_24h,
            critical_events_24h=critical_events_24h,
            error_events_24h=error_events_24h,
            telegram_accounts_active=active_accounts,
            telegram_accounts_problem=problem_accounts,
        ),
        checks=checks,
        recent_events=recent_events,
    )


def _campaign_requires_live_listener(campaign: Campaign) -> bool:
    if campaign.type not in LIVE_LISTENER_CAMPAIGN_TYPES:
        return False
    if campaign.type != "comment_posts":
        return True
    config = campaign.config or {}
    return config.get("post_selection_mode", "new_posts") == "new_posts"


async def _can_alert(session: AsyncSession, dedupe_key: str | None) -> bool:
    if not dedupe_key:
        return True
    since = datetime.now(UTC) - timedelta(minutes=get_settings().operation_alert_min_interval_minutes)
    result = await session.execute(
        select(OperationalEvent.id)
        .where(
            OperationalEvent.dedupe_key == dedupe_key,
            OperationalEvent.notified_at.is_not(None),
            OperationalEvent.created_at >= since,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is None


async def _get_unresolved_event_id(session: AsyncSession, dedupe_key: str) -> str | None:
    result = await session.execute(
        select(OperationalEvent.id)
        .where(
            OperationalEvent.dedupe_key == dedupe_key,
            OperationalEvent.resolved_at.is_(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_campaign_run_stale_events(
    session: AsyncSession,
    stale_run_ids: set[str],
    resolved_at: datetime,
) -> None:
    conditions = [
        OperationalEvent.event_type == "campaign_run_stale",
        OperationalEvent.resolved_at.is_(None),
    ]
    if stale_run_ids:
        conditions.append(OperationalEvent.entity_id.not_in(stale_run_ids))

    await session.execute(update(OperationalEvent).where(*conditions).values(resolved_at=resolved_at))


async def _resolve_live_listener_failed_events(session: AsyncSession, resolved_at: datetime) -> None:
    result = await session.execute(
        select(OperationalEvent)
        .where(
            OperationalEvent.event_type == "live_listener_failed",
            OperationalEvent.resolved_at.is_(None),
        )
        .where(OperationalEvent.created_at >= resolved_at - MONITOR_EVENT_LOOKBACK)
        .order_by(OperationalEvent.created_at.desc())
        .limit(MONITOR_EVENT_BATCH_SIZE)
    )
    for event in result.scalars().all():
        heartbeat = await _latest_live_listener_heartbeat_after(session, event.entity_id, event.created_at)
        if heartbeat:
            event.resolved_at = resolved_at


async def _latest_live_listener_heartbeat_after(
    session: AsyncSession,
    account_id: str | None,
    failed_at: datetime,
) -> datetime | None:
    if not account_id:
        return None
    result = await session.execute(
        select(OperationalEvent.created_at)
        .where(
            OperationalEvent.event_type == "live_listener_heartbeat",
            OperationalEvent.entity_id == account_id,
            OperationalEvent.created_at > failed_at,
        )
        .order_by(OperationalEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_archived_source_error_events(session: AsyncSession, resolved_at: datetime) -> None:
    result = await session.execute(
        select(OperationalEvent)
        .where(
            OperationalEvent.category == "telegram",
            OperationalEvent.severity == "error",
            OperationalEvent.event_type.in_(("source_fetch_failed", "comment_send_failed")),
            OperationalEvent.resolved_at.is_(None),
        )
        .where(OperationalEvent.created_at >= resolved_at - MONITOR_EVENT_LOOKBACK)
        .order_by(OperationalEvent.created_at.desc())
        .limit(MONITOR_EVENT_BATCH_SIZE)
    )
    for event in result.scalars().all():
        payload = event.payload or {}
        source_id = payload.get("source_id")
        if not isinstance(source_id, str) and event.entity_type == "telegram_source":
            source_id = event.entity_id
        if not isinstance(source_id, str):
            continue

        if await _source_is_invalid_or_archived_after_event(session, source_id, event.created_at):
            event.resolved_at = resolved_at


async def _source_is_invalid_or_archived_after_event(
    session: AsyncSession,
    source_id: str,
    failed_at: datetime,
) -> bool:
    source = await session.get(TelegramSource, source_id)
    if source and (source.access_status == "archived" or source.access_status.startswith("invalid")):
        return True

    result = await session.execute(
        select(OperationalEvent.id)
        .where(
            OperationalEvent.event_type == "source_archived_after_critical_error",
            OperationalEvent.entity_id == source_id,
            OperationalEvent.created_at >= failed_at,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _count(session: AsyncSession, statement) -> int:
    result = await session.execute(statement)
    return int(result.scalar_one() or 0)


def _workspace_event_filter(workspace_id: str):
    return or_(OperationalEvent.workspace_id == workspace_id, OperationalEvent.workspace_id.is_(None))
