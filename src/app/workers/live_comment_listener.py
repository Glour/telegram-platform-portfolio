import asyncio
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.telegram_gateway.service import (
    TelegramChatMessageInfo,
    TelegramGatewayError,
    source_ref_from_fields,
)
from sqlalchemy import select
from telethon.tl import types
from telethon.tl.types import Message

from app.core.sentry import capture_exception, init_sentry
from app.db.session import AsyncSessionLocal
from app.modules.auth.models import User  # noqa: F401 - load FK target for live ORM flushes
from app.modules.campaigns.comment_posting import live_comment_delay_seconds, process_live_comment_message
from app.modules.campaigns.domain import RUN_TICKABLE_STATUSES
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignRun
from app.modules.campaigns.neuro_chat import process_live_neuro_message
from app.modules.campaigns.service import _account_is_actionable, tick_generic_campaign_run
from app.modules.operations.service import record_operational_event
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.workspaces.models import Workspace  # noqa: F401 - load FK target for live ORM flushes

init_sentry()

_account_processing_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class LiveSourceBinding:
    kind: str
    source_id: str
    source_ref: str
    title: str | None


@dataclass(frozen=True)
class LiveAccountBinding:
    account_id: str
    sources: list[LiveSourceBinding]


def _refresh_seconds() -> int:
    try:
        return max(60, min(int(os.getenv("LIVE_LISTENER_REFRESH_SECONDS", "60")), 1800))
    except ValueError:
        return 60


def _account_processing_lock(account_id: str) -> asyncio.Lock:
    lock = _account_processing_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _account_processing_locks[account_id] = lock
    return lock


def _source_hard_blocked_for_live(source: TelegramSource) -> bool:
    metadata = source.source_metadata or {}
    return (
        source.access_status in {"blacklisted", "archived"}
        or source.access_status.startswith("invalid_")
        or metadata.get("blacklisted") is True
        or metadata.get("invalid") is True
        or source.is_private
        or source.is_join_request_required
    )


async def main() -> None:
    raise RuntimeError("Persistent Telegram listener is disabled; use the guarded polling engine.")


async def _load_live_bindings() -> list[LiveAccountBinding]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CampaignRun, Campaign)
            .join(Campaign, Campaign.id == CampaignRun.campaign_id)
            .where(
                CampaignRun.status.in_(RUN_TICKABLE_STATUSES),
                Campaign.type.in_(["comment_posts", "neuro_chat", "neuro_dialog", "mass_reactions"]),
            )
        )
        bindings_by_account: dict[str, dict[tuple[str, str], LiveSourceBinding]] = {}
        for _run, campaign in result.all():
            config = campaign.config or {}
            if campaign.type == "comment_posts" and config.get("post_selection_mode", "new_posts") != "new_posts":
                continue
            if campaign.type == "mass_reactions" and (
                config.get("post_selection_mode", "new_posts") != "new_posts"
                or str(config.get("target_scope") or "posts") != "posts"
            ):
                continue
            account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]
            source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
            target_refs = [
                str(item) for item in config.get("target_refs", []) if isinstance(item, str) and item.strip()
            ]
            if not account_ids or (not source_ids and not target_refs):
                continue
            for account_id in account_ids:
                account_bindings = bindings_by_account.setdefault(account_id, {})
                for source_id in source_ids:
                    if campaign.type == "comment_posts" and config.get("source_assignment_mode") in {
                        "manual_pinned",
                        "multi_account_split",
                    }:
                        assigned = (config.get("source_assignment") or {}).get(account_id, [])
                        if source_id not in assigned:
                            continue
                    account_bindings[(campaign.type, source_id)] = LiveSourceBinding(
                        kind=campaign.type,
                        source_id=source_id,
                        source_ref="",
                        title=None,
                    )
                for ref in target_refs:
                    account_bindings[(campaign.type, ref)] = LiveSourceBinding(
                        kind=campaign.type,
                        source_id="",
                        source_ref=ref,
                        title=ref,
                    )

        if not bindings_by_account:
            return []

        account_ids = sorted(bindings_by_account)
        accounts_result = await session.execute(select(TelegramAccount).where(TelegramAccount.id.in_(account_ids)))
        actionable_account_ids = {
            account.id for account in accounts_result.scalars().all() if _account_is_actionable(account)
        }
        if not actionable_account_ids:
            return []

        all_source_ids = sorted(
            {
                binding.source_id
                for account_bindings in bindings_by_account.values()
                for binding in account_bindings.values()
                if binding.source_id
            }
        )
        sources_result = await session.execute(select(TelegramSource).where(TelegramSource.id.in_(all_source_ids)))
        sources_by_id = {source.id: source for source in sources_result.scalars().all()}

        bindings: list[LiveAccountBinding] = []
        for account_id, account_bindings in bindings_by_account.items():
            if account_id not in actionable_account_ids:
                continue
            sources: list[LiveSourceBinding] = []
            for binding in account_bindings.values():
                if binding.source_ref:
                    sources.append(binding)
                    continue
                source = sources_by_id.get(binding.source_id)
                if not source:
                    continue
                if _source_hard_blocked_for_live(source):
                    continue
                try:
                    source_ref = source_ref_from_fields(source.username, source.url, source.tg_id)
                except TelegramGatewayError:
                    continue
                sources.append(
                    LiveSourceBinding(
                        kind=binding.kind,
                        source_id=source.id,
                        source_ref=source_ref,
                        title=source.title,
                    )
                )
            if sources:
                bindings.append(LiveAccountBinding(account_id=account_id, sources=sources))
        return bindings


async def _run_account_listener(binding: LiveAccountBinding) -> None:
    raise RuntimeError("Persistent Telegram listener is disabled; use the guarded polling engine.")


async def _mark_source_unresolved(source: LiveSourceBinding, exc: Exception) -> None:
    if not source.source_id:
        return
    async with AsyncSessionLocal() as session:
        db_source = await session.get(TelegramSource, source.source_id)
        if not db_source:
            return
        db_source.source_metadata = {
            **(db_source.source_metadata or {}),
            "live_listener_last_error": {
                "code": type(exc).__name__,
                "message": "Источник не найден Telegram или недоступен аккаунту.",
                "source_ref": source.source_ref,
                "failed_at": datetime.now(UTC).isoformat(),
            },
        }
        await session.commit()


async def _record_listener_heartbeat(account_id: str, source_count: int) -> None:
    async with AsyncSessionLocal() as session:
        account = await session.get(TelegramAccount, account_id)
        await record_operational_event(
            session,
            workspace_id=account.workspace_id if account else None,
            severity="info",
            category="live_listener",
            event_type="live_listener_heartbeat",
            title="Live-listener активен",
            message=f"Аккаунт слушает источники: {source_count}.",
            source="live_comment_listener",
            entity_type="telegram_account",
            entity_id=account_id,
            dedupe_key=f"live_listener_heartbeat:{account_id}",
            payload={"account_id": account_id, "source_count": source_count},
            commit=True,
        )


async def _record_listener_failure(account_id: str, exc: Exception) -> None:
    async with AsyncSessionLocal() as session:
        account = await session.get(TelegramAccount, account_id)
        await record_operational_event(
            session,
            workspace_id=account.workspace_id if account else None,
            severity="warning",
            category="live_listener",
            event_type="live_listener_failed",
            title="Live-listener аккаунта перезапускается",
            message=(
                "Один Telegram-аккаунт временно выпал из live-listener. "
                "Сервис продолжает работать и попробует подключить аккаунт снова на следующем цикле."
            ),
            source="live_comment_listener",
            entity_type="telegram_account",
            entity_id=account_id,
            dedupe_key=f"live_listener_failed:{account_id}:{type(exc).__name__}",
            payload={"account_id": account_id, "error_code": type(exc).__name__, "error": str(exc)},
            alert=False,
            commit=True,
        )


def _message_payload(message: Message) -> dict[str, Any]:
    replies = getattr(message, "replies", None)
    published_at = message.date.astimezone(UTC) if message.date else datetime.now(UTC)
    return {
        "tg_message_id": str(message.id),
        "text": message.message or None,
        "published_at": published_at,
        "views_count": int(message.views or 0),
        "forwards_count": int(message.forwards or 0),
        "replies_count": int(getattr(replies, "replies", 0) or 0),
        "raw": {
            "mode": "telethon_live",
            "grouped_id": str(message.grouped_id) if message.grouped_id else None,
            "post_author": message.post_author,
            "has_replies": bool(replies),
            "has_media": bool(message.media),
            "media_type": _message_media_type(message),
        },
    }


def _chat_message_payload(message: Message) -> TelegramChatMessageInfo:
    return TelegramChatMessageInfo(
        tg_message_id=str(message.id),
        text=message.message or None,
        published_at=message.date.astimezone(UTC) if message.date else datetime.now(UTC),
        sender_tg_id=str(message.sender_id) if message.sender_id else None,
        outgoing=bool(getattr(message, "out", False)),
        raw={
            "mode": "telethon_live",
            "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
            "grouped_id": str(message.grouped_id) if message.grouped_id else None,
        },
    )


def _entity_ref(source_ref: str) -> str | int:
    cleaned = source_ref.strip()
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if cleaned.startswith("@"):
        return cleaned[1:]
    return cleaned


def _message_media_type(message: Message) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        mime_type = getattr(document, "mime_type", None)
        if isinstance(mime_type, str) and mime_type:
            return mime_type.split("/", 1)[0] or "document"
        return "document"
    if isinstance(media, types.MessageMediaWebPage):
        return "webpage"
    return type(media).__name__


async def _safe_handle_new_post(account_id: str, source: LiveSourceBinding, payload: dict[str, Any]) -> None:
    try:
        async with _account_processing_lock(account_id):
            await _handle_new_post(account_id, source.source_id, payload)
    except Exception as exc:  # noqa: BLE001 - keep listener task alive.
        capture_exception(exc, source="live_comment_listener", handler="comment_posts", account_id=account_id)
        await _record_live_handler_failure(
            account_id=account_id,
            source=source,
            campaign_type="comment_posts",
            exc=exc,
        )


async def _safe_handle_new_mass_reaction_post(
    account_id: str,
    source: LiveSourceBinding,
    payload: dict[str, Any],
) -> None:
    try:
        async with _account_processing_lock(account_id):
            await _handle_new_mass_reaction_post(account_id, source.source_id, payload)
    except Exception as exc:  # noqa: BLE001 - keep listener task alive.
        capture_exception(exc, source="live_comment_listener", handler="mass_reactions", account_id=account_id)
        await _record_live_handler_failure(
            account_id=account_id,
            source=source,
            campaign_type="mass_reactions",
            exc=exc,
        )


async def _safe_handle_new_neuro_message(
    account_id: str,
    campaign_type: str,
    source: LiveSourceBinding,
    message: TelegramChatMessageInfo,
) -> None:
    try:
        async with _account_processing_lock(account_id):
            await _handle_new_neuro_message(
                account_id,
                campaign_type,
                source.source_id,
                source.source_ref,
                source.title,
                message,
            )
    except Exception as exc:  # noqa: BLE001 - keep listener task alive.
        capture_exception(exc, source="live_comment_listener", handler=campaign_type, account_id=account_id)
        await _record_live_handler_failure(
            account_id=account_id,
            source=source,
            campaign_type=campaign_type,
            exc=exc,
        )


async def _record_live_handler_failure(
    *,
    account_id: str,
    source: LiveSourceBinding,
    campaign_type: str,
    exc: Exception,
) -> None:
    async with AsyncSessionLocal() as session:
        db_source = await session.get(TelegramSource, source.source_id) if source.source_id else None
        account = await session.get(TelegramAccount, account_id)
        await record_operational_event(
            session,
            workspace_id=(db_source.workspace_id if db_source else account.workspace_id if account else None),
            severity="critical" if _is_provider_or_connection_error(exc) else "error",
            category="campaigns",
            event_type="live_message_processing_failed",
            title="Новый пост не обработан",
            message=_friendly_live_handler_error(exc),
            source="live_comment_listener",
            entity_type="telegram_source",
            entity_id=source.source_id or source.source_ref,
            dedupe_key=(
                f"live_message_processing_failed:{campaign_type}:"
                f"{source.source_id or source.source_ref}:{type(exc).__name__}"
            ),
            payload={
                "account_id": account_id,
                "source_id": source.source_id,
                "source_ref": source.source_ref,
                "campaign_type": campaign_type,
                "error_code": type(exc).__name__,
                "error": str(exc),
            },
            alert=True,
            commit=True,
        )


def _is_provider_or_connection_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "openai" in text or "provider" in text or "connection" in text or "timeout" in text


def _friendly_live_handler_error(exc: Exception) -> str:
    text = str(exc)
    if "Connection to Telegram failed" in text:
        return "Не удалось подключиться к Telegram при обработке нового поста. Проверь прокси и сессию аккаунта."
    if "OpenAI" in text or "AI_PROVIDER" in text:
        return "AI-провайдер не ответил при обработке нового поста. Проверь настройки провайдера."
    return text or "Live-listener не смог обработать новое сообщение."


async def _handle_new_post(account_id: str, source_id: str, payload: dict[str, Any]) -> None:
    text = str(payload.get("text") or "").strip()
    if not text:
        return

    async with AsyncSessionLocal() as session:
        source = await session.get(TelegramSource, source_id)
        if not source:
            return
        existing = await session.execute(
            select(TelegramMessage).where(
                TelegramMessage.source_id == source_id,
                TelegramMessage.tg_message_id == payload["tg_message_id"],
            )
        )
        message = existing.scalar_one_or_none()
        if not message:
            message = TelegramMessage(
                workspace_id=source.workspace_id,
                source_id=source.id,
                tg_message_id=payload["tg_message_id"],
            )
            session.add(message)
        message.text = payload["text"]
        message.published_at = payload["published_at"]
        message.views_count = payload["views_count"]
        message.forwards_count = payload["forwards_count"]
        message.replies_count = payload["replies_count"]
        message.raw = payload["raw"]
        source.last_parsed_at = datetime.now(UTC)
        await session.flush()

        pairs = await _active_campaign_runs_for_source(session, source.workspace_id, account_id, source_id)
        message_id = message.id
        await session.commit()

    for run_id, campaign_id, delay_seconds in pairs:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        async with AsyncSessionLocal() as session:
            run = await session.get(CampaignRun, run_id)
            campaign = await session.get(Campaign, campaign_id)
            source = await session.get(TelegramSource, source_id)
            message = await session.get(TelegramMessage, message_id)
            if not run or not campaign or not source or not message:
                continue
            await process_live_comment_message(
                session,
                source.workspace_id,
                campaign,
                run,
                source,
                message,
                account_id=account_id,
            )


async def _handle_new_mass_reaction_post(account_id: str, source_id: str, payload: dict[str, Any]) -> None:
    text = str(payload.get("text") or "").strip()
    if not text:
        return

    async with AsyncSessionLocal() as session:
        source = await session.get(TelegramSource, source_id)
        if not source:
            return
        existing = await session.execute(
            select(TelegramMessage).where(
                TelegramMessage.source_id == source_id,
                TelegramMessage.tg_message_id == payload["tg_message_id"],
            )
        )
        message = existing.scalar_one_or_none()
        if not message:
            message = TelegramMessage(
                workspace_id=source.workspace_id,
                source_id=source.id,
                tg_message_id=payload["tg_message_id"],
            )
            session.add(message)
        message.text = payload["text"]
        message.published_at = payload["published_at"]
        message.views_count = payload["views_count"]
        message.forwards_count = payload["forwards_count"]
        message.replies_count = payload["replies_count"]
        message.raw = payload["raw"]
        source.last_parsed_at = datetime.now(UTC)
        pairs = await _active_mass_reaction_runs_for_source(session, source.workspace_id, account_id, source_id)
        await session.commit()

    for run_id, campaign_id in pairs:
        async with AsyncSessionLocal() as session:
            run = await session.get(CampaignRun, run_id)
            campaign = await session.get(Campaign, campaign_id)
            if not run or not campaign:
                continue
            _ensure_live_reaction_cursor(campaign, source_id, payload)
            await tick_generic_campaign_run(session, run.workspace_id, run)
            asyncio.create_task(_send_queued_reactions_when_due(account_id, campaign.id, run.id))


async def _handle_new_neuro_message(
    account_id: str,
    campaign_type: str,
    source_id: str,
    peer_ref: str,
    peer_title: str | None,
    message: TelegramChatMessageInfo,
) -> None:
    text = str(message.text or "").strip()
    if not text:
        return
    print(
        "live-listener: neuro message received "
        f"type={campaign_type} account={account_id} peer={peer_ref} "
        f"message={message.tg_message_id} outgoing={message.outgoing}"
    )

    async with AsyncSessionLocal() as session:
        pairs = await _active_neuro_campaign_runs_for_peer(session, account_id, campaign_type, source_id, peer_ref)
    if not pairs:
        print(
            "live-listener: neuro message has no active campaign "
            f"type={campaign_type} account={account_id} peer={peer_ref} source_id={source_id}"
        )
        return

    for run_id, campaign_id, workspace_id in pairs:
        async with AsyncSessionLocal() as session:
            run = await session.get(CampaignRun, run_id)
            campaign = await session.get(Campaign, campaign_id)
            if not run or not campaign:
                continue
            await process_live_neuro_message(
                session,
                workspace_id,
                campaign,
                run,
                account_id,
                peer_ref,
                peer_title,
                message,
            )
            asyncio.create_task(
                _send_queued_neuro_reply_when_due(
                    account_id,
                    campaign.id,
                    run.id,
                    f"{peer_ref}:{message.tg_message_id}",
                )
            )


async def _send_queued_reactions_when_due(account_id: str, campaign_id: str, run_id: str) -> None:
    async with _account_processing_lock(account_id):
        for _ in range(100):
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(CampaignAction)
                    .where(
                        CampaignAction.campaign_id == campaign_id,
                        CampaignAction.action_type == "set_reaction",
                        CampaignAction.status == "queued",
                    )
                    .order_by(CampaignAction.planned_at.asc(), CampaignAction.created_at.asc())
                    .limit(1)
                )
                action = result.scalar_one_or_none()
                planned_at = action.planned_at if action else None
            if not action or not planned_at:
                return
            planned_at = planned_at.replace(tzinfo=UTC) if planned_at.tzinfo is None else planned_at.astimezone(UTC)
            delay_seconds = max(0.0, (planned_at - datetime.now(UTC)).total_seconds())
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            async with AsyncSessionLocal() as session:
                run = await session.get(CampaignRun, run_id)
                if not run or run.status not in RUN_TICKABLE_STATUSES:
                    return
                await tick_generic_campaign_run(session, run.workspace_id, run)


async def _send_queued_neuro_reply_when_due(account_id: str, campaign_id: str, run_id: str, target_id: str) -> None:
    async with _account_processing_lock(account_id):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CampaignAction)
                .where(
                    CampaignAction.campaign_id == campaign_id,
                    CampaignAction.target_id == target_id,
                    CampaignAction.action_type.in_(["neuro_chat_reply", "neuro_dialog_reply"]),
                    CampaignAction.status == "queued",
                )
                .limit(1)
            )
            action = result.scalar_one_or_none()
            planned_at = action.planned_at if action else None
        if not action or not planned_at:
            return
        planned_at = planned_at.replace(tzinfo=UTC) if planned_at.tzinfo is None else planned_at.astimezone(UTC)
        delay_seconds = max(0.0, (planned_at - datetime.now(UTC)).total_seconds())
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        async with AsyncSessionLocal() as session:
            run = await session.get(CampaignRun, run_id)
            if not run or run.status not in RUN_TICKABLE_STATUSES:
                return
            await tick_generic_campaign_run(session, run.workspace_id, run)


async def _active_campaign_runs_for_source(
    session,
    workspace_id: str,
    account_id: str,
    source_id: str,
) -> list[tuple[str, str, int]]:
    result = await session.execute(
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.status.in_(RUN_TICKABLE_STATUSES),
            Campaign.type == "comment_posts",
            Campaign.status.in_(["pending", "running"]),
        )
    )
    pairs: list[tuple[str, str, int]] = []
    for run, campaign in result.all():
        config = campaign.config or {}
        if config.get("post_selection_mode", "new_posts") != "new_posts":
            continue
        if account_id not in [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]:
            continue
        if source_id not in [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]:
            continue
        if config.get("source_assignment_mode") in {"manual_pinned", "multi_account_split"}:
            if source_id not in (config.get("source_assignment") or {}).get(account_id, []):
                continue
        pairs.append((run.id, campaign.id, live_comment_delay_seconds(campaign.policy or {})))
    return pairs


async def _active_mass_reaction_runs_for_source(
    session,
    workspace_id: str,
    account_id: str,
    source_id: str,
) -> list[tuple[str, str]]:
    result = await session.execute(
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.status.in_(RUN_TICKABLE_STATUSES),
            Campaign.type == "mass_reactions",
            Campaign.status.in_(["pending", "running"]),
        )
    )
    pairs: list[tuple[str, str]] = []
    for run, campaign in result.all():
        config = campaign.config or {}
        if config.get("post_selection_mode", "new_posts") != "new_posts":
            continue
        if str(config.get("target_scope") or "posts") != "posts":
            continue
        if account_id not in [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]:
            continue
        if source_id not in [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]:
            continue
        pairs.append((run.id, campaign.id))
    return pairs


def _ensure_live_reaction_cursor(campaign: Campaign, source_id: str, payload: dict[str, Any]) -> None:
    config = campaign.config or {}
    cursors = config.get("source_cursors") if isinstance(config.get("source_cursors"), dict) else {}
    if cursors.get(source_id):
        return
    published_at = payload.get("published_at")
    if not isinstance(published_at, datetime):
        published_at = datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    cursors = dict(cursors)
    cursors[source_id] = {
        "last_seen_message_id": None,
        "last_seen_tg_message_id": None,
        "last_seen_target_kind": "telegram_message",
        "last_seen_at": (published_at.astimezone(UTC) - timedelta(microseconds=1)).isoformat(),
    }
    campaign.config = {**config, "source_cursors": cursors}


async def _active_neuro_campaign_runs_for_peer(
    session,
    account_id: str,
    campaign_type: str,
    source_id: str,
    peer_ref: str,
) -> list[tuple[str, str, str]]:
    result = await session.execute(
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(
            CampaignRun.status.in_(RUN_TICKABLE_STATUSES),
            Campaign.type == campaign_type,
            Campaign.status.in_(["pending", "running"]),
        )
    )
    pairs: list[tuple[str, str, str]] = []
    for run, campaign in result.all():
        config = campaign.config or {}
        if account_id not in [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]:
            continue
        if source_id:
            source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
            if source_id not in source_ids:
                continue
        else:
            refs = [str(item) for item in config.get("target_refs", []) if isinstance(item, str)]
            if peer_ref not in refs:
                continue
        pairs.append((run.id, campaign.id, run.workspace_id))
    return pairs


if __name__ == "__main__":
    asyncio.run(main())
