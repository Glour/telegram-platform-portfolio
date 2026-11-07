"""Opt-in, owner-authorized diagnostics for the two scoped campaign controllers."""

from datetime import UTC, datetime, timedelta


def requires_spambot_check(method: str, error_code: str | None) -> bool:
    return method in {"SendMessageRequest", "SendMediaRequest"} and error_code in {
        "UserBannedInChannelError",
        "PeerFloodError",
    }


def confirmed_spambot_restriction(classification: str) -> bool:
    return classification in {"limited", "blocked"}


async def check_campaign_restrictions(campaign_id: str, account_id: str) -> dict:
    """No retries, appeals, clearance, admission changes, or alternate transport."""
    import asyncio

    from app.modules.telegram_gateway.service import telegram_client_for_account
    from sqlalchemy import select
    from telethon.tl.functions.messages import GetHistoryRequest

    from app.db.session import AsyncSessionLocal
    from app.modules.campaigns.engine import request_stop_campaign_run
    from app.modules.campaigns.models import Campaign, CampaignEvent, CampaignRun
    from app.modules.telegram_accounts.models import TelegramAccount
    from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
    from app.modules.telegram_safety.spambot import PERMIT_METHOD, parse_spambot_reply
    from app.modules.workspaces.models import Workspace

    async with AsyncSessionLocal() as db:
        campaign = await db.get(Campaign, campaign_id)
        if (
            not campaign
            or campaign.status != "running"
            or campaign.config.get("account_ids") != [account_id]
            or not campaign.config.get("spambot_check_on_write_denial")
        ):
            return {"status": "not_enabled"}
        account = await db.get(TelegramAccount, account_id)
        workspace = await db.get(Workspace, campaign.workspace_id)
        state = await db.get(TelegramSafetyState, account_id, with_for_update=True)
        if (
            not account
            or account.workspace_id != campaign.workspace_id
            or not state
            or state.actor_id != workspace.owner_user_id
        ):
            return {"status": "authorization_mismatch"}
        since = datetime.now(UTC) - timedelta(hours=24)
        prior = await db.scalar(
            select(TelegramOperation.id)
            .where(
                TelegramOperation.account_id == account_id,
                TelegramOperation.method == PERMIT_METHOD,
                TelegramOperation.created_at >= since,
            )
            .limit(1)
        )
        if prior:
            return {"status": "already_checked_24h"}
        failures = list(
            await db.scalars(
                select(TelegramOperation)
                .where(
                    TelegramOperation.account_id == account_id,
                    TelegramOperation.method.in_(["SendMessageRequest", "SendMediaRequest"]),
                    TelegramOperation.error_code.in_(["UserBannedInChannelError", "PeerFloodError"]),
                    TelegramOperation.created_at >= since,
                )
                .order_by(TelegramOperation.created_at.desc())
                .limit(1)
            )
        )
        if not failures:
            return {"status": "no_trigger"}
        run = await db.scalar(
            select(CampaignRun).where(CampaignRun.campaign_id == campaign_id, CampaignRun.status == "running")
        )
        if not run:
            return {"status": "not_running"}
        permit = TelegramOperation(
            account_id=account_id,
            method=PERMIT_METHOD,
            target=workspace.owner_user_id,
            outbound=False,
            outcome="approved",
            created_at=datetime.now(UTC),
        )
        db.add(permit)
        await db.commit()
        # The existing guarded transport enforces all holds, waits and session locks.
        # A held account is never unheld to make this diagnostic succeed.
        result = {"status": "unknown", "trigger_operation_id": failures[0].id}
        try:
            async with asyncio.timeout(60):
                async with telegram_client_for_account(db, account, strict_requests=True) as client:
                    bot = await client.get_entity("SpamBot")
                    if not (bot.bot and bot.verified and (bot.username or "").lower() == "spambot"):
                        raise ValueError("Official verified SpamBot required")
                    client._safety_business_intent = f"diagnostic:spambot:{permit.id}"
                    sent = await client.send_message(bot, "/start")
                    peer = await client.get_input_entity(bot)
                    for _ in range(2):
                        await asyncio.sleep(3)
                        history = await client(
                            GetHistoryRequest(
                                peer=peer,
                                offset_id=0,
                                offset_date=None,
                                add_offset=0,
                                limit=4,
                                max_id=0,
                                min_id=sent.id,
                                hash=0,
                            )
                        )
                        replies = [
                            m
                            for m in history.messages
                            if not getattr(m, "out", False)
                            and getattr(m, "id", 0) > sent.id
                            and getattr(m, "message", None)
                            and getattr(getattr(m, "from_id", None), "user_id", bot.id) == bot.id
                        ]
                        if replies:
                            reply = min(replies, key=lambda m: m.id)
                            result.update(
                                status=parse_spambot_reply(reply.message),
                                reply=reply.message,
                                reply_at=reply.date.isoformat(),
                            )
                            break
            permit.outcome = "completed"
        except Exception as exc:
            result.update(error_code=getattr(exc, "code", type(exc).__name__), error=str(exc)[:300])
            permit.outcome = "failed"
        permit.finished_at = datetime.now(UTC)
        db.add(
            CampaignEvent(
                campaign_id=campaign_id,
                run_id=run.id,
                level="info",
                event_type="spambot_diagnostic_result",
                message="Результат разрешённой проверки SpamBot.",
                payload={**result, "permit_id": permit.id, "account_id": account_id},
            )
        )
        await db.commit()
        if confirmed_spambot_restriction(result["status"]):
            await db.refresh(state, with_for_update=True)
            await db.refresh(account, with_for_update=True)
            state.manual_hold = True
            state.reason = "spam_restricted"
            state.restriction_status = result["status"]
            state.restriction_checked_at = datetime.now(UTC)
            state.evidence = result["reply"]
            account.daily_limits = {
                **(account.daily_limits or {}),
                "restriction_status": {
                    "status": result["status"],
                    "label": "Подтверждено ограничение Telegram",
                    "checked_at": result["reply_at"],
                    "raw_text": result["reply"],
                },
            }
            await db.commit()
            await request_stop_campaign_run(
                db,
                campaign.workspace_id,
                run,
                reason="spambot_confirmed_restriction",
                campaign_status="paused",
                event_type="spambot_restriction_confirmed",
                event_message="Кампания остановлена: официальный SpamBot подтвердил ограничение.",
            )
        return result
