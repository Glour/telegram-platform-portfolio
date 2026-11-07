import random
import secrets
from datetime import UTC, datetime, timedelta

from app.modules.telegram_gateway.contacts import send_contact_photo
from app.modules.telegram_gateway.service import real_telegram_enabled
from sqlalchemy import func, select

from app.core.errors import bad_request
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignEvent, CampaignRun
from app.modules.contact_broadcasts.schemas import BroadcastTiming
from app.modules.contact_broadcasts.service import ensure_can_start, media_for_workspace, reserved_recipient_ids
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.workspaces.models import Workspace

ACTIVE = {"pending", "running", "paused", "stopping"}
SENT = {"succeeded", "sent_unverified"}
RESTRICTED = {
    "PeerFloodError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
    "SESSION_UNAUTHORIZED",
    "AuthKeyUnregisteredError",
    "OWNER_MISMATCH",
    "FrozenMethodInvalidError",
}


def event(session, campaign, run, message, *, level="info", details=None):
    session.add(
        CampaignEvent(
            campaign_id=campaign.id,
            run_id=run.id,
            level=level,
            event_type="contact_broadcast",
            message=message,
            payload=details or {},
        )
    )


async def start(session, workspace_id, campaign):
    await session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    campaign = (
        await session.execute(
            select(Campaign).where(Campaign.id == campaign.id, Campaign.workspace_id == workspace_id).with_for_update()
        )
    ).scalar_one()
    await ensure_can_start(session, campaign)
    existing = (
        (
            await session.execute(
                select(CampaignRun)
                .where(CampaignRun.campaign_id == campaign.id)
                .order_by(CampaignRun.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if existing:
        # Never revive a stopped/ambiguous job or produce another set of deliveries.
        return existing
    actions = (
        (await session.execute(select(CampaignAction).where(CampaignAction.campaign_id == campaign.id))).scalars().all()
    )
    if not actions or any(action.status != "prepared" for action in actions):
        raise bad_request("Нет подготовленных получателей.")
    previous = await reserved_recipient_ids(
        session, workspace_id, campaign.id, include_sent=campaign.config.get("exclude_previous", True)
    )
    for action in actions:
        if action.target_id in previous:
            action.status = "skipped"
            action.error_message = "Получатель уже отправлен или закреплён за другой кампанией."
            continue
        action.status = "queued"
        action.planned_at = datetime.now(UTC)
        action.result = {**(action.result or {}), "random_id": secrets.randbits(63) or 1}
    run = CampaignRun(
        campaign_id=campaign.id,
        workspace_id=workspace_id,
        status="pending",
        stop_conditions={"manual_stop": True},
        progress={},
        report={},
    )
    session.add(run)
    campaign.status = "pending"
    await session.flush()
    event(session, campaign, run, "Рассылка по сохранённым контактам поставлена в очередь.")
    await session.commit()
    return run


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


async def report(session, campaign, run):
    actions = (
        (await session.execute(select(CampaignAction).where(CampaignAction.campaign_id == campaign.id))).scalars().all()
    )
    counts = {}
    for action in actions:
        counts[action.status] = counts.get(action.status, 0) + 1
    sent = sum(counts.get(key, 0) for key in SENT)
    run.progress = {
        **(run.progress or {}),
        "broadcast_targets": len(actions),
        "broadcast_sent": sent,
        "broadcast_failed": counts.get("failed", 0),
        "successful_actions": sent,
    }
    run.report = {
        "recipient_mode": "saved_contacts",
        "counts": counts,
        "outgoing_verified": counts.get("succeeded", 0),
        "sent": sent,
        "unknown": counts.get("unknown", 0),
        "read_by_recipient": None,
    }
    if (
        run.status in {"pending", "running"}
        and actions
        and all(action.status in SENT | {"failed", "skipped"} for action in actions)
    ):
        run.status = campaign.status = "completed"
        run.finished_at = datetime.now(UTC)
    run.heartbeat_at = datetime.now(UTC)
    await session.commit()
    return run


async def tick(session, workspace_id, run):
    now = datetime.now(UTC)
    await session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    # Serialize tick requests (including manual API ticks) before claiming a recipient.
    campaign = (
        await session.execute(
            select(Campaign)
            .where(Campaign.id == run.campaign_id, Campaign.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one()
    await session.refresh(run)
    if run.status not in {"pending", "running", "stopping"}:
        return run
    if run.requested_stop_at or run.status == "stopping":
        run.status = campaign.status = "cancelled"
        run.finished_at = now
        return await report(session, campaign, run)
    try:
        account, payload = await ensure_can_start(session, campaign)
        await session.execute(select(TelegramAccount).where(TelegramAccount.id == account.id).with_for_update())
        await session.refresh(account, attribute_names=["daily_limits"])
        if not real_telegram_enabled():
            raise bad_request("Для этого режима требуется настоящее подключение к Telegram.")
        _, path = await media_for_workspace(session, workspace_id, payload["media_id"])
    except Exception as exc:
        run.status = campaign.status = "failed"
        run.error_code = "CONTACT_PREFLIGHT_FAILED"
        run.error_message = str(getattr(exc, "detail", exc))
        event(session, campaign, run, run.error_message, level="error")
        return await report(session, campaign, run)
    in_flight = (
        (
            await session.execute(
                select(CampaignAction).where(
                    CampaignAction.telegram_account_id == account.id,
                    CampaignAction.target_type == "saved_contact",
                    CampaignAction.status == "sending",
                )
            )
        )
        .scalars()
        .all()
    )
    if in_flight:
        for action in in_flight:
            started = datetime.fromisoformat(action.result["sending_started_at"])
            if now - started > timedelta(minutes=10):
                action.status = "unknown"
                action.error_code = "DELIVERY_UNKNOWN"
                action.error_message = "Процесс прерван после начала отправки; требуется проверка диалога."
                affected_run = (
                    (
                        await session.execute(
                            select(CampaignRun).where(
                                CampaignRun.campaign_id == action.campaign_id, CampaignRun.status.in_(ACTIVE)
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if affected_run:
                    affected_run.status = "paused"
                affected = await session.get(Campaign, action.campaign_id)
                affected.status = "paused"
        return await report(session, campaign, run)
    if (
        await session.execute(
            select(CampaignAction.id).where(
                CampaignAction.campaign_id == campaign.id, CampaignAction.status == "unknown"
            )
        )
    ).first():
        run.status = campaign.status = "paused"
        return await report(session, campaign, run)
    account_wait = (account.daily_limits or {}).get("contact_broadcast_wait_until")
    if account_wait and datetime.fromisoformat(account_wait) > now:
        run.progress = {**(run.progress or {}), "next_send_at": account_wait}
        return await report(session, campaign, run)
    next_send_at = (run.progress or {}).get("next_send_at")
    if next_send_at and aware(datetime.fromisoformat(next_send_at)) > now:
        return await report(session, campaign, run)
    timing = BroadcastTiming.model_validate(campaign.config.get("timing") or {})
    latest_send = (
        await session.execute(
            select(func.max(CampaignAction.executed_at)).where(
                CampaignAction.telegram_account_id == account.id,
                CampaignAction.target_type == "saved_contact",
                CampaignAction.status.in_(SENT),
            )
        )
    ).scalar_one()
    if latest_send and aware(latest_send) + timedelta(seconds=timing.delay_min) > now:
        run.progress = {
            **(run.progress or {}),
            "next_send_at": (aware(latest_send) + timedelta(seconds=timing.delay_min)).isoformat(),
        }
        return await report(session, campaign, run)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = (
        await session.execute(
            select(func.count(CampaignAction.id)).where(
                CampaignAction.telegram_account_id == account.id,
                CampaignAction.action_type == "message_broadcast",
                CampaignAction.status.in_(SENT),
                CampaignAction.executed_at >= today,
            )
        )
    ).scalar_one()
    timing = BroadcastTiming.model_validate(campaign.config.get("timing") or {})
    if sent_today >= timing.daily_limit:
        run.progress = {**(run.progress or {}), "next_send_at": (today + timedelta(days=1)).isoformat()}
        return await report(session, campaign, run)
    action = (
        await session.execute(
            select(CampaignAction)
            .where(
                CampaignAction.campaign_id == campaign.id,
                CampaignAction.status == "queued",
                CampaignAction.planned_at <= now,
            )
            .order_by(CampaignAction.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if not action:
        return await report(session, campaign, run)
    recipient = next(row for row in payload["recipients"] if row["user_id"] == action.target_id)
    action.status = "sending"
    action.result = {**action.result, "sending_started_at": now.isoformat()}
    run.status = campaign.status = "running"
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    # Persist the claim before network I/O. A killed worker cannot automatically repeat the send.
    await session.commit()
    try:
        sent = await send_contact_photo(session, account, payload, recipient, str(path), action.result["random_id"])
        action.status = "succeeded" if sent.raw.get("outgoing_verified") else "sent_unverified"
        action.result = {**action.result, "sent_message_id": sent.tg_message_id, "send": sent.raw}
        action.executed_at = datetime.now(UTC)
        event(
            session,
            campaign,
            run,
            "Фото с подписью отправлено.",
            details={
                "recipient_id": action.target_id,
                "message_id": sent.tg_message_id,
                "outgoing_verified": sent.raw.get("outgoing_verified", False),
            },
        )
        completed_at = datetime.now(UTC)
        progress = {**(run.progress or {}), "consecutive_failures": 0}
        pause_at = aware(datetime.fromisoformat(progress["pause_at"])) if progress.get("pause_at") else None
        delay = random.randint(timing.delay_min, timing.delay_max)
        if pause_at and completed_at >= pause_at:
            delay += random.randint(timing.pause_min, timing.pause_max)
            pause_at = None
        progress["pause_at"] = (
            pause_at
            or (completed_at + timedelta(seconds=random.randint(timing.pause_every_min, timing.pause_every_max)))
        ).isoformat()
        progress["next_send_at"] = (completed_at + timedelta(seconds=delay)).isoformat()
        run.progress = progress
        account.daily_limits = {
            **(account.daily_limits or {}),
            "contact_broadcast_wait_until": progress["next_send_at"],
        }
    except TelegramGatewayError as exc:
        action.error_code, action.error_message = exc.code, str(exc)
        if exc.code == "FLOOD_WAIT":
            action.status = "queued"
            action.planned_at = datetime.now(UTC) + timedelta(seconds=max(1, int(exc.raw.get("seconds", 60))))
            run.progress = {**(run.progress or {}), "next_send_at": action.planned_at.isoformat()}
            account.daily_limits = {
                **(account.daily_limits or {}),
                "contact_broadcast_wait_until": action.planned_at.isoformat(),
            }
        elif exc.code in {
            "TELEGRAM_CONNECTION_FAILED",
            "TELEGRAM_OPERATION_VALUE_ERROR",
            "SEND_RESULT_UNKNOWN",
            "ServerError",
            "RpcCallFailError",
            "TimedOutError",
        }:
            action.status = "unknown"
            run.status = campaign.status = "paused"
        else:
            action.status = "failed"
            action.executed_at = datetime.now(UTC)
            run.progress = {
                **(run.progress or {}),
                "next_send_at": (
                    datetime.now(UTC) + timedelta(seconds=random.randint(timing.delay_min, timing.delay_max))
                ).isoformat(),
            }
            failures = int((run.progress or {}).get("consecutive_failures", 0)) + 1
            run.progress = {**(run.progress or {}), "consecutive_failures": failures}
            if failures >= 3:
                run.status = campaign.status = "paused"
                run.progress = {**run.progress, "pause_reason": "recipient_errors"}
            if exc.code in RESTRICTED:
                run.status = campaign.status = "failed"
                account.daily_limits = {
                    **(account.daily_limits or {}),
                    "restriction_status": {"status": "limited", "reason": exc.code},
                }
        event(
            session,
            campaign,
            run,
            str(exc),
            level="error",
            details={"code": exc.code, "recipient_id": action.target_id},
        )
    except Exception as exc:
        action.status = "unknown"
        action.error_code = type(exc).__name__
        action.error_message = "Неопределённый результат отправки; проверьте диалог перед дальнейшими действиями."
        run.status = campaign.status = "paused"
        event(session, campaign, run, action.error_message, level="error")
    # Respect an API stop that arrived while Telegram was processing the current message.
    await session.refresh(run, attribute_names=["requested_stop_at"])
    if run.requested_stop_at:
        run.status = campaign.status = "cancelled"
    elif (await session.execute(select(CampaignRun.status).where(CampaignRun.id == run.id))).scalar_one() == "paused":
        run.status = campaign.status = "paused"
    return await report(session, campaign, run)
