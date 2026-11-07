# ruff: noqa: E501
import copy
import csv
import hashlib
import io
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from itertools import cycle
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.modules.telegram_gateway.service import (
    fetch_chat_messages_with_account,
    fetch_post_comments_with_account,
    react_to_message_with_account,
    real_telegram_enabled,
    send_chat_reply_with_account,
    send_comment_with_account,
    source_ref_from_fields,
)
from app.modules.telegram_gateway.service import (
    fetch_posts_with_account as fetch_posts_with_account,
)
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.humanizer import normalize_author_gender
from app.modules.ai.models import AiGeneration, CommentPromptTemplate
from app.modules.ai.service import AiProviderError, generate_chat_reply_text, generate_comment_text
from app.modules.campaigns.behavior_protection import apply_behavior_delay_range
from app.modules.campaigns.conflicts import campaign_account_conflict_keys
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignEvent, CampaignRun, CampaignTarget
from app.modules.campaigns.schemas import (
    CampaignChannelReportSummary,
    CampaignCommentReport,
    CampaignCommentReportRow,
    CampaignReactionReport,
    CampaignReactionReportRow,
)
from app.modules.comments.models import TelegramComment
from app.modules.operations.service import record_operational_event
from app.modules.parsing.service import fetch_real_posts
from app.modules.sources.blacklist import blacklist_source
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount, TelegramSession
from app.modules.telegram_accounts.risk import get_module_eligibility
from app.modules.telegram_gateway.contracts import (
    TelegramChatMessageInfo,
    TelegramCommentInfo,
    TelegramGatewayError,
    TelegramPostInfo,
    TelegramSendResult,
)
from app.modules.telegram_safety.admission import owner_attested

RUN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
RUN_ACTIVE_STATUSES = {"pending", "running", "paused", "stopping"}
RUN_TICKABLE_STATUSES = {"pending", "running", "stopping"}
COMMENT_ACCOUNT_ACTIVE_STATUSES = {"active", "ready", "warming"}
COMMENT_DAILY_LIMIT_HARD_CAP = 5000
COMMENT_POST_INTERVAL_MAX = 5
BROADCAST_DAILY_LIMIT_HARD_CAP = 3
COMMENT_POST_INTERVAL_STATE_VERSION = 2
DEFAULT_COMMENT_SOURCES_PER_TICK = 3
SCHEDULED_CAMPAIGN_TICK_MIN_INTERVAL_SECONDS = 240
DEFAULT_COMMENT_MIN_POST_CHARS = 200
DEFAULT_COMMENT_MIN_MEDIA_POST_CHARS = 20
DEFAULT_COMMENT_STOP_WORDS = (
    "реклама",
    "рекламный пост",
    "рекламная интеграция",
    "промокод",
    "скидка",
    "акция",
    "розыгрыш",
    "giveaway",
    "конкурс",
    "вебинар",
    "марафон",
    "оффер",
    "купить",
    "продам",
    "прайс",
    "рассрочка",
)
PROGRESS_COUNTER_KEYS = {
    "sources_checked",
    "sources_skipped",
    "messages_seen",
    "new_posts_found",
    "duplicate_posts_skipped",
    "posts_skipped_by_interval",
    "posts_skipped_by_stop_words",
    "posts_skipped_by_length",
    "comments_generated",
    "comments_sent",
    "comments_drafted",
    "comments_failed",
    "actions_created",
    "successful_actions",
    "cursors_initialized",
    "messages_scored",
    "messages_selected",
    "replies_generated",
    "replies_sent",
    "replies_drafted",
    "replies_failed",
    "broadcast_targets",
    "broadcast_sent",
    "broadcast_failed",
    "reaction_targets",
    "reactions_planned",
    "reactions_sent",
    "reactions_failed",
    "reactions_skipped_probability",
    "reactions_skipped_duplicate",
    "reactions_skipped_daily_limit",
    "reactions_skipped_ai_context",
    "reactions_skipped_scope_unavailable",
    "reaction_cursors_initialized",
}


@dataclass
class CommentTickSummary:
    sources_checked: int = 0
    sources_skipped: int = 0
    messages_seen: int = 0
    new_posts_found: int = 0
    duplicate_posts_skipped: int = 0
    posts_skipped_by_interval: int = 0
    posts_skipped_by_stop_words: int = 0
    posts_skipped_by_length: int = 0
    comments_generated: int = 0
    comments_sent: int = 0
    comments_drafted: int = 0
    comments_failed: int = 0
    actions_created: int = 0
    successful_actions: int = 0
    cursors_initialized: int = 0
    stop_reason: str | None = None

    def as_progress(self) -> dict[str, Any]:
        return {
            "sources_checked": self.sources_checked,
            "sources_skipped": self.sources_skipped,
            "messages_seen": self.messages_seen,
            "new_posts_found": self.new_posts_found,
            "duplicate_posts_skipped": self.duplicate_posts_skipped,
            "posts_skipped_by_interval": self.posts_skipped_by_interval,
            "posts_skipped_by_stop_words": self.posts_skipped_by_stop_words,
            "posts_skipped_by_length": self.posts_skipped_by_length,
            "comments_generated": self.comments_generated,
            "comments_sent": self.comments_sent,
            "comments_drafted": self.comments_drafted,
            "comments_failed": self.comments_failed,
            "actions_created": self.actions_created,
            "successful_actions": self.successful_actions,
            "cursors_initialized": self.cursors_initialized,
            "stop_reason": self.stop_reason,
        }


@dataclass
class CommentRuntimeContext:
    prompt: CommentPromptTemplate
    sources: list[TelegramSource]
    accounts: list[TelegramAccount]


@dataclass(frozen=True)
class SourceCommentPreparationResult:
    attempted: bool = False
    archived: bool = False


@dataclass
class ChatTickSummary:
    sources_checked: int = 0
    messages_seen: int = 0
    messages_scored: int = 0
    messages_selected: int = 0
    replies_generated: int = 0
    replies_sent: int = 0
    replies_drafted: int = 0
    replies_failed: int = 0
    broadcast_targets: int = 0
    broadcast_sent: int = 0
    broadcast_failed: int = 0
    successful_actions: int = 0
    actions_created: int = 0
    stop_reason: str | None = None

    def as_progress(self) -> dict[str, Any]:
        return {
            "sources_checked": self.sources_checked,
            "messages_seen": self.messages_seen,
            "messages_scored": self.messages_scored,
            "messages_selected": self.messages_selected,
            "replies_generated": self.replies_generated,
            "replies_sent": self.replies_sent,
            "replies_drafted": self.replies_drafted,
            "replies_failed": self.replies_failed,
            "broadcast_targets": self.broadcast_targets,
            "broadcast_sent": self.broadcast_sent,
            "broadcast_failed": self.broadcast_failed,
            "successful_actions": self.successful_actions,
            "actions_created": self.actions_created,
            "stop_reason": self.stop_reason,
        }


@dataclass
class ReactionTickSummary:
    sources_checked: int = 0
    messages_seen: int = 0
    messages_selected: int = 0
    reaction_targets: int = 0
    reactions_planned: int = 0
    reactions_sent: int = 0
    reactions_failed: int = 0
    reactions_skipped_probability: int = 0
    reactions_skipped_duplicate: int = 0
    reactions_skipped_daily_limit: int = 0
    reactions_skipped_ai_context: int = 0
    reactions_skipped_scope_unavailable: int = 0
    reaction_cursors_initialized: int = 0
    successful_actions: int = 0
    actions_created: int = 0
    stop_reason: str | None = None

    def as_progress(self) -> dict[str, Any]:
        return {
            "sources_checked": self.sources_checked,
            "messages_seen": self.messages_seen,
            "messages_selected": self.messages_selected,
            "reaction_targets": self.reaction_targets,
            "reactions_planned": self.reactions_planned,
            "reactions_sent": self.reactions_sent,
            "reactions_failed": self.reactions_failed,
            "reactions_skipped_probability": self.reactions_skipped_probability,
            "reactions_skipped_duplicate": self.reactions_skipped_duplicate,
            "reactions_skipped_daily_limit": self.reactions_skipped_daily_limit,
            "reactions_skipped_ai_context": self.reactions_skipped_ai_context,
            "reactions_skipped_scope_unavailable": self.reactions_skipped_scope_unavailable,
            "reaction_cursors_initialized": self.reaction_cursors_initialized,
            "successful_actions": self.successful_actions,
            "actions_created": self.actions_created,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class ReactionTarget:
    source_id: str
    source_ref: str
    source_title: str | None
    target_kind: str
    target_tg_message_id: str
    local_message_id: str | None = None
    text: str | None = None
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReactionSendResult:
    status: str
    sent_at: datetime | None
    send_raw: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class NeuroReplyDeliveryResult:
    status: str
    sent_at: datetime | None
    sent_message_id: str | None
    send_raw: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None


async def start_comment_posts_campaign(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
) -> Campaign:
    context = await _load_comment_context(session, workspace_id, campaign)
    if not context:
        campaign.status = "failed"
        await session.commit()
        return campaign

    config = _json_dict(campaign.config)
    selection_mode = _selection_mode_from_config(config)
    delivery_mode = _delivery_mode_from_config(config)
    latest_limit = _effective_latest_limit(config, selection_mode)
    summary = await _process_comment_sources(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        context=context,
        selection_mode=selection_mode,
        delivery_mode=delivery_mode,
        latest_limit=latest_limit,
        max_comments=None,
        dedupe=False,
        update_cursors=selection_mode == "new",
    )

    campaign.status = "completed" if summary.successful_actions else "failed"
    await session.commit()
    await session.refresh(campaign)
    return campaign


async def start_comment_posts_campaign_run(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
) -> CampaignRun:
    from app.modules.campaigns.account_replacement import replacement_blocks_launch

    if replacement_blocks_launch(campaign):
        raise ValueError("CAMPAIGN_ACCOUNT_REPLACEMENT_PENDING")
    existing = await session.execute(
        select(CampaignRun)
        .where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(["pending", "running", "stopping"]),
        )
        .order_by(CampaignRun.created_at.desc())
        .limit(1)
    )
    active_run = existing.scalar_one_or_none()
    if active_run:
        if active_run.status == "pending":
            active_run.status = "running"
            active_run.started_at = active_run.started_at or datetime.now(UTC)
            active_run.heartbeat_at = datetime.now(UTC)
            active_run.report = {
                **_json_dict(active_run.report),
                "status": "running",
                "started_at": _iso(active_run.started_at),
                "heartbeat_at": _iso(active_run.heartbeat_at),
            }
            campaign.status = "running"
            _add_run_event(
                session,
                active_run,
                "info",
                "live_listener_active",
                "Кампания слушает новые сообщения в реальном времени.",
                {"campaign_type": campaign.type},
            )
            await session.commit()
            await session.refresh(active_run)
        return active_run

    now = datetime.now(UTC)
    stop_conditions = _json_dict(campaign.config.get("stop_conditions"))
    run = CampaignRun(
        workspace_id=workspace_id,
        campaign_id=campaign.id,
        status="running",
        started_at=now,
        heartbeat_at=now,
        stop_conditions=stop_conditions,
        progress=_empty_progress(),
        report=_build_report(campaign, "running", _empty_progress(), stop_conditions, now, None),
    )
    session.add(run)
    await session.flush()
    _add_run_event(
        session,
        run,
        "info",
        "run_created",
        "Запуск кампании создан и ожидает фоновой обработки.",
        {
            "post_selection_mode": _post_selection_mode_from_config(_json_dict(campaign.config)),
            "delivery_mode": _delivery_mode_from_config(_json_dict(campaign.config)),
        },
    )
    campaign.status = "pending"
    await session.commit()
    await session.refresh(run)
    return await tick_comment_posts_campaign_run(session, workspace_id, run)


async def tick_comment_posts_campaign_run(
    session: AsyncSession,
    workspace_id: str,
    run: CampaignRun,
) -> CampaignRun:
    now = datetime.now(UTC)
    if run.workspace_id != workspace_id:
        return run

    await session.refresh(run, with_for_update=True)

    campaign = await session.get(Campaign, run.campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        run.status = "failed"
        run.finished_at = now
        run.heartbeat_at = now
        run.error_code = "CAMPAIGN_NOT_FOUND"
        run.error_message = "Campaign for this run was not found."
        run.report = {
            **_json_dict(run.report),
            "status": "failed",
            "finished_at": _iso(now),
            "error": {"code": run.error_code, "message": run.error_message},
        }
        await session.commit()
        await session.refresh(run)
        return run

    from app.modules.campaigns.account_replacement import replacement_blocks_launch

    if replacement_blocks_launch(campaign):
        return run
    if run.status in RUN_TERMINAL_STATUSES:
        return run

    stop_conditions = _json_dict(run.stop_conditions or campaign.config.get("stop_conditions"))
    progress_before = _json_dict(run.progress)

    stop_reason = _terminal_stop_reason(run, progress_before, stop_conditions, now)
    if stop_reason:
        return await _complete_run(session, campaign, run, stop_reason, now)

    pending_reason = _pending_reason(stop_conditions, now)
    if pending_reason:
        run.status = "pending"
        campaign.status = "pending"
        run.heartbeat_at = now
        run.report = _build_report(
            campaign,
            "pending",
            progress_before,
            stop_conditions,
            run.started_at or now,
            None,
            now,
            pending_reason,
        )
        _add_run_event(
            session,
            run,
            "info",
            "run_pending",
            "Campaign run is pending because a stop condition prevents an immediate tick.",
            {"reason": pending_reason},
        )
        await session.commit()
        await session.refresh(run)
        return run

    runtime_block_reason = await _campaign_runtime_account_block_reason(session, campaign, run=run)
    if runtime_block_reason:
        return await _fail_run(
            session,
            campaign,
            run,
            "ACCOUNT_SAFETY_BLOCKED",
            runtime_block_reason,
        )

    run.status = "running"
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    campaign.status = "running"
    tick_number = int(progress_before.get("ticks", 0) or 0) + 1
    config = _json_dict(campaign.config)
    selection_mode = _selection_mode_from_config(config)

    context = await _load_comment_context(session, workspace_id, campaign)
    if not context:
        return await _fail_run(
            session,
            campaign,
            run,
            "PROMPT_NOT_FOUND",
            "Comment prompt not found for this campaign.",
        )
    if not context.accounts:
        return await _fail_run(
            session,
            campaign,
            run,
            "NO_ACTIONABLE_ACCOUNTS",
            "В кампании не осталось подключенных Telegram-аккаунтов. Проверьте аккаунты кампании: после чистки заблокированных аккаунтов нужно выбрать аккаунт с живой сессией.",
            alert=False,
        )

    try:
        delivery_mode = _delivery_mode_from_config(config)
        latest_limit = _effective_latest_limit(config, selection_mode)
        _add_run_event(
            session,
            run,
            "info",
            "tick_started",
            "Campaign run tick started.",
            {"tick": tick_number},
        )
        remaining_comments = _remaining_max_comments(stop_conditions, progress_before)
        if remaining_comments == 0:
            return await _complete_run(session, campaign, run, "max_comments", now)
        tick_comment_limit = remaining_comments
        summary = await _process_comment_sources(
            session=session,
            workspace_id=workspace_id,
            campaign=campaign,
            run=run,
            context=context,
            selection_mode=selection_mode,
            delivery_mode=delivery_mode,
            latest_limit=latest_limit,
            max_comments=tick_comment_limit,
            dedupe=True,
            update_cursors=selection_mode == "new",
        )
    except Exception as exc:
        return await _fail_run(session, campaign, run, type(exc).__name__, str(exc))

    tick_finished_at = datetime.now(UTC)
    tick_progress = summary.as_progress()
    progress = _merge_progress(progress_before, tick_progress, tick_finished_at)
    run.progress = progress
    run.heartbeat_at = tick_finished_at
    report_status = "running"

    stop_reason = _terminal_stop_reason(run, progress, stop_conditions, tick_finished_at, summary)
    waiting_reason = _comment_run_waiting_reason(campaign, stop_reason)
    if waiting_reason:
        stop_reason = None
        progress.pop("stop_reason", None)
        progress = {
            **progress,
            "last_wait_reason": waiting_reason,
            "last_wait_reason_at": _iso(tick_finished_at),
        }
        run.progress = progress
    if stop_reason or _run_should_finish(campaign, summary):
        run.status = "completed"
        run.finished_at = tick_finished_at
        campaign.status = "completed"
        report_status = "completed"
        if stop_reason:
            progress = {**progress, "stop_reason": stop_reason}
            run.progress = progress
        _add_run_event(
            session,
            run,
            "info",
            "run_completed",
            "Campaign run completed.",
            {"tick": tick_number, "stop_reason": stop_reason, "progress": tick_progress},
        )
    else:
        run.status = "running"
        campaign.status = "running"
        if waiting_reason:
            _add_run_event(
                session,
                run,
                "info",
                "run_waiting",
                "Кампания достигла временного ограничения и продолжит работу в следующих фоновых тиках.",
                {"tick": tick_number, "reason": waiting_reason, "progress": tick_progress},
            )
        elif _selection_mode_from_config(_json_dict(campaign.config)) != "new" or _has_meaningful_comment_progress(
            tick_progress
        ):
            _add_run_event(
                session,
                run,
                "info",
                "tick_completed",
                "Фоновая проверка источников завершена.",
                {"tick": tick_number, "progress": tick_progress},
            )
        else:
            _add_run_event(
                session,
                run,
                "info",
                "runner_waiting",
                "Проверка завершена, ждем следующий фоновый тик.",
                {"tick": tick_number, "progress": tick_progress},
            )

    run.report = _build_report(
        campaign,
        report_status,
        progress,
        stop_conditions,
        run.started_at or now,
        run.finished_at,
        tick_finished_at,
    )
    await session.commit()
    await session.refresh(run)
    return run


async def process_live_comment_message(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun,
    source: TelegramSource,
    message: TelegramMessage,
    account_id: str | None = None,
) -> CampaignRun:
    now = datetime.now(UTC)
    if campaign.workspace_id != workspace_id or run.workspace_id != workspace_id or run.campaign_id != campaign.id:
        return run

    await session.refresh(run, with_for_update=True)
    await session.refresh(campaign)
    if run.status in RUN_TERMINAL_STATUSES or campaign.status not in {"pending", "running"}:
        return run

    stop_conditions = _json_dict(run.stop_conditions or campaign.config.get("stop_conditions"))
    progress_before = _json_dict(run.progress)
    published_at = _parse_datetime(message.published_at)
    started_at = _parse_datetime(run.started_at)
    if published_at and started_at and published_at < started_at:
        return run

    stop_reason = _terminal_stop_reason(run, progress_before, stop_conditions, now)
    if stop_reason:
        return await _complete_run(session, campaign, run, stop_reason, now)

    runtime_block_reason = await _campaign_runtime_account_block_reason(session, campaign, run=run)
    if runtime_block_reason:
        return await _fail_run(
            session,
            campaign,
            run,
            "ACCOUNT_SAFETY_BLOCKED",
            runtime_block_reason,
        )

    remaining_comments = _remaining_max_comments(stop_conditions, progress_before)
    if remaining_comments == 0:
        return await _complete_run(session, campaign, run, "max_comments", now)

    context = await _load_comment_context(session, workspace_id, campaign)
    if not context:
        return await _fail_run(
            session, campaign, run, "PROMPT_NOT_FOUND", "Comment prompt not found for this campaign."
        )

    if source.id not in {item.id for item in context.sources}:
        return run

    deduped_messages = await _without_existing_actions(session, campaign.id, [message])
    if not deduped_messages:
        return run

    delivery_mode = _delivery_mode_from_config(_json_dict(campaign.config))
    policy = _json_dict(campaign.policy)
    assigned_account = _assigned_account_for_source(source.id, context.accounts, _json_dict(campaign.config))
    if _source_assignment_mode(_json_dict(campaign.config)) in {"manual_pinned", "multi_account_split"}:
        if not assigned_account or (account_id and assigned_account.id != account_id):
            return run
        account_id = assigned_account.id
    send_account = _select_live_comment_account(context.accounts, account_id)
    if delivery_mode == "generate_and_send":
        wait_reason = await _comment_account_send_wait_reason(
            session, workspace_id, send_account, policy, now, campaign=campaign
        )
        if wait_reason:
            run.heartbeat_at = now
            _add_run_event(
                session,
                run,
                "info",
                "live_comment_send_deferred",
                "Комментарий отложен по ограничению аккаунта.",
                {"account_id": account_id, "source_id": source.id, "reason": wait_reason},
            )
            await session.commit()
            return run
    stop_word = _message_stop_word_match(message, policy)
    if stop_word:
        summary = CommentTickSummary(
            sources_checked=1,
            messages_seen=1,
            new_posts_found=1,
            posts_skipped_by_stop_words=1,
        )
        finished_at = datetime.now(UTC)
        progress = _merge_progress(progress_before, summary.as_progress(), finished_at)
        run.status = "running"
        run.started_at = run.started_at or now
        run.heartbeat_at = finished_at
        run.progress = progress
        campaign.status = "running"
        run.report = _build_report(
            campaign,
            "running",
            progress,
            stop_conditions,
            run.started_at or now,
            run.finished_at,
            finished_at,
        )
        _add_run_event(
            session,
            run,
            "info",
            "live_comment_stop_word_skipped",
            f"Пост пропущен по стоп-слову: {stop_word}.",
            {
                "source_id": source.id,
                "message_id": message.id,
                "stop_word": stop_word,
            },
        )
        await session.commit()
        return run

    if not _message_passes_post_length(message, policy):
        has_media = _message_has_media_context(message)
        min_chars = _comment_min_chars_for_message(message, policy)
        post_kind = "медиа-подпись" if has_media else "текст"
        summary = CommentTickSummary(
            sources_checked=1,
            messages_seen=1,
            new_posts_found=1,
            posts_skipped_by_length=1,
        )
        finished_at = datetime.now(UTC)
        progress = _merge_progress(progress_before, summary.as_progress(), finished_at)
        run.status = "running"
        run.started_at = run.started_at or now
        run.heartbeat_at = finished_at
        run.progress = progress
        campaign.status = "running"
        run.report = _build_report(
            campaign,
            "running",
            progress,
            stop_conditions,
            run.started_at or now,
            run.finished_at,
            finished_at,
        )
        _add_run_event(
            session,
            run,
            "info",
            "live_comment_length_skipped",
            f"Пост пропущен: {post_kind} короче {min_chars} символов.",
            {
                "source_id": source.id,
                "message_id": message.id,
                "min_post_chars": min_chars,
                "post_kind": post_kind,
                "has_media": has_media,
                "post_chars": _message_text_chars(message),
            },
        )
        await session.commit()
        return run

    interval_rule_active = _comment_post_interval_active(policy)
    config_state = _json_dict(campaign.config)
    interval_counters, interval_targets, interval_state_reset = _comment_interval_state(config_state)
    account = _select_live_comment_account(context.accounts, account_id)

    if interval_rule_active:
        should_comment, interval_counters, interval_targets = _next_comment_post_decision(
            policy=policy,
            counters=interval_counters,
            targets=interval_targets,
            source_id=source.id,
        )
        campaign.config = {
            **_json_dict(campaign.config),
            "comment_post_interval_counters": interval_counters,
            "comment_post_interval_targets": interval_targets,
            "comment_post_interval_state_version": COMMENT_POST_INTERVAL_STATE_VERSION,
        }
    elif interval_state_reset:
        campaign.config = {
            **_json_dict(campaign.config),
            "comment_post_interval_counters": {},
            "comment_post_interval_targets": {},
            "comment_post_interval_state_version": COMMENT_POST_INTERVAL_STATE_VERSION,
        }
    else:
        should_comment = True

    if not should_comment:
        summary = CommentTickSummary(
            sources_checked=1,
            messages_seen=1,
            new_posts_found=1,
            posts_skipped_by_interval=1,
        )
        finished_at = datetime.now(UTC)
        progress = _merge_progress(progress_before, summary.as_progress(), finished_at)
        run.status = "running"
        run.started_at = run.started_at or now
        run.heartbeat_at = finished_at
        run.progress = progress
        campaign.status = "running"
        run.report = _build_report(
            campaign,
            "running",
            progress,
            stop_conditions,
            run.started_at or now,
            run.finished_at,
            finished_at,
        )
        _add_run_event(
            session,
            run,
            "info",
            "live_comment_interval_skipped",
            f"Пост пропущен по правилу: {_comment_post_interval_label(policy)}.",
            {
                "source_id": source.id,
                "message_id": message.id,
                "comment_post_interval": _comment_post_interval(policy),
                "comment_post_interval_mode": _comment_post_interval_mode(policy),
                "counter": interval_counters.get(source.id),
                "target": interval_targets.get(source.id),
            },
        )
        await session.commit()
        return run

    result = await _create_comment_action(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        run=run,
        prompt=context.prompt,
        source=source,
        message=message,
        account=account,
        delivery_mode=delivery_mode,
        now=now,
    )

    summary = CommentTickSummary(
        sources_checked=1,
        messages_seen=1,
        new_posts_found=1,
        comments_generated=1,
        comments_sent=1 if result["comment_status"] == "sent" else 0,
        comments_drafted=1 if result["comment_status"] == "draft" else 0,
        comments_failed=1 if result["comment_status"] == "failed" else 0,
        actions_created=1,
        successful_actions=1 if result["action_status"] in {"succeeded", "generated"} else 0,
    )
    finished_at = datetime.now(UTC)
    progress = _merge_progress(progress_before, summary.as_progress(), finished_at)
    run.status = "running"
    run.started_at = run.started_at or now
    run.heartbeat_at = finished_at
    run.progress = progress
    campaign.status = "running"
    run.report = _build_report(
        campaign,
        "running",
        progress,
        stop_conditions,
        run.started_at or now,
        run.finished_at,
        finished_at,
    )
    _add_run_event(
        session,
        run,
        "error" if result["comment_status"] == "failed" else "info",
        "live_comment_failed" if result["comment_status"] == "failed" else "live_comment_created",
        "Новый пост обработан live-handler.",
        {
            "source_id": source.id,
            "message_id": message.id,
            "comment_status": result["comment_status"],
            "delivery_mode": delivery_mode,
        },
    )

    stop_reason = _terminal_stop_reason(run, progress, stop_conditions, finished_at, summary)
    if stop_reason and not _continuous_live_neuro_campaign(campaign, stop_reason):
        return await _complete_run(session, campaign, run, stop_reason, finished_at)

    await session.commit()
    await session.refresh(run)
    return run


def _select_live_comment_account(accounts: list[TelegramAccount], account_id: str | None) -> TelegramAccount | None:
    if account_id:
        for account in accounts:
            if account.id == account_id:
                return account
    return accounts[0] if accounts else None


async def start_generic_campaign_run(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
) -> CampaignRun:
    if campaign.config.get("recipient_mode") == "saved_contacts":
        from app.modules.contact_broadcasts.runner import start

        return await start(session, workspace_id, campaign)
    existing = await session.execute(
        select(CampaignRun)
        .where(
            CampaignRun.workspace_id == workspace_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(["pending", "running", "stopping"]),
        )
        .order_by(CampaignRun.created_at.desc())
        .limit(1)
    )
    active_run = existing.scalar_one_or_none()
    now = datetime.now(UTC)
    if active_run:
        stop_conditions = _json_dict(active_run.stop_conditions or campaign.config.get("stop_conditions"))
        progress = _json_dict(active_run.progress)
        active_run.status = "running"
        active_run.started_at = active_run.started_at or now
        active_run.heartbeat_at = now
        active_run.report = _build_report(
            campaign,
            "running",
            progress,
            stop_conditions,
            active_run.started_at or now,
            active_run.finished_at,
            now,
        )
        campaign.status = "running"
        _add_run_event(
            session,
            active_run,
            "info",
            "live_listener_active",
            "Кампания слушает новые сообщения в реальном времени.",
            {"campaign_type": campaign.type},
        )
        await session.commit()
        await session.refresh(active_run)
        return active_run

    stop_conditions = _json_dict(campaign.config.get("stop_conditions"))
    run = CampaignRun(
        workspace_id=workspace_id,
        campaign_id=campaign.id,
        status="running",
        started_at=now,
        heartbeat_at=now,
        stop_conditions=stop_conditions,
        progress=_empty_progress(),
        report=_build_report(campaign, "running", _empty_progress(), stop_conditions, now, None, now),
    )
    session.add(run)
    await session.flush()
    _add_run_event(
        session,
        run,
        "info",
        "run_created",
        "Кампания запущена и слушает новые сообщения в реальном времени.",
        {"campaign_type": campaign.type, "delivery_mode": _delivery_mode_from_config(_json_dict(campaign.config))},
    )
    campaign.status = "running"
    await session.commit()
    await session.refresh(run)
    return run


async def tick_generic_campaign_run(
    session: AsyncSession,
    workspace_id: str,
    run: CampaignRun,
) -> CampaignRun:
    now = datetime.now(UTC)
    campaign = await session.get(Campaign, run.campaign_id)
    if campaign and campaign.workspace_id == workspace_id and campaign.config.get("recipient_mode") == "saved_contacts":
        from app.modules.contact_broadcasts.runner import tick

        return await tick(session, workspace_id, run)
    if not campaign or campaign.workspace_id != workspace_id:
        return await _fail_run(
            session,
            Campaign(workspace_id=workspace_id, type="unknown", name="missing"),
            run,
            "CAMPAIGN_NOT_FOUND",
            "Campaign was not found",
        )
    if campaign.type == "comment_posts":
        return await tick_comment_posts_campaign_run(session, workspace_id, run)
    if campaign.type not in {"neuro_chat", "neuro_dialog", "message_broadcast", "mass_reactions"}:
        return await _fail_run(
            session, campaign, run, "UNSUPPORTED_CAMPAIGN_TYPE", f"Unsupported campaign type: {campaign.type}"
        )
    if run.status in RUN_TERMINAL_STATUSES:
        return run

    stop_conditions = _json_dict(run.stop_conditions or campaign.config.get("stop_conditions"))
    progress_before = _json_dict(run.progress)
    stop_reason = _terminal_stop_reason(run, progress_before, stop_conditions, now)
    if stop_reason and not _continuous_live_neuro_campaign(campaign, stop_reason):
        return await _complete_run(session, campaign, run, stop_reason, now)
    pending_reason = _pending_reason(stop_conditions, now)
    if pending_reason:
        run.status = "pending"
        campaign.status = "pending"
        run.heartbeat_at = now
        run.report = _build_report(
            campaign, "pending", progress_before, stop_conditions, run.started_at or now, None, now, pending_reason
        )
        await session.commit()
        await session.refresh(run)
        return run

    runtime_block_reason = await _campaign_runtime_account_block_reason(session, campaign, run=run)
    if runtime_block_reason:
        return await _fail_run(
            session,
            campaign,
            run,
            "ACCOUNT_SAFETY_BLOCKED",
            runtime_block_reason,
        )

    run.status = "running"
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    campaign.status = "running"
    tick_number = int(progress_before.get("ticks", 0) or 0) + 1
    _add_run_event(session, run, "info", "tick_started", "Проверка сообщений началась.", {"tick": tick_number})

    try:
        remaining = _remaining_max_comments(stop_conditions, progress_before)
        if remaining == 0:
            return await _complete_run(session, campaign, run, "max_comments", now)
        if campaign.type == "mass_reactions":
            summary = await _process_mass_reactions(session, workspace_id, campaign, remaining)
        elif campaign.type == "message_broadcast":
            summary = await _process_message_broadcast(session, workspace_id, campaign, remaining)
        else:
            summary = await _process_neuro_messages(session, workspace_id, campaign, remaining)
    except Exception as exc:
        return await _fail_run(session, campaign, run, type(exc).__name__, str(exc))

    tick_finished_at = datetime.now(UTC)
    tick_progress = summary.as_progress()
    progress = _merge_progress(progress_before, tick_progress, tick_finished_at)
    run.progress = progress
    run.heartbeat_at = tick_finished_at
    stop_reason = _terminal_stop_reason(run, progress, stop_conditions, tick_finished_at, summary)
    continuous = bool(_json_dict(campaign.config).get("continuous", True))
    if campaign.type == "mass_reactions" and int(tick_progress.get("reactions_planned", 0) or 0) > int(
        tick_progress.get("reactions_sent", 0) or 0
    ):
        continuous = True
    if campaign.type == "mass_reactions" and await _has_pending_reaction_actions(session, campaign.id):
        continuous = True
    if stop_reason and _continuous_live_neuro_campaign(campaign, stop_reason):
        stop_reason = None
    if stop_reason or not continuous:
        run.status = "completed"
        run.finished_at = tick_finished_at
        campaign.status = "completed"
        report_status = "completed"
        _add_run_event(
            session, run, "info", "run_completed", "Запуск завершен.", {"tick": tick_number, "progress": tick_progress}
        )
    else:
        run.status = "running"
        campaign.status = "running"
        report_status = "running"
        _add_run_event(
            session,
            run,
            "info",
            "runner_waiting",
            "Проверка завершена, ждем следующий фоновый тик.",
            {"tick": tick_number, "progress": tick_progress},
        )
    run.report = _build_report(
        campaign, report_status, progress, stop_conditions, run.started_at or now, run.finished_at, tick_finished_at
    )
    await session.commit()
    await session.refresh(run)
    return run


async def tick_active_campaign_runs(
    session: AsyncSession,
    workspace_id: str | None = None,
    *,
    limit: int = 20,
) -> list[CampaignRun]:
    now = datetime.now(UTC)
    tick_cutoff = now - timedelta(seconds=SCHEDULED_CAMPAIGN_TICK_MIN_INTERVAL_SECONDS)
    statement = (
        select(CampaignRun, Campaign)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .where(CampaignRun.status.in_(RUN_TICKABLE_STATUSES))
        .where(or_(CampaignRun.heartbeat_at.is_(None), CampaignRun.heartbeat_at <= tick_cutoff))
        .order_by(CampaignRun.heartbeat_at.asc().nulls_first(), CampaignRun.created_at.asc())
        .limit(limit)
    )
    if workspace_id:
        statement = statement.where(CampaignRun.workspace_id == workspace_id)
    statement = statement.with_for_update(skip_locked=True)
    result = await session.execute(statement)
    pairs = list(result.all())
    ticked: list[CampaignRun] = []
    ticked_account_keys: set[tuple[str, str]] = set()
    for run, campaign in pairs:
        account_keys = _campaign_account_conflict_keys(campaign)
        if account_keys & ticked_account_keys:
            continue
        ticked.append(await tick_generic_campaign_run(session, run.workspace_id, run))
        ticked_account_keys.update(account_keys)
    return ticked


def _campaign_account_conflict_keys(campaign: Campaign) -> set[tuple[str, str]]:
    return campaign_account_conflict_keys(campaign.type, _campaign_account_ids(campaign))


def _campaign_account_ids(campaign: Campaign) -> set[str]:
    config = _json_dict(campaign.config)
    account_ids = {str(item) for item in config.get("account_ids", []) if isinstance(item, str) and item}
    account_id = config.get("account_id")
    if isinstance(account_id, str) and account_id:
        account_ids.add(account_id)
    return account_ids


async def _campaign_runtime_account_block_reason(
    session: AsyncSession,
    campaign: Campaign,
    *,
    run: CampaignRun | None = None,
) -> str | None:
    module = _module_for_campaign_type(campaign.type)
    account_ids = _campaign_account_ids(campaign)
    if not module or not account_ids:
        return None
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == campaign.workspace_id,
            TelegramAccount.id.in_(account_ids),
        )
    )
    blocked: list[tuple[TelegramAccount, str]] = []
    removable: list[tuple[TelegramAccount, str]] = []
    for account in result.scalars().all():
        eligibility = get_module_eligibility(account, module)
        if eligibility.status == "blocked":
            reason = eligibility.message or eligibility.reason or "blocked"
            if _campaign_account_block_is_removable(account, eligibility.reason):
                removable.append((account, reason))
            else:
                blocked.append((account, reason))
    if removable:
        removal_stop_reason = _remove_blocked_accounts_from_campaign(session, campaign, removable, run=run)
        if removal_stop_reason:
            return removal_stop_reason
    if not blocked:
        return None
    blocked_labels = [f"{_account_log_name(account)}: {reason}" for account, reason in blocked]
    return "Кампания остановлена safety-проверкой аккаунтов. " + "; ".join(blocked_labels[:5])


def _campaign_account_block_is_removable(account: TelegramAccount, reason: str | None) -> bool:
    blocked_statuses = {"archived", "banned", "blocked", "deleted", "frozen", "invalid", "quarantined", "spam_blocked"}
    if account.status in blocked_statuses:
        return True
    if account.restriction_status in {"limited", "blocked", "frozen"}:
        return True
    return bool(reason and (reason.startswith("account_status_") or reason.startswith("telegram_restriction_")))


def _remove_blocked_accounts_from_campaign(
    session: AsyncSession,
    campaign: Campaign,
    blocked_accounts: list[tuple[TelegramAccount, str]],
    *,
    run: CampaignRun | None = None,
) -> str | None:
    config = copy.deepcopy(_json_dict(campaign.config))
    list_account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str) and item]
    single_account_id = config.get("account_id")
    single_account_ids = [single_account_id] if isinstance(single_account_id, str) and single_account_id else []
    configured_account_ids = list(dict.fromkeys([*list_account_ids, *single_account_ids]))
    removed_ids = {account.id for account, _ in blocked_accounts}
    remaining_ids = [account_id for account_id in configured_account_ids if account_id not in removed_ids]
    removed_payload = [
        {
            "account_id": account.id,
            "account": _account_log_name(account),
            "status": account.status,
            "restriction_status": account.restriction_status,
            "reason": reason,
            "removed_at": _iso(datetime.now(UTC)),
        }
        for account, reason in blocked_accounts
        if account.id in configured_account_ids
    ]
    if not removed_payload:
        return None
    if not remaining_ids:
        labels = [f"{item['account']}: {item['reason']}" for item in removed_payload]
        return "Кампания остановлена: все аккаунты кампании заблокированы или заморожены. " + "; ".join(labels[:5])

    if isinstance(config.get("account_ids"), list):
        config["account_ids"] = [account_id for account_id in list_account_ids if account_id not in removed_ids]
    if isinstance(single_account_id, str) and single_account_id in removed_ids:
        config.pop("account_id", None)
    assignment = _json_dict(config.get("source_assignment"))
    if assignment:
        config["source_assignment"] = {
            account_id: source_ids
            for account_id, source_ids in assignment.items()
            if str(account_id) not in removed_ids
        }
    existing_removed = config.get("removed_accounts")
    removed_history = existing_removed if isinstance(existing_removed, list) else []
    config["removed_accounts"] = [*removed_history, *removed_payload][-50:]
    campaign.config = config
    if run:
        _add_run_event(
            session,
            run=run,
            level="warning",
            event_type="campaign_account_removed",
            message=(
                "Замороженный или заблокированный аккаунт удален из кампании, остальные аккаунты продолжают работу."
            ),
            payload={"removed_accounts": removed_payload, "remaining_account_ids": remaining_ids},
        )
    return None


def _module_for_campaign_type(campaign_type: str) -> str | None:
    if campaign_type == "comment_posts":
        return "neurocommenting"
    if campaign_type in {"neuro_chat", "neuro_dialog"}:
        return "neurochatting"
    if campaign_type == "mass_reactions":
        return "mass_reactions"
    if campaign_type == "message_broadcast":
        return "message_broadcast"
    return None


def _campaign_uses_live_listener(campaign: Campaign) -> bool:
    return False


def _continuous_live_neuro_campaign(campaign: Campaign, stop_reason: str | None) -> bool:
    if campaign.type not in {"neuro_chat", "neuro_dialog"}:
        return False
    if stop_reason not in {None, "max_comments"}:
        return False
    return bool(_json_dict(campaign.config).get("continuous", True))


async def _process_neuro_messages(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    max_replies: int | None,
) -> ChatTickSummary:
    config = _json_dict(campaign.config)
    policy = _json_dict(campaign.policy)
    delivery_mode = _delivery_mode_from_config(config)
    account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]
    accounts_result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.id.in_(account_ids))
    )
    accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
    accounts = [accounts_by_id[account_id] for account_id in account_ids if account_id in accounts_by_id]
    if not accounts:
        raise ValueError("Telegram account is required")

    refs: list[tuple[str, str | None]] = []
    if campaign.type == "neuro_chat":
        source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
        sources_result = await session.execute(
            select(TelegramSource).where(TelegramSource.workspace_id == workspace_id, TelegramSource.id.in_(source_ids))
        )
        sources_by_id = {source.id: source for source in sources_result.scalars().all()}
        for source_id in source_ids:
            source = sources_by_id.get(source_id)
            if source:
                refs.append((_source_peer_ref(source), source.title))
    else:
        refs = [(str(ref), str(ref)) for ref in config.get("target_refs", []) if isinstance(ref, str) and ref.strip()]
    if not refs:
        raise ValueError("No chat targets configured")

    summary = ChatTickSummary()
    account_cycle = cycle(accounts)
    messages_limit = int(config.get("messages_per_source") or config.get("messages_per_dialog") or 30)
    min_quality_score = int(config.get("min_quality_score") or 50)
    trigger_keywords = _trigger_keywords_from_config(config)
    if campaign.type == "neuro_chat" and trigger_keywords:
        min_quality_score = 0
    max_per_tick = int(config.get("max_replies_per_tick") or 5)
    if max_replies is not None:
        max_per_tick = min(max_per_tick, max_replies)
    prompt_text = str(config.get("prompt_text") or "Ответь уместно и коротко.")
    tone = str(config.get("tone") or "живой")
    context_text = str(config.get("context_text") or "").strip() or None
    reply_to_only = bool(config.get("reply_to_only", campaign.type == "neuro_chat"))
    only_incoming = bool(config.get("only_reply_to_incoming", campaign.type == "neuro_dialog"))
    max_length = int(policy.get("max_length", 280) or 280)
    delay_min = max(0, _safe_int(policy.get("delay_seconds_min"), default=0))
    delay_max = max(delay_min, _safe_int(policy.get("delay_seconds_max"), default=delay_min))
    delay_min, delay_max, behavior_snapshot = apply_behavior_delay_range(
        policy,
        accounts,
        delay_min,
        delay_max,
    )

    due_summary = await _execute_due_neuro_reply_actions(
        session=session,
        campaign=campaign,
        accounts_by_id=accounts_by_id,
        now=datetime.now(UTC),
        max_replies=max_replies,
    )
    summary = due_summary
    await session.flush()
    if max_replies is not None:
        max_replies = max(0, max_replies - due_summary.successful_actions)
        max_per_tick = min(max_per_tick, max_replies)

    for peer_ref, title in refs:
        if summary.successful_actions >= max_per_tick:
            summary.stop_reason = "max_comments"
            break
        summary.sources_checked += 1
        parser_account = accounts[0]
        messages = await fetch_chat_messages_with_account(session, parser_account, peer_ref, messages_limit)
        summary.messages_seen += len(messages)
        candidates = []
        for message in messages:
            if only_incoming and message.outgoing:
                continue
            text = (message.text or "").strip()
            score, reasons = _message_quality_score(text, bool(message.outgoing), campaign.type)
            summary.messages_scored += 1
            trigger_match = _neuro_chat_trigger_match(text, trigger_keywords) if campaign.type == "neuro_chat" else None
            if campaign.type == "neuro_chat" and trigger_keywords and not trigger_match:
                continue
            if score < min_quality_score:
                continue
            dedupe_key = f"{peer_ref}:{message.tg_message_id}"
            if await _campaign_action_exists(session, campaign.id, dedupe_key):
                continue
            candidates.append((score, reasons, message))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for score, reasons, message in candidates:
            if summary.successful_actions >= max_per_tick:
                summary.stop_reason = "max_comments"
                break
            summary.messages_selected += 1
            account = next(account_cycle)
            status = await _create_neuro_reply_action(
                session=session,
                workspace_id=workspace_id,
                campaign=campaign,
                account=account,
                peer_ref=peer_ref,
                title=title,
                message=message,
                score=score,
                reasons=reasons,
                prompt_text=prompt_text,
                context_text=context_text,
                tone=tone,
                max_length=max_length,
                delivery_mode=delivery_mode,
                reply_to_only=reply_to_only,
                delay_seconds_min=delay_min,
                delay_seconds_max=delay_max,
                behavior_snapshot=behavior_snapshot,
                match_metadata=trigger_match,
            )
            if status == "skipped":
                continue
            summary.actions_created += 1
            if status == "failed":
                summary.replies_failed += 1
            elif status == "queued":
                summary.replies_generated += 1
            else:
                summary.replies_generated += 1
                summary.successful_actions += 1
                if delivery_mode == "draft_only":
                    summary.replies_drafted += 1
                else:
                    summary.replies_sent += 1
    return summary


async def _execute_due_neuro_reply_actions(
    *,
    session: AsyncSession,
    campaign: Campaign,
    accounts_by_id: dict[str, TelegramAccount],
    now: datetime,
    max_replies: int | None,
) -> ChatTickSummary:
    action_types = (
        ["neuro_dialog_reply"]
        if campaign.type == "neuro_dialog"
        else ["neuro_chat_reply"]
        if campaign.type == "neuro_chat"
        else ["neuro_chat_reply", "neuro_dialog_reply"]
    )
    statement = (
        select(CampaignAction)
        .where(
            CampaignAction.campaign_id == campaign.id,
            CampaignAction.action_type.in_(action_types),
            CampaignAction.status == "queued",
            CampaignAction.planned_at <= now,
        )
        .order_by(CampaignAction.planned_at.asc(), CampaignAction.created_at.asc())
    )
    if max_replies is not None:
        statement = statement.limit(max_replies)
    statement = statement.with_for_update(skip_locked=True)
    result = await session.execute(statement)
    actions = list(result.scalars().all())
    summary = ChatTickSummary()
    processed_targets: set[str] = set()
    for action in actions:
        if action.target_id in processed_targets or await _completed_neuro_reply_exists(
            session,
            campaign.id,
            action.target_id,
            exclude_action_id=action.id,
        ):
            action.status = "skipped"
            action.executed_at = now
            action.error_code = "DUPLICATE_REPLY"
            action.error_message = "Ответ на это сообщение уже был обработан."
            action.result = {**_json_dict(action.result), "send": {"mode": "skipped", "reason": "duplicate_reply"}}
            continue
        action_result = _json_dict(action.result)
        account = accounts_by_id.get(str(action.telegram_account_id or ""))
        if not account:
            action.status = "failed"
            action.executed_at = now
            action.error_code = "ACCOUNT_NOT_FOUND"
            action.error_message = "Аккаунт для отложенного ответа больше не найден."
            summary.replies_failed += 1
            continue
        if not str(action_result.get("reply_text") or "").strip():
            reply_text, usage = await _generate_neuro_reply_for_action(
                session=session,
                workspace_id=campaign.workspace_id,
                campaign=campaign,
                account=account,
                action_result=action_result,
            )
            action_result = {
                **action_result,
                "reply_text": reply_text,
                "usage": {**_json_dict(action_result.get("usage")), **usage},
            }
            action.result = action_result
            if reply_text.strip().upper() == "SKIP":
                action.status = "skipped"
                action.executed_at = now
                action.result = {**action_result, "send": {"mode": "skipped"}}
                continue
        delivery = await _deliver_neuro_reply_action(
            session=session,
            account=account,
            peer_ref=str(action_result.get("peer_ref") or ""),
            reply_text=str(action_result.get("reply_text") or ""),
            business_intent=f"neuro:{campaign.id}:{action.target_id}",
            reply_to_tg_message_id=(
                str(action_result.get("reply_to_tg_message_id"))
                if action_result.get("reply_to_tg_message_id")
                else None
            ),
        )
        action.status = delivery.status
        action.executed_at = delivery.sent_at or now
        action.error_code = delivery.error_code
        action.error_message = delivery.error_message
        action.result = {**action_result, "sent_message_id": delivery.sent_message_id, "send": delivery.send_raw}
        if delivery.status == "failed":
            summary.replies_failed += 1
        else:
            summary.replies_sent += 1
            summary.successful_actions += 1
            processed_targets.add(action.target_id)
    return summary


async def _deliver_neuro_reply_action(
    *,
    session: AsyncSession,
    account: TelegramAccount,
    peer_ref: str,
    reply_text: str,
    reply_to_tg_message_id: str | None,
    business_intent: str,
) -> NeuroReplyDeliveryResult:
    now = datetime.now(UTC)
    if not real_telegram_enabled():
        return NeuroReplyDeliveryResult(
            status="succeeded",
            sent_at=now,
            sent_message_id=f"mock-message-{reply_to_tg_message_id or 'direct'}",
            send_raw={"mode": "mock_send"},
        )
    try:
        sent = await send_chat_reply_with_account(
            session,
            account,
            peer_ref,
            reply_text,
            reply_to_tg_message_id=reply_to_tg_message_id,
            business_intent=business_intent,
        )
        return NeuroReplyDeliveryResult(
            status="succeeded",
            sent_at=now,
            sent_message_id=sent.tg_message_id,
            send_raw=sent.raw,
        )
    except TelegramGatewayError as exc:
        return NeuroReplyDeliveryResult(
            status="failed",
            sent_at=now,
            sent_message_id=None,
            send_raw={"mode": "telethon", "error": {"code": exc.code, **exc.raw}},
            error_code=exc.code,
            error_message=str(exc),
        )


async def _process_message_broadcast(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    max_messages: int | None,
) -> ChatTickSummary:
    config = _json_dict(campaign.config)
    policy = _json_dict(campaign.policy)
    account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]
    source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
    message_text = str(config.get("message_text") or "").strip()
    if not message_text:
        raise ValueError("Текст рассылки не задан")

    accounts_result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.id.in_(account_ids))
    )
    accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
    accounts = [accounts_by_id[account_id] for account_id in account_ids if account_id in accounts_by_id]
    if not accounts:
        raise ValueError("Для рассылки нужен минимум один Telegram аккаунт")

    sources_result = await session.execute(
        select(TelegramSource).where(TelegramSource.workspace_id == workspace_id, TelegramSource.id.in_(source_ids))
    )
    sources_by_id = {source.id: source for source in sources_result.scalars().all()}
    sources = [sources_by_id[source_id] for source_id in source_ids if source_id in sources_by_id]
    if not sources:
        raise ValueError("Для рассылки нужен минимум один диалог")

    summary = ChatTickSummary()
    account_cycle = cycle(accounts)
    daily_limit = max(
        1,
        min(_safe_int(policy.get("daily_limit_per_account"), default=3), BROADCAST_DAILY_LIMIT_HARD_CAP),
    )
    daily_sent_by_account: dict[str, int] = {account.id: 0 for account in accounts}
    max_to_send = (
        max_messages
        if max_messages is not None
        else int(_json_dict(config.get("stop_conditions")).get("max_messages") or len(sources))
    )
    max_to_send = max(1, min(max_to_send, len(sources) * max(1, len(accounts))))
    delay_min = max(900, _safe_int(policy.get("delay_seconds_min"), default=900))
    delay_max = _safe_int(policy.get("delay_seconds_max"), default=delay_min)
    if delay_max < delay_min:
        delay_max = delay_min
    delay_min, delay_max, behavior_snapshot = apply_behavior_delay_range(
        policy,
        accounts,
        delay_min,
        delay_max,
        floor_min=900,
    )

    due_summary = await _execute_due_broadcast_actions(
        session=session,
        campaign=campaign,
        accounts_by_id=accounts_by_id,
        now=datetime.now(UTC),
        max_messages=max_to_send,
    )
    summary = due_summary
    await session.flush()
    max_to_send = max(0, max_to_send - due_summary.successful_actions)
    sent_or_planned = 0
    next_planned_at = datetime.now(UTC)
    for source in sources:
        if sent_or_planned >= max_to_send:
            summary.stop_reason = "max_comments"
            break
        summary.sources_checked += 1
        summary.messages_seen += 1
        summary.broadcast_targets += 1
        peer_ref = _source_peer_ref(source)
        if await _campaign_action_exists(session, campaign.id, peer_ref):
            continue

        assigned_account = _assigned_account_for_source(source.id, accounts, config)
        account = assigned_account or next(account_cycle)
        account_rotations = 0
        while (
            not assigned_account
            and daily_sent_by_account.get(account.id, 0) >= daily_limit
            and account_rotations < len(accounts)
        ):
            account = next(account_cycle)
            account_rotations += 1
        if daily_sent_by_account.get(account.id, 0) >= daily_limit:
            summary.stop_reason = "daily_limit"
            break

        now = datetime.now(UTC)
        delay_seconds = random.randint(delay_min, delay_max)
        planned_at = max(next_planned_at, now) + timedelta(seconds=delay_seconds)
        next_planned_at = planned_at
        session.add(
            CampaignAction(
                campaign_id=campaign.id,
                telegram_account_id=account.id,
                target_type="dialog",
                target_id=peer_ref,
                action_type="message_broadcast",
                status="queued",
                planned_at=planned_at,
                executed_at=None,
                result={
                    "peer_ref": peer_ref,
                    "chat_title": source.title,
                    "message_text": message_text,
                    "reply_text": message_text,
                    "sent_message_id": None,
                    "delivery_mode": "generate_and_send",
                    "send": {
                        "mode": "queued",
                        "delay_seconds": int((planned_at - now).total_seconds()),
                        "spacing_delay_seconds": delay_seconds,
                        "behavior_protection": behavior_snapshot,
                        "assignment_mode": _source_assignment_mode(config),
                    },
                    "behavior_protection": behavior_snapshot,
                    "assignment_mode": _source_assignment_mode(config),
                },
                error_code=None,
                error_message=None,
            )
        )
        summary.messages_selected += 1
        summary.actions_created += 1
        sent_or_planned += 1
        summary.replies_generated += 1
        daily_sent_by_account[account.id] = daily_sent_by_account.get(account.id, 0) + 1
        await session.flush()

    return summary


async def _execute_due_broadcast_actions(
    *,
    session: AsyncSession,
    campaign: Campaign,
    accounts_by_id: dict[str, TelegramAccount],
    now: datetime,
    max_messages: int | None,
) -> ChatTickSummary:
    summary = ChatTickSummary()
    statement = (
        select(CampaignAction)
        .where(
            CampaignAction.campaign_id == campaign.id,
            CampaignAction.action_type == "message_broadcast",
            CampaignAction.status == "queued",
            CampaignAction.planned_at <= now,
        )
        .order_by(CampaignAction.planned_at.asc(), CampaignAction.created_at.asc())
    )
    if max_messages is not None:
        statement = statement.limit(max_messages)
    statement = statement.with_for_update(skip_locked=True)
    result = await session.execute(statement)
    actions = list(result.scalars().all())
    for action in actions:
        action_result = _json_dict(action.result)
        account = accounts_by_id.get(str(action.telegram_account_id or ""))
        if not account:
            action.status = "failed"
            action.executed_at = now
            action.error_code = "ACCOUNT_NOT_FOUND"
            action.error_message = "Аккаунт для отложенной рассылки больше не найден."
            summary.replies_failed += 1
            summary.broadcast_failed += 1
            continue

        peer_ref = str(action_result.get("peer_ref") or action.target_id or "")
        message_text = str(action_result.get("message_text") or action_result.get("reply_text") or "")
        sent_message_id = None
        if real_telegram_enabled():
            try:
                sent = await send_chat_reply_with_account(
                    session,
                    account,
                    peer_ref,
                    message_text,
                    reply_to_tg_message_id=None,
                    business_intent=f"broadcast:{action.id}",
                )
                sent_message_id = sent.tg_message_id
                send_raw = sent.raw
                status = "succeeded"
                error_code = None
                error_message = None
            except TelegramGatewayError as exc:
                status = "failed"
                error_code = exc.code
                error_message = str(exc)
                send_raw = {"mode": "telethon", "error": {"code": exc.code, **exc.raw}}
        else:
            status = "succeeded"
            error_code = None
            error_message = None
            sent_message_id = f"mock-broadcast-{str(action.id)[:8]}"
            send_raw = {"mode": "mock_send"}

        action.status = status
        action.executed_at = now
        action.error_code = error_code
        action.error_message = error_message
        action.result = {
            **action_result,
            "sent_message_id": sent_message_id,
            "send": send_raw,
        }
        if status == "failed":
            summary.replies_failed += 1
            summary.broadcast_failed += 1
        else:
            summary.replies_sent += 1
            summary.broadcast_sent += 1
            summary.successful_actions += 1
    return summary


async def _execute_due_reaction_actions(
    *,
    session: AsyncSession,
    campaign: Campaign,
    accounts_by_id: dict[str, TelegramAccount],
    sources_by_id: dict[str, TelegramSource],
    now: datetime,
    max_reactions: int | None,
) -> ReactionTickSummary:
    summary = ReactionTickSummary()
    statement = (
        select(CampaignAction)
        .where(
            CampaignAction.campaign_id == campaign.id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status == "queued",
            CampaignAction.planned_at <= now,
        )
        .order_by(CampaignAction.planned_at.asc(), CampaignAction.created_at.asc())
    )
    if max_reactions is not None:
        statement = statement.limit(max_reactions)
    statement = statement.with_for_update(skip_locked=True)
    result = await session.execute(statement)
    actions = list(result.scalars().all())
    for action in actions:
        action_result = _json_dict(action.result)
        account = accounts_by_id.get(str(action.telegram_account_id or ""))
        source = sources_by_id.get(str(action_result.get("source_id") or ""))
        if not account:
            action.status = "failed"
            action.executed_at = now
            action.error_code = "ACCOUNT_NOT_FOUND"
            action.error_message = "Аккаунт для отложенной реакции больше не найден."
            summary.reactions_failed += 1
            continue
        send_result = await _send_reaction_target(
            session=session,
            account=account,
            source=source,
            peer_ref=str(action_result.get("reaction_peer_ref") or action_result.get("source_ref") or ""),
            tg_message_id=str(action_result.get("tg_message_id") or ""),
            reaction=str(action_result.get("reaction") or "👍"),
            react_as=str(action_result.get("react_as") or "account"),
        )
        action.status = send_result.status
        action.executed_at = send_result.sent_at or now
        action.error_code = send_result.error_code
        action.error_message = send_result.error_message
        action.result = {**action_result, "send": send_result.send_raw}
        if send_result.status == "failed":
            summary.reactions_failed += 1
        else:
            summary.reactions_sent += 1
            summary.successful_actions += 1
    return summary


async def _send_reaction_target(
    *,
    session: AsyncSession,
    account: TelegramAccount,
    source: TelegramSource | None,
    peer_ref: str,
    tg_message_id: str,
    reaction: str,
    react_as: str,
) -> ReactionSendResult:
    now = datetime.now(UTC)
    if not real_telegram_enabled():
        return ReactionSendResult(
            status="succeeded",
            sent_at=now,
            send_raw={"mode": "mock_send", "reaction_peer_ref": peer_ref},
        )
    try:
        sent = await react_to_message_with_account(
            session,
            account,
            peer_ref,
            tg_message_id,
            reaction,
            react_as=react_as,
        )
        return ReactionSendResult(status="succeeded", sent_at=now, send_raw=sent.raw)
    except TelegramGatewayError as exc:
        _apply_gateway_error_account_risk(session, account, exc, now)
        return ReactionSendResult(
            status="failed",
            sent_at=now,
            send_raw={"mode": "telethon", "error": {"code": exc.code, **exc.raw}},
            error_code=exc.code,
            error_message=_friendly_gateway_error(exc, source=source, account=account, action="reaction"),
        )


async def _process_mass_reactions(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    max_reactions: int | None,
) -> ReactionTickSummary:
    config = _json_dict(campaign.config)
    policy = _json_dict(campaign.policy)
    account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]
    source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
    reactions = [str(item).strip() for item in config.get("reactions", []) if str(item).strip()]
    if not reactions:
        raise ValueError("Для AI реакций нужна минимум одна реакция")

    accounts_result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.id.in_(account_ids))
    )
    accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
    accounts = [accounts_by_id[account_id] for account_id in account_ids if account_id in accounts_by_id]
    if not accounts:
        raise ValueError("Для AI реакций нужен минимум один Telegram аккаунт")

    sources_result = await session.execute(
        select(TelegramSource).where(TelegramSource.workspace_id == workspace_id, TelegramSource.id.in_(source_ids))
    )
    sources_by_id = {source.id: source for source in sources_result.scalars().all()}
    sources = [sources_by_id[source_id] for source_id in source_ids if source_id in sources_by_id]
    if not sources:
        raise ValueError("Для AI реакций нужен минимум один канал или группа")

    selection_mode = _selection_mode_from_config(config)
    latest_limit = max(1, _effective_latest_limit(config, selection_mode))
    target_scope = _reaction_target_scope(config.get("target_scope"))
    comments_per_post = max(1, min(_safe_int(config.get("comments_per_post"), default=3), 200))
    messages_per_source = max(1, min(_safe_int(config.get("messages_per_source"), default=10), 500))
    probability = max(1, min(_safe_int(config.get("reaction_probability"), default=100), 100))
    daily_limit = _reaction_daily_limit(policy)
    delay_min = max(600, _safe_int(policy.get("delay_seconds_min"), default=600))
    delay_max = max(delay_min, _safe_int(policy.get("delay_seconds_max"), default=delay_min))
    delay_min, delay_max, behavior_snapshot = apply_behavior_delay_range(
        policy,
        accounts,
        delay_min,
        delay_max,
        floor_min=600,
    )
    reaction_rotation = str(policy.get("reaction_rotation") or "round_robin")
    reaction_weights = _reaction_weights(config.get("reaction_weights"))
    react_as = str(policy.get("react_as") or config.get("react_as") or "account")
    max_to_send = max_reactions if max_reactions is not None else _max_reactions(config)
    if max_to_send is not None:
        max_to_send = max(1, max_to_send)

    summary = await _execute_due_reaction_actions(
        session=session,
        campaign=campaign,
        accounts_by_id=accounts_by_id,
        sources_by_id=sources_by_id,
        now=datetime.now(UTC),
        max_reactions=max_to_send,
    )
    await session.flush()
    if max_to_send is not None:
        max_to_send = max(0, max_to_send - summary.successful_actions)
    account_cycle = cycle(accounts)
    daily_sent_by_account = await _daily_reactions_by_account(session, [account.id for account in accounts])
    reaction_index = int(_json_dict(campaign.config).get("reaction_rotation_index", 0) or 0)
    updated_cursors = dict(_json_dict(config.get("source_cursors")))
    sent_or_planned = 0
    next_planned_at = datetime.now(UTC)

    for source in sources:
        if max_to_send is not None and sent_or_planned >= max_to_send:
            summary.stop_reason = "max_comments"
            break
        summary.sources_checked += 1
        source_ref = _reaction_source_ref(source)
        source_account = _assigned_account_for_source(source.id, accounts, config)
        fetch_account = source_account or accounts[0]
        observed_targets = await _reaction_targets_for_source(
            session=session,
            workspace_id=workspace_id,
            source=source,
            source_ref=source_ref,
            account=fetch_account,
            target_scope=target_scope,
            latest_limit=latest_limit,
            comments_per_post=comments_per_post,
            messages_per_source=messages_per_source,
            refresh_latest=selection_mode == "new",
        )
        summary.messages_seen += len(observed_targets)
        if target_scope != "posts" and not observed_targets:
            summary.reactions_skipped_scope_unavailable += 1
        cursor = _json_dict(updated_cursors.get(source.id))
        if selection_mode == "new" and not cursor:
            latest_observed = max(observed_targets, key=_reaction_target_sort_key) if observed_targets else None
            if latest_observed:
                updated_cursors[source.id] = _cursor_from_reaction_target(latest_observed)
                summary.reaction_cursors_initialized += 1
            if _configured_latest_limit(config) <= 0:
                continue
            candidate_targets = observed_targets
        else:
            candidate_targets = _select_candidate_reaction_targets(
                selection_mode,
                source.id,
                observed_targets,
                updated_cursors,
            )
        if selection_mode == "new" and observed_targets:
            updated_cursors[source.id] = _cursor_from_reaction_target(
                max(observed_targets, key=_reaction_target_sort_key)
            )
        if selection_mode == "latest":
            candidate_targets = candidate_targets[:1]
        assignment_mode = _source_assignment_mode(config)
        parallel_accounts = accounts if assignment_mode == "parallel_pool" else None
        existing_target_ids = await _existing_reaction_target_ids(session, campaign.id) if parallel_accounts else set()
        before_dedupe = len(candidate_targets)
        if parallel_accounts:
            candidate_targets = [
                target
                for target in candidate_targets
                if not all(
                    _reaction_target_id(source.id, target.target_kind, target.target_tg_message_id, account.id)
                    in existing_target_ids
                    for account in parallel_accounts
                )
            ]
        else:
            candidate_targets = await _without_existing_reaction_target_actions(session, campaign.id, candidate_targets)
        summary.reactions_skipped_duplicate += max(0, before_dedupe - len(candidate_targets))

        for target in candidate_targets:
            if max_to_send is not None and sent_or_planned >= max_to_send:
                summary.stop_reason = "max_comments"
                break
            target_accounts: list[TelegramAccount]
            if parallel_accounts:
                target_accounts = parallel_accounts
            else:
                account = source_account or next(account_cycle)
                rotations = 0
                while (
                    not source_account
                    and daily_limit is not None
                    and daily_sent_by_account.get(account.id, 0) >= daily_limit
                    and rotations < len(accounts)
                ):
                    account = next(account_cycle)
                    rotations += 1
                target_accounts = [account]

            stop_target_loop = False
            for account in target_accounts:
                if max_to_send is not None and sent_or_planned >= max_to_send:
                    summary.stop_reason = "max_comments"
                    stop_target_loop = True
                    break
                target_id = _reaction_target_id(source.id, target.target_kind, target.target_tg_message_id, account.id)
                if target_id in existing_target_ids:
                    summary.reactions_skipped_duplicate += 1
                    continue
                if random.randint(1, 100) > probability:
                    summary.reactions_skipped_probability += 1
                    continue
                if daily_limit is not None and daily_sent_by_account.get(account.id, 0) >= daily_limit:
                    summary.reactions_skipped_daily_limit += 1
                    summary.stop_reason = "daily_limit"
                    if not parallel_accounts:
                        stop_target_loop = True
                        break
                    continue

                reaction, reaction_index, skipped_reason = _select_reaction_for_target(
                    reactions=reactions,
                    reaction_rotation=reaction_rotation,
                    reaction_index=reaction_index,
                    target=target,
                    reaction_weights=reaction_weights,
                )
                if not reaction:
                    if skipped_reason == "ai_context":
                        summary.reactions_skipped_ai_context += 1
                    continue

                now = datetime.now(UTC)
                if delay_min > 0:
                    delay_seconds = random.randint(delay_min, delay_max)
                    planned_at = max(next_planned_at, now) + timedelta(seconds=delay_seconds)
                    next_planned_at = planned_at
                else:
                    delay_seconds = 0
                    planned_at = now
                reaction_peer_ref = _reaction_peer_ref(target)
                if planned_at > now:
                    status = "queued"
                    executed_at = None
                    action_error_code = None
                    action_error_message = None
                    send_raw: dict[str, Any] = {
                        "mode": "queued",
                        "delay_seconds": int((planned_at - now).total_seconds()),
                        "spacing_delay_seconds": delay_seconds,
                        "reaction_peer_ref": reaction_peer_ref,
                        "behavior_protection": behavior_snapshot,
                    }
                else:
                    send_result = await _send_reaction_target(
                        session=session,
                        account=account,
                        source=source,
                        peer_ref=reaction_peer_ref,
                        tg_message_id=target.target_tg_message_id,
                        reaction=reaction,
                        react_as=react_as,
                    )
                    status = send_result.status
                    executed_at = send_result.sent_at
                    action_error_code = send_result.error_code
                    action_error_message = send_result.error_message
                    send_raw = send_result.send_raw
                session.add(
                    CampaignAction(
                        campaign_id=campaign.id,
                        telegram_account_id=account.id,
                        target_type=target.target_kind,
                        target_id=target_id,
                        action_type="set_reaction",
                        status=status,
                        planned_at=planned_at,
                        executed_at=executed_at,
                        result={
                            "source_id": source.id,
                            "source_title": source.title,
                            "source_ref": target.source_ref,
                            "reaction_peer_ref": reaction_peer_ref,
                            "message_id": target.local_message_id,
                            "tg_message_id": target.target_tg_message_id,
                            "target_kind": target.target_kind,
                            "reaction": reaction,
                            "react_as": react_as,
                            "settings_snapshot": {
                                "target_scope": target_scope,
                                "reaction_rotation": reaction_rotation,
                                "reactions": reactions,
                                "delay_seconds_min": delay_min,
                                "delay_seconds_max": delay_max,
                                "spacing_delay_seconds": delay_seconds,
                                "comments_per_post": comments_per_post,
                                "messages_per_source": messages_per_source,
                                "post_selection_mode": selection_mode,
                                "assignment_mode": assignment_mode,
                            },
                            "behavior_protection": behavior_snapshot,
                            "assignment_mode": assignment_mode,
                            "post_text": target.text,
                            "post_published_at": _iso(target.published_at),
                            "target_raw": target.raw,
                            "send": send_raw,
                        },
                        error_code=action_error_code,
                        error_message=action_error_message,
                    )
                )
                existing_target_ids.add(target_id)
                summary.messages_selected += 1
                summary.reaction_targets += 1
                summary.actions_created += 1
                sent_or_planned += 1
                if status == "failed":
                    summary.reactions_failed += 1
                elif status == "queued":
                    summary.reactions_planned += 1
                    daily_sent_by_account[account.id] = daily_sent_by_account.get(account.id, 0) + 1
                else:
                    summary.reactions_planned += 1
                    summary.reactions_sent += 1
                    summary.successful_actions += 1
                    daily_sent_by_account[account.id] = daily_sent_by_account.get(account.id, 0) + 1
                await session.flush()
            if stop_target_loop:
                break

    config_updates: dict[str, Any] = {}
    if reaction_rotation == "round_robin":
        config_updates["reaction_rotation_index"] = reaction_index
    if selection_mode == "new":
        config_updates["source_cursors"] = updated_cursors
    if config_updates:
        campaign.config = {**_json_dict(campaign.config), **config_updates}
    return summary


async def _has_pending_reaction_actions(session: AsyncSession, campaign_id: str) -> bool:
    result = await session.execute(
        select(CampaignAction.id)
        .where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status == "queued",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _has_due_reaction_actions(session: AsyncSession, campaign_id: str, now: datetime) -> bool:
    result = await session.execute(
        select(CampaignAction.id)
        .where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status == "queued",
            CampaignAction.planned_at <= now,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def process_live_neuro_message(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun,
    account_id: str,
    peer_ref: str,
    peer_title: str | None,
    message: TelegramChatMessageInfo,
) -> CampaignRun:
    now = datetime.now(UTC)
    if campaign.workspace_id != workspace_id or run.workspace_id != workspace_id or run.campaign_id != campaign.id:
        return run
    if campaign.type not in {"neuro_chat", "neuro_dialog"}:
        return run

    stop_conditions = _json_dict(run.stop_conditions or campaign.config.get("stop_conditions"))
    progress_before = _json_dict(run.progress)
    stop_reason = _terminal_stop_reason(run, progress_before, stop_conditions, now)
    if stop_reason and not _continuous_live_neuro_campaign(campaign, stop_reason):
        return await _complete_run(session, campaign, run, stop_reason, now)

    remaining = _remaining_max_comments(stop_conditions, progress_before)
    if remaining == 0 and not _continuous_live_neuro_campaign(campaign, "max_comments"):
        return await _complete_run(session, campaign, run, "max_comments", now)

    config = _json_dict(campaign.config)
    policy = _json_dict(campaign.policy)
    if account_id not in [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]:
        return run
    if bool(config.get("only_reply_to_incoming", campaign.type == "neuro_dialog")) and message.outgoing:
        _add_run_event(
            session,
            run,
            "info",
            "live_neuro_message_skipped",
            "Сообщение пропущено: это исходящее сообщение выбранного аккаунта.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "reason": "outgoing_message",
                "campaign_type": campaign.type,
            },
        )
        run.heartbeat_at = now
        await session.commit()
        return run

    text = (message.text or "").strip()
    score, reasons = _message_quality_score(text, bool(message.outgoing), campaign.type)
    trigger_keywords = _trigger_keywords_from_config(config)
    trigger_match = _neuro_chat_trigger_match(text, trigger_keywords) if campaign.type == "neuro_chat" else None
    if campaign.type == "neuro_chat" and trigger_keywords and not trigger_match:
        _add_run_event(
            session,
            run,
            "info",
            "live_neuro_message_skipped",
            "Сообщение пропущено: не найдено ключевое слово кампании.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "reason": "keyword_mismatch",
                "campaign_type": campaign.type,
                "keywords_count": len(trigger_keywords),
            },
        )
        run.heartbeat_at = now
        await session.commit()
        return run
    min_quality_score = int(config.get("min_quality_score") or 50)
    if campaign.type == "neuro_chat" and trigger_keywords:
        min_quality_score = 0
    if score < min_quality_score:
        _add_run_event(
            session,
            run,
            "info",
            "live_neuro_message_skipped",
            "Сообщение пропущено: не прошло фильтр качества.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "score": score,
                "min_quality_score": min_quality_score,
                "reasons": reasons,
                "campaign_type": campaign.type,
            },
        )
        run.heartbeat_at = now
        await session.commit()
        return run

    target_id = f"{peer_ref}:{message.tg_message_id}"
    if await _campaign_action_exists(session, campaign.id, target_id):
        _add_run_event(
            session,
            run,
            "info",
            "live_neuro_message_skipped",
            "Сообщение пропущено: ответ на него уже был создан.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "reason": "duplicate",
                "campaign_type": campaign.type,
            },
        )
        run.heartbeat_at = now
        await session.commit()
        return run

    account = await session.get(TelegramAccount, account_id)
    if not account or account.workspace_id != workspace_id:
        _add_run_event(
            session,
            run,
            "error",
            "live_neuro_message_skipped",
            "Сообщение пропущено: аккаунт кампании не найден.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "account_id": account_id,
                "campaign_type": campaign.type,
            },
        )
        run.heartbeat_at = now
        await session.commit()
        return run

    delivery_mode = _delivery_mode_from_config(config)
    delay_min = max(0, _safe_int(policy.get("delay_seconds_min"), default=0))
    delay_max = max(delay_min, _safe_int(policy.get("delay_seconds_max"), default=delay_min))
    delay_min, delay_max, behavior_snapshot = apply_behavior_delay_range(
        policy,
        [account],
        delay_min,
        delay_max,
    )
    status = await _create_neuro_reply_action(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        account=account,
        peer_ref=peer_ref,
        title=peer_title,
        message=message,
        score=score,
        reasons=reasons,
        prompt_text=str(config.get("prompt_text") or "Ответь уместно и коротко."),
        context_text=str(config.get("context_text") or "").strip() or None,
        tone=str(config.get("tone") or "живой"),
        max_length=int(policy.get("max_length", 280) or 280),
        delivery_mode=delivery_mode,
        reply_to_only=bool(config.get("reply_to_only", campaign.type == "neuro_chat")),
        delay_seconds_min=delay_min,
        delay_seconds_max=delay_max,
        behavior_snapshot=behavior_snapshot,
        match_metadata=trigger_match,
    )
    if status == "skipped":
        _add_run_event(
            session,
            run,
            "info",
            "live_neuro_message_skipped",
            "AI решил не отвечать на это сообщение.",
            {
                "peer_ref": peer_ref,
                "incoming_message_id": message.tg_message_id,
                "score": score,
                "reasons": reasons,
                "campaign_type": campaign.type,
            },
        )
        run.heartbeat_at = datetime.now(UTC)
        await session.commit()
        return run

    summary = ChatTickSummary(
        sources_checked=1,
        messages_seen=1,
        messages_scored=1,
        messages_selected=1,
        replies_generated=0 if status == "failed" else 1,
        replies_sent=1 if status == "succeeded" and delivery_mode == "generate_and_send" else 0,
        replies_drafted=1 if status == "generated" else 0,
        replies_failed=1 if status == "failed" else 0,
        successful_actions=1 if status in {"generated", "succeeded"} else 0,
        actions_created=1,
    )
    finished_at = datetime.now(UTC)
    progress = _merge_progress(progress_before, summary.as_progress(), finished_at)
    run.status = "running"
    run.started_at = run.started_at or now
    run.heartbeat_at = finished_at
    run.progress = progress
    campaign.status = "running"
    run.report = _build_report(
        campaign,
        "running",
        progress,
        stop_conditions,
        run.started_at or now,
        run.finished_at,
        finished_at,
    )
    _add_run_event(
        session,
        run,
        "error" if status == "failed" else "info",
        "live_neuro_reply_failed" if status == "failed" else "live_neuro_reply_created",
        "Новое сообщение обработано live-handler.",
        {
            "peer_ref": peer_ref,
            "incoming_message_id": message.tg_message_id,
            "campaign_type": campaign.type,
            "delivery_mode": delivery_mode,
        },
    )

    stop_reason = _terminal_stop_reason(run, progress, stop_conditions, finished_at, summary)
    if stop_reason and not _continuous_live_neuro_campaign(campaign, stop_reason):
        return await _complete_run(session, campaign, run, stop_reason, finished_at)

    await session.commit()
    await session.refresh(run)
    return run


async def _create_neuro_reply_action(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    account: TelegramAccount,
    peer_ref: str,
    title: str | None,
    message: TelegramChatMessageInfo,
    score: int,
    reasons: list[str],
    prompt_text: str,
    context_text: str | None,
    tone: str,
    max_length: int,
    delivery_mode: str,
    reply_to_only: bool,
    delay_seconds_min: int = 0,
    delay_seconds_max: int = 0,
    behavior_snapshot: dict[str, Any] | None = None,
    match_metadata: dict[str, Any] | None = None,
) -> str:
    text = (message.text or "").strip()
    now = datetime.now(UTC)
    delay_seconds = (
        random.randint(delay_seconds_min, max(delay_seconds_min, delay_seconds_max)) if delay_seconds_min > 0 else 0
    )
    status = "generated" if delivery_mode == "draft_only" else "queued" if delay_seconds > 0 else "succeeded"
    action_error_code = None
    action_error_message = None
    send_raw = {"mode": "draft"}
    sent_message_id = None
    reply_to_tg_message_id = message.tg_message_id if reply_to_only else None
    planned_at = now + timedelta(seconds=delay_seconds) if status == "queued" else now
    author_gender = _account_author_gender(account, _json_dict(campaign.config).get("author_gender"))
    identity_context = _account_identity_context(account, author_gender)
    pending_generation = {
        "prompt_text": prompt_text,
        "context_text": context_text,
        "tone": tone,
        "max_length": max_length,
        "allow_skip": campaign.type != "neuro_dialog",
        "author_gender": author_gender,
        "identity_context": identity_context,
    }
    if delivery_mode == "generate_and_send" and status == "queued":
        send_raw = {
            "mode": "queued",
            "delay_seconds": delay_seconds,
            "behavior_protection": behavior_snapshot,
        }
        result = _neuro_reply_action_result(
            account=account,
            peer_ref=peer_ref,
            title=title,
            message=message,
            text=text,
            reply_text="",
            reply_to_tg_message_id=reply_to_tg_message_id,
            score=score,
            reasons=reasons,
            delivery_mode=delivery_mode,
            sent_message_id=None,
            send_raw=send_raw,
            usage={"mode": "queued_before_generation"},
            pending_generation=pending_generation,
            behavior_snapshot=behavior_snapshot,
            match_metadata=match_metadata,
        )
        existing_action = (
            await _queued_neuro_reply_for_peer(session, campaign.id, peer_ref)
            if campaign.type == "neuro_dialog"
            else None
        )
        if existing_action:
            existing_action.telegram_account_id = account.id
            existing_action.target_id = f"{peer_ref}:{message.tg_message_id}"
            existing_action.planned_at = planned_at
            existing_action.executed_at = None
            existing_action.result = result
            existing_action.error_code = None
            existing_action.error_message = None
            return "queued"
        session.add(
            CampaignAction(
                campaign_id=campaign.id,
                telegram_account_id=account.id,
                target_type="dialog_message" if campaign.type == "neuro_dialog" else "group_message",
                target_id=f"{peer_ref}:{message.tg_message_id}",
                action_type="neuro_dialog_reply" if campaign.type == "neuro_dialog" else "neuro_chat_reply",
                status=status,
                planned_at=planned_at,
                executed_at=None,
                result=result,
                error_code=None,
                error_message=None,
            )
        )
        return status
    reply_text, usage = await _generate_neuro_reply_text(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        prompt_text=prompt_text,
        incoming_text=text,
        chat_title=title,
        context_text=context_text,
        tone=tone,
        max_length=max_length,
        allow_skip=campaign.type != "neuro_dialog",
        author_gender=author_gender,
        identity_context=identity_context,
    )
    if reply_text.strip().upper() == "SKIP":
        return "skipped"
    if delivery_mode == "generate_and_send" and status == "queued":
        send_raw = {
            "mode": "queued",
            "delay_seconds": delay_seconds,
            "behavior_protection": behavior_snapshot,
        }
    elif delivery_mode == "generate_and_send":
        delivery = await _deliver_neuro_reply_action(
            session=session,
            account=account,
            peer_ref=peer_ref,
            reply_text=reply_text,
            reply_to_tg_message_id=reply_to_tg_message_id,
            business_intent=f"neuro:{campaign.id}:{peer_ref}:{message.tg_message_id}",
        )
        status = delivery.status
        action_error_code = delivery.error_code
        action_error_message = delivery.error_message
        sent_message_id = delivery.sent_message_id
        send_raw = delivery.send_raw
    result = _neuro_reply_action_result(
        account=account,
        peer_ref=peer_ref,
        title=title,
        message=message,
        text=text,
        reply_text=reply_text,
        reply_to_tg_message_id=reply_to_tg_message_id,
        score=score,
        reasons=reasons,
        delivery_mode=delivery_mode,
        sent_message_id=sent_message_id,
        send_raw=send_raw,
        usage=usage,
        pending_generation=None,
        behavior_snapshot=behavior_snapshot,
        match_metadata=match_metadata,
    )
    session.add(
        CampaignAction(
            campaign_id=campaign.id,
            telegram_account_id=account.id,
            target_type="dialog_message" if campaign.type == "neuro_dialog" else "group_message",
            target_id=f"{peer_ref}:{message.tg_message_id}",
            action_type="neuro_dialog_reply" if campaign.type == "neuro_dialog" else "neuro_chat_reply",
            status=status,
            planned_at=planned_at,
            executed_at=now if status in {"generated", "succeeded"} else None,
            result=result,
            error_code=action_error_code,
            error_message=action_error_message,
        )
    )
    return status


async def _generate_neuro_reply_for_action(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    account: TelegramAccount,
    action_result: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    pending = _json_dict(action_result.get("pending_generation"))
    return await _generate_neuro_reply_text(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        prompt_text=str(pending.get("prompt_text") or "Ответь уместно и коротко."),
        incoming_text=str(action_result.get("incoming_text") or ""),
        chat_title=_optional_str(action_result.get("chat_title")),
        context_text=_optional_str(pending.get("context_text")),
        tone=str(pending.get("tone") or "живой"),
        max_length=_safe_int(pending.get("max_length"), default=280),
        allow_skip=bool(pending.get("allow_skip", campaign.type != "neuro_dialog")),
        author_gender=_account_author_gender(account, pending.get("author_gender")),
        identity_context=str(
            pending.get("identity_context")
            or _account_identity_context(account, _account_author_gender(account, pending.get("author_gender")))
        ),
    )


async def _generate_neuro_reply_text(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    prompt_text: str,
    incoming_text: str,
    chat_title: str | None,
    context_text: str | None,
    tone: str,
    max_length: int,
    allow_skip: bool,
    author_gender: str,
    identity_context: str,
) -> tuple[str, dict[str, Any]]:
    final_context = "\n".join(part for part in [identity_context, context_text] if part)
    final_prompt = (
        f"{prompt_text}\n"
        "Всегда отвечай строго от лица указанного Telegram-аккаунта. "
        "Не придумывай другое имя, пол, роль, знакомство или прошлый контекст. "
        "Если собеседник спрашивает кто ты, называй только имя или username из профиля аккаунта. "
        "Не упоминай tg.demo-platform, demo-platform TG, Tg.ai и другие старые брендовые названия."
    )
    if campaign.type == "neuro_chat":
        final_prompt = (
            f"{final_prompt}\n"
            "Это AI-чат в группе по ключевым словам, а не личный диалог. "
            "Не задавай вопросов и не пытайся развивать переписку: следующий ответ пользователя может не содержать ключевое слово. "
            "Сделай один короткий самостоятельный ответ в стиле мягкой непрямой рекламы. "
            "Можно аккуратно упомянуть demo-platform как платформу для лидогенерации, автоматизации Telegram, AI-комментариев, прогрева, источников, логов и отчетов. "
            "Не обещай гарантированный результат, не дави на продажу и не проси написать в личку. "
            "В ответе не должно быть вопросительного знака."
        )
    reply_text, usage = await generate_chat_reply_text(
        session,
        workspace_id=workspace_id,
        prompt_text=final_prompt,
        incoming_text=incoming_text,
        chat_title=chat_title,
        context_text=final_context or None,
        tone=tone,
        max_length=max_length,
        allow_skip=allow_skip,
        author_gender=normalize_author_gender(author_gender),
        is_dialogue=campaign.type == "neuro_dialog",
    )
    if reply_text.strip().upper() == "SKIP" and campaign.type == "neuro_dialog":
        return "Понял тебя. Напиши чуть подробнее, что именно имеешь в виду?", {**usage, "skip_fallback": True}
    if campaign.type == "neuro_chat" and reply_text.strip().upper() != "SKIP":
        cleaned_reply = _sanitize_neuro_chat_reply(reply_text, max_length=max_length)
        if cleaned_reply != reply_text:
            return cleaned_reply, {**usage, "neuro_chat_question_sanitized": True}
    return reply_text, usage


def _sanitize_neuro_chat_reply(text: str, *, max_length: int) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return cleaned
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    non_questions = [part.strip() for part in parts if part.strip() and "?" not in part]
    cleaned = " ".join(non_questions).strip() or re.sub(r"\?+", ".", cleaned).strip()
    cleaned = cleaned.replace("?", ".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "Для таких задач удобно смотреть demo-platform: там лиды, источники, AI-ответы и логи собраны в одном рабочем контуре"
    if len(cleaned) > max_length:
        trimmed = cleaned[: max(0, max_length)].rsplit(" ", 1)[0].strip(" .,;:")
        cleaned = trimmed or cleaned[:max_length].strip(" .,;:")
    return cleaned


def _neuro_reply_action_result(
    *,
    account: TelegramAccount,
    peer_ref: str,
    title: str | None,
    message: TelegramChatMessageInfo,
    text: str,
    reply_text: str,
    reply_to_tg_message_id: str | None,
    score: int,
    reasons: list[str],
    delivery_mode: str,
    sent_message_id: str | None,
    send_raw: dict[str, Any],
    usage: dict[str, Any],
    pending_generation: dict[str, Any] | None,
    behavior_snapshot: dict[str, Any] | None = None,
    match_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "peer_ref": peer_ref,
        "chat_title": title,
        "incoming_message_id": message.tg_message_id,
        "reply_to_tg_message_id": reply_to_tg_message_id,
        "incoming_text": text,
        "reply_text": reply_text,
        "account_id": account.id,
        "account_identity": _account_identity_payload(account),
        "quality_score": score,
        "quality_reasons": reasons,
        "delivery_mode": delivery_mode,
        "sent_message_id": sent_message_id,
        "send": send_raw,
        "usage": usage,
        "behavior_protection": behavior_snapshot,
        "mode": "telethon_live" if message.raw.get("mode") == "telethon_live" else "bounded_scan",
    }
    if pending_generation:
        result["pending_generation"] = pending_generation
    if match_metadata:
        result.update(match_metadata)
    return result


def _message_quality_score(text: str, outgoing: bool, campaign_type: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    cleaned = " ".join(text.split())
    if outgoing or not cleaned:
        return 0, ["empty_or_outgoing"]
    lowered = cleaned.lower()
    if campaign_type == "neuro_dialog":
        if lowered.startswith(("/", "!")):
            return 5, ["command"]
        if sum(token in lowered for token in ["http://", "https://", "t.me/", "подпиш", "скидк", "акция"]) >= 2:
            return 20, ["promotional"]
        score = 55
        reasons.append("dialog")
        if len(cleaned) < 18:
            score += 5
            reasons.append("short_dialog_message")
        elif len(cleaned) <= 1200:
            score += 10
            reasons.append("normal_dialog_message")
        else:
            reasons.append("long_dialog_message")
        if "?" in cleaned:
            score += 15
            reasons.append("question")
        if any(word in lowered for word in ["как", "почему", "зачем", "что", "кто", "где", "когда", "можно", "нужно"]):
            score += 10
            reasons.append("dialog_hook")
        return min(score, 100), reasons
    if len(cleaned) < 18:
        return 10, ["too_short"]
    if len(cleaned) > 900:
        return 20, ["too_long"]
    if lowered.startswith(("/", "!")):
        return 5, ["command"]
    if sum(token in lowered for token in ["http://", "https://", "t.me/", "подпиш", "скидк", "акция"]) >= 2:
        return 20, ["promotional"]
    score = 35
    if "?" in cleaned:
        score += 20
        reasons.append("question")
    if any(word in lowered for word in ["как", "почему", "зачем", "что думаете", "кто", "где", "когда"]):
        score += 15
        reasons.append("discussion_hook")
    if any(word in lowered for word in ["лично", "опыт", "пробовал", "думаю", "кажется", "вопрос", "интересно"]):
        score += 15
        reasons.append("human_context")
    if 60 <= len(cleaned) <= 420:
        score += 10
        reasons.append("good_length")
    if campaign_type == "neuro_dialog":
        score += 10
        reasons.append("dialog")
    return min(score, 100), reasons or ["basic"]


def _trigger_keywords_from_config(config: dict[str, Any]) -> list[str]:
    raw_keywords = config.get("trigger_keywords")
    if not isinstance(raw_keywords, list):
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for item in raw_keywords:
        keyword = " ".join(str(item or "").split()).strip(" ,;")
        if not keyword:
            continue
        lowered = keyword.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(keyword)
    return keywords


def _neuro_chat_trigger_match(text: str, keywords: list[str]) -> dict[str, Any] | None:
    normalized_text = _normalize_trigger_text(text)
    if not normalized_text:
        return None
    for keyword in keywords:
        normalized_keyword = _normalize_trigger_text(keyword)
        if normalized_keyword and normalized_keyword in normalized_text:
            return {
                "matched_keyword": keyword,
                "match_type": "keyword",
                "match_score": 100,
            }
    return None


def _normalize_trigger_text(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


async def _campaign_action_exists(session: AsyncSession, campaign_id: str, target_id: str) -> bool:
    result = await session.execute(
        select(CampaignAction.id)
        .where(CampaignAction.campaign_id == campaign_id, CampaignAction.target_id == target_id)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _completed_neuro_reply_exists(
    session: AsyncSession,
    campaign_id: str,
    target_id: str,
    *,
    exclude_action_id: str | None = None,
) -> bool:
    statement = (
        select(CampaignAction.id)
        .where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.target_id == target_id,
            CampaignAction.action_type.in_(["neuro_chat_reply", "neuro_dialog_reply"]),
            CampaignAction.status.in_(["succeeded", "sent", "generated", "skipped"]),
        )
        .limit(1)
    )
    if exclude_action_id:
        statement = statement.where(CampaignAction.id != exclude_action_id)
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


async def _queued_neuro_reply_for_peer(
    session: AsyncSession,
    campaign_id: str,
    peer_ref: str,
) -> CampaignAction | None:
    action_types = ["neuro_chat_reply", "neuro_dialog_reply"]
    result = await session.execute(
        select(CampaignAction)
        .where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type.in_(action_types),
            CampaignAction.status == "queued",
            CampaignAction.target_id.like(f"{peer_ref}:%"),
        )
        .order_by(CampaignAction.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _account_identity_payload(account: TelegramAccount) -> dict[str, str | None]:
    first_name = _clean_neuro_identity_text(account.first_name)
    last_name = _clean_neuro_identity_text(account.last_name)
    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
    fallback_display_name = _clean_neuro_identity_text(account.display_name)
    return {
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name or fallback_display_name,
        "username": f"@{account.username.lstrip('@')}" if account.username else None,
        "author_gender": _account_author_gender(account, None),
    }


def _clean_neuro_identity_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    replacements = {
        "tg.demo-platform": "demo-platform",
        "demo-platform TG": "demo-platform",
        "demo-platform tg": "demo-platform",
        "Tg.ai": "demo-platform",
        "tg.ai": "demo-platform",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned or None


def _account_identity_context(account: TelegramAccount, author_gender: str) -> str:
    identity = _account_identity_payload(account)
    name = identity.get("display_name") or identity.get("first_name") or identity.get("username") or "этот аккаунт"
    username = identity.get("username")
    gender_label = {
        "feminine": "женский",
        "masculine": "мужской",
        "neutral": "нейтральный",
    }.get(author_gender, "нейтральный")
    parts = [
        f"Профиль Telegram-аккаунта, от лица которого нужно отвечать: {name}.",
        f"Username аккаунта: {username}." if username else None,
        f"Авторский род аккаунта: {gender_label}.",
        "Нельзя называть себя другим именем, менять пол аккаунта или придумывать знакомство с собеседником.",
        f"Если собеседник спрашивает кто ты, отвечай от имени {name}.",
    ]
    return "\n".join(part for part in parts if part)


def _account_author_gender(account: TelegramAccount, fallback: object) -> str:
    profile = account._profile_metadata()
    raw_gender = profile.get("gender") or profile.get("profile_gender") or profile.get("sex") or fallback
    if isinstance(raw_gender, str):
        normalized = raw_gender.strip().lower()
        if normalized in {"feminine", "female", "woman", "girl", "ж", "жен", "женский"}:
            return "feminine"
        if normalized in {"masculine", "male", "man", "boy", "м", "муж", "мужской"}:
            return "masculine"
        if normalized in {"neutral", "unknown", "other", "none", "нейтральный"}:
            return "neutral"
    first_name = (account.first_name or "").strip().lower()
    if first_name:
        feminine_names = {
            "алина",
            "анна",
            "анастасия",
            "настя",
            "мария",
            "маша",
            "екатерина",
            "катя",
            "елена",
            "ольга",
            "юлия",
            "дарья",
            "ирина",
            "наталья",
            "ксения",
            "софия",
        }
        masculine_names = {
            "александр",
            "алексей",
            "иван",
            "дмитрий",
            "сергей",
            "андрей",
            "максим",
            "михаил",
            "никита",
            "илья",
            "артем",
            "артём",
            "егор",
            "павел",
            "роман",
        }
        if first_name in feminine_names:
            return "feminine"
        if first_name in masculine_names:
            return "masculine"
        if first_name.endswith(("а", "я")) and first_name not in {"илья", "никита", "саша", "женя"}:
            return "feminine"
    return normalize_author_gender(fallback)


async def tick_active_comment_posts_campaign_runs(
    session: AsyncSession,
    workspace_id: str | None = None,
    *,
    limit: int = 20,
) -> list[CampaignRun]:
    statement = (
        select(CampaignRun)
        .where(CampaignRun.status.in_(RUN_TICKABLE_STATUSES))
        .order_by(CampaignRun.heartbeat_at.asc().nulls_first(), CampaignRun.created_at.asc())
        .limit(limit)
    )
    if workspace_id:
        statement = statement.where(CampaignRun.workspace_id == workspace_id)

    statement = statement.with_for_update(skip_locked=True)

    result = await session.execute(statement)
    runs = list(result.scalars().all())
    ticked: list[CampaignRun] = []
    for run in runs:
        ticked.append(await tick_comment_posts_campaign_run(session, run.workspace_id, run))
    return ticked


async def request_stop_campaign_run(
    session: AsyncSession,
    workspace_id: str,
    run: CampaignRun,
    *,
    reason: str = "manual_stop",
    event_type: str = "manual_stop_requested",
    event_message: str = "Manual stop was requested; run was cancelled by the API.",
    campaign_status: str | None = "cancelled",
) -> CampaignRun:
    now = datetime.now(UTC)
    if run.workspace_id != workspace_id:
        return run

    if run.status not in RUN_TERMINAL_STATUSES:
        run.requested_stop_at = run.requested_stop_at or now
        run.status = "cancelled"
        run.finished_at = now
        run.heartbeat_at = now
        progress = {**_json_dict(run.progress), "stop_reason": reason}
        run.progress = progress
        run.report = {
            **_json_dict(run.report),
            "status": "cancelled",
            "finished_at": _iso(now),
            "stop_reason": reason,
            "progress": progress,
        }
        campaign = await session.get(Campaign, run.campaign_id)
        if campaign and campaign.workspace_id == workspace_id and campaign_status:
            campaign.status = campaign_status
        _add_run_event(
            session,
            run,
            "info",
            event_type,
            event_message,
            {"reason": reason},
        )
        if campaign and campaign.workspace_id == workspace_id:
            await record_operational_event(
                session,
                workspace_id=workspace_id,
                severity="info",
                category="campaigns",
                event_type="campaign_run_cancelled",
                title=f"Кампания остановлена: {campaign.name}",
                message=_friendly_stop_reason(reason),
                source="campaigns.engine",
                entity_type="campaign_run",
                entity_id=run.id,
                dedupe_key=f"campaign_run_cancelled:{run.id}:{reason}",
                payload={
                    "campaign_id": campaign.id,
                    "campaign_name": campaign.name,
                    "run_id": run.id,
                    "reason": reason,
                },
                alert=False,
            )

    await session.commit()
    await session.refresh(run)
    return run


async def build_campaign_comment_report(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    status_filter: str | None = None,
    limit: int = 1000,
) -> CampaignCommentReport:
    effective_limit = max(1, min(int(limit or 1000), 5000))
    statement = (
        select(TelegramComment, TelegramSource, TelegramMessage, TelegramAccount)
        .join(TelegramSource, TelegramSource.id == TelegramComment.source_id)
        .join(TelegramMessage, TelegramMessage.id == TelegramComment.post_message_id)
        .outerjoin(TelegramAccount, TelegramAccount.id == TelegramComment.telegram_account_id)
        .where(
            TelegramComment.workspace_id == workspace_id,
            TelegramComment.campaign_id == campaign.id,
            TelegramSource.workspace_id == workspace_id,
            TelegramMessage.workspace_id == workspace_id,
        )
        .order_by(
            func.coalesce(TelegramComment.sent_at, TelegramComment.created_at).desc(),
            TelegramComment.created_at.desc(),
        )
        .limit(effective_limit)
    )
    if status_filter:
        statement = statement.where(TelegramComment.status == status_filter)

    result = await session.execute(statement)
    rows: list[CampaignCommentReportRow] = []
    summaries_by_source: dict[str, CampaignChannelReportSummary] = {}
    sent_count = 0
    draft_count = 0
    failed_count = 0

    for comment, source, message, account in result.all():
        channel = _source_channel_label(source)
        effective_comment_at = comment.sent_at or comment.created_at
        row = CampaignCommentReportRow(
            comment_id=comment.id,
            campaign_id=campaign.id,
            source_id=source.id,
            channel=channel,
            source_title=source.title,
            source_username=source.username,
            source_url=source.url,
            post_message_id=message.id,
            post_tg_message_id=message.tg_message_id,
            post_published_at=message.published_at,
            post_url=_telegram_post_url(source, message),
            post_text=message.text,
            comment_text=comment.text,
            comment_status=comment.status,
            comment_sent_at=comment.sent_at,
            comment_created_at=comment.created_at,
            comment_error_code=comment.error_code,
            comment_error_message=comment.error_message,
            tg_comment_message_id=comment.tg_comment_message_id,
            account_id=comment.telegram_account_id,
            account_label=_account_label(account),
        )
        rows.append(row)

        if comment.status == "sent":
            sent_count += 1
        elif comment.status == "draft":
            draft_count += 1
        elif comment.status == "failed":
            failed_count += 1

        summary = summaries_by_source.get(source.id)
        if not summary:
            summary = CampaignChannelReportSummary(
                source_id=source.id,
                channel=channel,
                source_title=source.title,
                source_username=source.username,
                total_count=0,
                sent_count=0,
                draft_count=0,
                failed_count=0,
                last_comment_at=None,
            )
            summaries_by_source[source.id] = summary
        summary.total_count += 1
        if comment.status == "sent":
            summary.sent_count += 1
        elif comment.status == "draft":
            summary.draft_count += 1
        elif comment.status == "failed":
            summary.failed_count += 1
        if effective_comment_at and (summary.last_comment_at is None or effective_comment_at > summary.last_comment_at):
            summary.last_comment_at = effective_comment_at

    by_channel = sorted(
        summaries_by_source.values(),
        key=lambda item: _datetime_sort_value(item.last_comment_at),
        reverse=True,
    )
    return CampaignCommentReport(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        campaign_status=campaign.status,
        generated_at=datetime.now(UTC),
        total_count=len(rows),
        sent_count=sent_count,
        draft_count=draft_count,
        failed_count=failed_count,
        by_channel=by_channel,
        rows=rows,
    )


def campaign_comment_report_csv(report: CampaignCommentReport) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Канал",
            "Дата",
            "Время",
            "Статус",
            "Ссылка на пост",
            "ID поста",
            "Текст поста",
            "Комментарий",
            "Аккаунт",
        ]
    )
    for row in report.rows:
        comment_at = row.comment_sent_at or row.comment_created_at
        writer.writerow(
            [
                row.channel,
                _csv_date(comment_at),
                _csv_time(comment_at),
                row.comment_status,
                row.post_url or "",
                row.post_tg_message_id or row.post_message_id,
                row.post_text or "",
                row.comment_text,
                row.account_label or "",
            ]
        )
    return "\ufeff" + output.getvalue()


async def build_campaign_reaction_report(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    status_filter: str | None = None,
    limit: int = 1000,
) -> CampaignReactionReport:
    effective_limit = max(1, min(int(limit or 1000), 5000))
    statement = (
        select(CampaignAction, TelegramAccount)
        .outerjoin(TelegramAccount, TelegramAccount.id == CampaignAction.telegram_account_id)
        .where(
            CampaignAction.campaign_id == campaign.id,
            CampaignAction.action_type == "set_reaction",
        )
        .order_by(func.coalesce(CampaignAction.executed_at, CampaignAction.created_at).desc())
        .limit(effective_limit)
    )
    if status_filter:
        statement = statement.where(CampaignAction.status == status_filter)
    result = await session.execute(statement)
    rows: list[CampaignReactionReportRow] = []
    sent_count = 0
    failed_count = 0
    for action, account in result.all():
        action_result = _json_dict(action.result)
        row = CampaignReactionReportRow(
            action_id=action.id,
            campaign_id=campaign.id,
            source_id=_optional_str(action_result.get("source_id")),
            source_title=_optional_str(action_result.get("source_title")),
            source_ref=_optional_str(action_result.get("source_ref")),
            message_id=_optional_str(action_result.get("message_id")),
            tg_message_id=_optional_str(action_result.get("tg_message_id")),
            target_kind=_optional_str(action_result.get("target_kind")) or action.target_type,
            target_url=_telegram_message_url(
                _optional_str(action_result.get("source_ref")),
                _optional_str(action_result.get("tg_message_id")),
            ),
            post_published_at=_parse_datetime(action_result.get("post_published_at")),
            post_text=_optional_str(action_result.get("post_text")),
            reaction=_optional_str(action_result.get("reaction")),
            status=action.status,
            account_id=action.telegram_account_id,
            account_label=_account_label(account),
            created_at=action.created_at,
            executed_at=action.executed_at,
            error_code=action.error_code,
            error_message=action.error_message,
        )
        rows.append(row)
        if action.status in {"sent", "succeeded"}:
            sent_count += 1
        elif action.status == "failed":
            failed_count += 1
    return CampaignReactionReport(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        campaign_status=campaign.status,
        generated_at=datetime.now(UTC),
        total_count=len(rows),
        sent_count=sent_count,
        failed_count=failed_count,
        rows=rows,
    )


def campaign_reaction_report_csv(report: CampaignReactionReport) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "campaign_name",
            "source_title",
            "source_ref",
            "target_kind",
            "target_tg_message_id",
            "target_url",
            "reaction",
            "account_label",
            "status",
            "sent_at",
            "error_code",
            "error_message",
        ]
    )
    for row in report.rows:
        writer.writerow(
            [
                report.campaign_name,
                row.source_title or "",
                row.source_ref or row.source_id or "",
                row.target_kind or "telegram_message",
                row.tg_message_id or row.message_id or "",
                row.target_url or _telegram_message_url(row.source_ref, row.tg_message_id),
                row.reaction or "",
                row.account_label or "",
                row.status,
                _iso(row.executed_at),
                row.error_code or "",
                row.error_message or "",
            ]
        )
    return "\ufeff" + output.getvalue()


def _telegram_message_url(source_ref: str | None, tg_message_id: str | None) -> str:
    if not source_ref or not tg_message_id:
        return ""
    ref = str(source_ref).strip()
    if ref.startswith("https://t.me/"):
        return f"{ref.rstrip('/')}/{tg_message_id}"
    if ref.startswith("t.me/"):
        return f"https://{ref.rstrip('/')}/{tg_message_id}"
    if ref.startswith("@"):
        return f"https://t.me/{ref.lstrip('@')}/{tg_message_id}"
    return ""


def _source_channel_label(source: TelegramSource) -> str:
    if source.title:
        return source.title
    if source.username:
        return f"@{source.username.lstrip('@')}"
    if source.url:
        return source.url
    return source.id


def _account_label(account: TelegramAccount | None) -> str | None:
    if not account:
        return None
    if account.display_name:
        return account.display_name
    if account.username:
        return f"@{account.username.lstrip('@')}"
    phone = getattr(account, "phone", None)
    if phone:
        return phone
    return account.id


def _account_is_actionable(account: TelegramAccount | None) -> bool:
    if not account:
        return False
    if account.incubator_mode:
        return False
    return account.status in {"active", "ready", "warming"}


def _telegram_post_url(source: TelegramSource, message: TelegramMessage) -> str | None:
    if not message.tg_message_id:
        return None

    username = _clean_telegram_username(source.username)
    if username:
        return f"https://t.me/{username}/{message.tg_message_id}"

    base_url = _source_tme_base_url(source.url)
    if base_url:
        return f"{base_url}/{message.tg_message_id}"
    return None


def _clean_telegram_username(value: str | None) -> str:
    username = (value or "").strip().lstrip("@").strip("/")
    if not username or username in {"joinchat"} or username.startswith("+"):
        return ""
    return username.split("/")[0]


def _source_tme_base_url(value: str | None) -> str | None:
    raw_url = (value or "").strip()
    if not raw_url:
        return None
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"t.me", "telegram.me"}:
        return None
    path = parsed.path.strip("/")
    if not path or path.startswith("+") or path == "joinchat":
        return None
    return urlunparse(("https", "t.me", f"/{path}", "", "", "")).rstrip("/")


def _csv_date(value: datetime | None) -> str:
    return value.date().isoformat() if value else ""


def _csv_time(value: datetime | None) -> str:
    return value.time().replace(microsecond=0).isoformat() if value else ""


def _datetime_sort_value(value: datetime | None) -> float:
    if not value:
        return 0
    if value.tzinfo:
        return value.timestamp()
    return value.replace(tzinfo=UTC).timestamp()


async def _load_comment_context(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
) -> CommentRuntimeContext | None:
    config = campaign.config
    prompt = await session.get(CommentPromptTemplate, config["comment_prompt_template_id"])
    if not prompt or prompt.workspace_id != workspace_id:
        return None

    sources_result = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(config["source_ids"]),
        )
    )
    sources_by_id = {source.id: source for source in sources_result.scalars().all()}
    sources = [
        sources_by_id[source_id]
        for source_id in config["source_ids"]
        if source_id in sources_by_id and _source_available_for_campaign(sources_by_id[source_id])
    ]

    accounts_result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(config["account_ids"]),
        )
    )
    accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
    session_account_ids: set[str] | None = None
    if real_telegram_enabled() and accounts_by_id:
        session_result = await session.execute(
            select(TelegramSession.account_id).where(TelegramSession.account_id.in_(accounts_by_id.keys()))
        )
        session_account_ids = set(session_result.scalars().all())
    accounts = [
        accounts_by_id[account_id]
        for account_id in config["account_ids"]
        if account_id in accounts_by_id
        and _account_can_send_comments(accounts_by_id[account_id])
        and (session_account_ids is None or account_id in session_account_ids)
    ]
    return CommentRuntimeContext(prompt=prompt, sources=sources, accounts=accounts)


def _source_available_for_campaign(source: TelegramSource) -> bool:
    access_status = source.access_status or "available"
    if access_status == "blacklisted":
        return False
    if access_status == "archived":
        return False
    if access_status.startswith("invalid_"):
        return False
    if access_status == "blocked":
        return False
    metadata = _json_dict(source.source_metadata)
    return metadata.get("blacklisted") is not True and metadata.get("invalid") is not True


def _source_assignment_mode(config: dict[str, Any]) -> str:
    value = str(config.get("source_assignment_mode") or "standard")
    return value if value in {"standard", "parallel_pool", "multi_account_split", "manual_pinned"} else "standard"


def _assigned_account_for_source(
    source_id: str,
    accounts: list[TelegramAccount],
    config: dict[str, Any],
) -> TelegramAccount | None:
    if _source_assignment_mode(config) not in {"multi_account_split", "manual_pinned"}:
        return None
    assignment = _json_dict(config.get("source_assignment"))
    accounts_by_id = {account.id: account for account in accounts}
    for account_id, assigned_source_ids in assignment.items():
        if not isinstance(assigned_source_ids, list):
            continue
        if source_id in {str(item) for item in assigned_source_ids if item}:
            return accounts_by_id.get(str(account_id))
    return None


def _comment_sources_for_tick(
    sources: list[TelegramSource],
    config: dict[str, Any],
    policy: dict[str, Any],
    *,
    throttle_by_default: bool,
) -> tuple[list[TelegramSource], int]:
    if not sources:
        return [], 0
    raw_limit = (
        policy.get("max_sources_per_tick")
        or policy.get("sources_per_tick")
        or config.get("max_sources_per_tick")
        or config.get("sources_per_tick")
    )
    if raw_limit is None:
        limit = DEFAULT_COMMENT_SOURCES_PER_TICK if throttle_by_default else len(sources)
    else:
        limit = _safe_int(raw_limit, default=len(sources))
    limit = max(1, min(limit, len(sources)))
    if limit >= len(sources):
        return sources, 0

    start = _safe_int(config.get("source_rotation_index"), default=0) % len(sources)
    rotated = sources[start:] + sources[:start]
    return rotated[:limit], limit


async def _process_comment_sources(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun | None = None,
    context: CommentRuntimeContext,
    selection_mode: str,
    delivery_mode: str,
    latest_limit: int,
    max_comments: int | None,
    dedupe: bool,
    update_cursors: bool,
) -> CommentTickSummary:
    summary = CommentTickSummary()
    account_cycle = cycle(context.accounts) if context.accounts else None
    parser_account = context.accounts[0] if context.accounts else None
    policy = _json_dict(campaign.policy)
    config_state = _json_dict(campaign.config)
    dry_idle_pass = bool(policy.get("dry_idle_pass"))
    auto_join_policy_configured = "auto_join_sources" in policy or "auto_join_discussions" in policy
    auto_join_discussions = bool(policy.get("auto_join_discussions")) if auto_join_policy_configured else True
    auto_join_sources = bool(policy.get("auto_join_sources")) if auto_join_policy_configured else True
    auto_join_max_per_tick = _policy_auto_join_max_per_tick(policy)
    auto_join_daily_limit = _policy_auto_join_daily_limit(policy)
    auto_join_attempts_by_account: dict[str, int] = {}
    campaign_auto_join_wait_until = _parse_datetime(config_state.get("auto_join_wait_until"))
    auto_join_interval_seconds = _policy_auto_join_interval_seconds(policy)
    delay_seconds = _policy_delay_seconds(policy)
    interval_rule_active = _comment_post_interval_active(policy)
    daily_limit = _comment_daily_limit(policy)
    now = datetime.now(UTC)
    updated_cursors = dict(config_state.get("source_cursors") or {})
    updated_interval_counters, updated_interval_targets, interval_state_reset = _comment_interval_state(config_state)
    daily_sent_by_account = await _sent_comments_today_by_account(
        session,
        workspace_id,
        [account.id for account in context.accounts],
        now,
    )
    sources_to_process, source_rotation_delta = _comment_sources_for_tick(
        context.sources,
        config_state,
        policy,
        throttle_by_default=real_telegram_enabled(),
    )
    sources_processed = 0
    flood_wait_account_ids: set[str] = set()

    for source in sources_to_process:
        now = datetime.now(UTC)
        source_account = _assigned_account_for_source(source.id, context.accounts, config_state) or parser_account
        if source_account and not _account_can_send_comments(source_account):
            sources_processed += 1
            summary.sources_checked += 1
            summary.sources_skipped += 1
            continue
        auto_join_account_key = source_account.id if source_account else "__campaign__"
        source_auto_join_attempts = auto_join_attempts_by_account.get(auto_join_account_key, 0)
        source_fetch_wait_until = _account_source_fetch_wait_until(source_account)
        if source_account and (
            source_account.id in flood_wait_account_ids or (source_fetch_wait_until and now < source_fetch_wait_until)
        ):
            sources_processed += 1
            summary.sources_checked += 1
            summary.sources_skipped += 1
            if run:
                _add_run_event(
                    session,
                    run,
                    "debug",
                    "source_account_flood_wait_deferred",
                    f"Источник «{_source_log_name(source)}» отложен до окончания паузы Telegram для аккаунта.",
                    {
                        "source_id": source.id,
                        "source_title": source.title,
                        "account_id": source_account.id,
                        "wait_until": _iso(source_fetch_wait_until) if source_fetch_wait_until else None,
                    },
                )
            continue
        source_auto_join_wait_until = (
            _account_auto_join_wait_until(source_account) if source_account else campaign_auto_join_wait_until
        )
        source_auto_join_daily_attempts = _account_auto_join_daily_attempts(source_account, now)
        sources_processed += 1
        summary.sources_checked += 1
        if run:
            _add_run_event(
                session,
                run,
                "debug",
                "source_monitoring_started",
                f"Мониторинг источника «{_source_log_name(source)}» начался.",
                {
                    "source_id": source.id,
                    "source_title": source.title,
                    "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                    "account_id": source_account.id if source_account else None,
                    "account_name": _account_log_name(source_account),
                    "assignment_mode": config_state.get("source_assignment_mode") or "standard",
                },
            )
        if not source.comments_enabled:
            refreshed = await _try_prepare_source_comments(
                session=session,
                workspace_id=workspace_id,
                campaign=campaign,
                run=run,
                source=source,
                account=source_account,
                latest_limit=latest_limit,
                auto_join_sources=auto_join_sources,
                auto_join_discussions=auto_join_discussions,
                auto_join_attempts=source_auto_join_attempts,
                auto_join_max_per_tick=auto_join_max_per_tick,
                auto_join_wait_until=source_auto_join_wait_until,
                daily_attempts=source_auto_join_daily_attempts,
                daily_limit=auto_join_daily_limit,
                now=now,
            )
            if refreshed.attempted:
                source_auto_join_attempts += 1
                auto_join_attempts_by_account[auto_join_account_key] = source_auto_join_attempts
                source_auto_join_daily_attempts = _account_auto_join_daily_attempts(source_account, datetime.now(UTC))
                source_auto_join_wait_until = _account_auto_join_wait_until(source_account)
            if source.comments_enabled:
                if run:
                    _add_run_event(
                        session,
                        run,
                        "info",
                        "source_discussion_joined",
                        f"Аккаунт вступил в обсуждение источника «{_source_log_name(source)}», обработка продолжается.",
                        {
                            "source_id": source.id,
                            "source_title": source.title,
                            "account_id": source_account.id if source_account else None,
                        },
                    )
            elif refreshed.archived:
                summary.sources_skipped += 1
                continue
            else:
                summary.sources_skipped += 1
                if run and not refreshed.attempted:
                    _add_run_event(
                        session,
                        run,
                        "debug",
                        "source_discussion_prepare_deferred",
                        f"Подготовка обсуждения источника «{_source_log_name(source)}» отложена до следующего окна auto-join.",
                        {
                            "source_id": source.id,
                            "source_title": source.title,
                            "account_id": source_account.id if source_account else None,
                        },
                    )
                continue

        if real_telegram_enabled() and source_account and latest_limit > 0:
            should_ensure_membership = _should_ensure_source_membership(
                source,
                source_account,
                auto_join_sources,
                source_auto_join_attempts,
                auto_join_max_per_tick,
                source_auto_join_wait_until,
                now,
                daily_attempts=source_auto_join_daily_attempts,
                daily_limit=auto_join_daily_limit,
                join_discussion=auto_join_discussions,
            )
            if should_ensure_membership:
                source_auto_join_attempts += 1
                auto_join_attempts_by_account[auto_join_account_key] = source_auto_join_attempts
                _record_auto_join_attempt(campaign, source_account, datetime.now(UTC), auto_join_interval_seconds)

            if dry_idle_pass and not should_ensure_membership:
                summary.sources_skipped += 1
                continue

            try:
                await fetch_real_posts(
                    session,
                    workspace_id,
                    source,
                    source_account,
                    latest_limit,
                    ensure_membership=should_ensure_membership,
                    join_discussion=auto_join_discussions,
                )
            except TelegramGatewayError as exc:
                _apply_gateway_error_account_risk(session, source_account, exc, now)
                if source_account and _is_frozen_account_error(exc):
                    _remove_blocked_accounts_from_campaign(
                        session,
                        campaign,
                        [
                            (
                                source_account,
                                _friendly_gateway_error(exc, source=source, account=source_account, action="comment"),
                            )
                        ],
                        run=run,
                    )
                is_flood_wait = _is_flood_wait_error(exc)
                if is_flood_wait:
                    if source_account:
                        flood_wait_account_ids.add(source_account.id)

                summary.sources_skipped += 1
                friendly_error = _friendly_gateway_error(exc, source=source, account=source_account, action="read")
                should_alert = _should_alert_source_fetch_error(exc, ensure_membership=should_ensure_membership)
                if _is_source_access_block_error(exc):
                    await _archive_source_after_critical_error(
                        session=session,
                        workspace_id=workspace_id,
                        campaign=campaign,
                        run=run,
                        source=source,
                        account=source_account,
                        error_code=exc.code,
                        error_message=friendly_error,
                        now=now,
                    )
                if run:
                    _add_run_event(
                        session,
                        run,
                        "error",
                        "source_fetch_failed",
                        friendly_error,
                        {
                            "source_id": source.id,
                            "source_title": source.title,
                            "account_id": source_account.id if source_account else None,
                            "error_code": exc.code,
                        },
                    )
                await record_operational_event(
                    session,
                    workspace_id=workspace_id,
                    severity="warning" if is_flood_wait else "error",
                    category="telegram",
                    event_type="source_fetch_failed",
                    title="Источник не прочитан",
                    message=friendly_error,
                    source="campaigns.engine",
                    entity_type="telegram_source",
                    entity_id=source.id,
                    dedupe_key=f"source_fetch_failed:{campaign.id}:{source.id}:{exc.code}",
                    payload={
                        "campaign_id": campaign.id,
                        "campaign_name": campaign.name,
                        "run_id": run.id if run else None,
                        "source_id": source.id,
                        "source_title": source.title,
                        "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                        "account_id": source_account.id if source_account else None,
                        "account_name": _account_log_name(source_account),
                        "error_code": exc.code,
                    },
                    alert=should_alert,
                )
                session.add(
                    CampaignAction(
                        campaign_id=campaign.id,
                        telegram_account_id=source_account.id if source_account else None,
                        target_type="source",
                        target_id=source.id,
                        action_type="parse_source",
                        status="failed",
                        error_code=exc.code,
                        error_message=friendly_error,
                    )
                )

                continue

            if not source.comments_enabled:
                await _archive_source_after_critical_error(
                    session=session,
                    workspace_id=workspace_id,
                    campaign=campaign,
                    run=run,
                    source=source,
                    account=source_account,
                    error_code="COMMENTS_CLOSED",
                    error_message=(
                        f"Источник «{_source_log_name(source)}» больше не имеет доступного обсуждения; "
                        "он исключен до генерации и отправки комментария."
                    ),
                    now=now,
                )
                summary.sources_skipped += 1
                continue

            if dry_idle_pass:
                if run:
                    _add_run_event(
                        session,
                        run,
                        "info",
                        "dry_idle_source_prepared",
                        f"Холостой прогон подготовил источник «{_source_log_name(source)}» без чтения постов и комментариев.",
                        {
                            "source_id": source.id,
                            "source_title": source.title,
                            "account_id": source_account.id,
                        },
                    )
                continue

        observed_messages = await _latest_messages(session, workspace_id, source.id, latest_limit)
        summary.messages_seen += len(observed_messages)
        cursor_before_tick = _json_dict(updated_cursors.get(source.id))
        if update_cursors and not cursor_before_tick:
            summary.cursors_initialized += 1
            updated_cursors[source.id] = _cursor_from_time(run.started_at if run and run.started_at else now)
        candidate_messages = _select_candidate_messages(
            selection_mode,
            source.id,
            observed_messages,
            updated_cursors,
        )
        if selection_mode == "new":
            candidate_messages = _messages_after_polling_cutover(candidate_messages, config_state)

        if selection_mode == "new":
            summary.new_posts_found += len(candidate_messages)

        if run:
            _add_run_event(
                session,
                run,
                "debug",
                "source_posts_loaded",
                f"Источник «{_source_log_name(source)}» прочитан: {len(observed_messages)} постов, кандидатов {len(candidate_messages)}.",
                {
                    "source_id": source.id,
                    "source_title": source.title,
                    "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                    "account_id": source_account.id if source_account else None,
                    "account_name": _account_log_name(source_account),
                    "observed_count": len(observed_messages),
                    "candidate_count": len(candidate_messages),
                    "selection_mode": selection_mode,
                },
            )

        if selection_mode == "new" and delay_seconds > 0:
            candidate_messages = [
                message for message in candidate_messages if _message_ready_after_delay(message, now, delay_seconds)
            ]

        messages = candidate_messages
        if dedupe and messages:
            before_dedupe = len(messages)
            messages = await _without_existing_actions(session, campaign.id, messages)
            summary.duplicate_posts_skipped += before_dedupe - len(messages)

        if messages:
            before_stop_words = len(messages)
            messages = _without_stop_word_messages(messages, policy)
            summary.posts_skipped_by_stop_words += before_stop_words - len(messages)

        if messages:
            before_length_filter = len(messages)
            messages = _without_short_post_messages(messages, policy)
            summary.posts_skipped_by_length += before_length_filter - len(messages)

        if run:
            _add_run_event(
                session,
                run,
                "debug",
                "source_posts_filtered",
                f"После фильтров по «{_source_log_name(source)}» осталось {len(messages)} постов для обработки.",
                {
                    "source_id": source.id,
                    "source_title": source.title,
                    "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                    "account_id": source_account.id if source_account else None,
                    "account_name": _account_log_name(source_account),
                    "ready_count": len(messages),
                },
            )

        if max_comments == 0:
            if update_cursors and candidate_messages:
                updated_cursors[source.id] = _cursor_from_message(max(candidate_messages, key=_message_sort_key))
            continue

        if messages:
            messages, skipped_by_interval, updated_interval_counters, updated_interval_targets = (
                _apply_comment_post_interval(
                    messages,
                    updated_interval_counters,
                    updated_interval_targets,
                    source.id,
                    policy,
                )
            )
            summary.posts_skipped_by_interval += skipped_by_interval

        source_deferred = False
        retain_deferred = bool(policy.get("retain_deferred_comment_posts"))
        for message in messages:
            if max_comments is not None and summary.successful_actions >= max_comments:
                summary.stop_reason = "max_comments"
                break

            account = None
            if delivery_mode == "generate_and_send":
                if _source_assignment_mode(config_state) in {"multi_account_split", "manual_pinned"} and source_account:
                    account = source_account if daily_sent_by_account.get(source_account.id, 0) < daily_limit else None
                else:
                    account = _next_comment_account(context.accounts, account_cycle, daily_sent_by_account, daily_limit)
                if not account:
                    if (
                        retain_deferred
                        and source_account
                        and _source_assignment_mode(config_state) in {"multi_account_split", "manual_pinned"}
                    ):
                        source_deferred = True
                        break
                    summary.stop_reason = "daily_limit"
                    break
            elif account_cycle:
                account = next(account_cycle)
            if delivery_mode == "generate_and_send":
                wait_reason = await _comment_account_send_wait_reason(
                    session, workspace_id, account, policy, now, campaign=campaign
                )
                if wait_reason:
                    if run:
                        _add_run_event(
                            session,
                            run,
                            "debug",
                            "comment_send_deferred",
                            "Отправка отложена по ограничению аккаунта.",
                            {
                                "account_id": account.id if account else None,
                                "source_id": source.id,
                                "reason": wait_reason,
                            },
                        )
                    if retain_deferred:
                        source_deferred = True
                        break
                    continue
            try:
                result = await _create_comment_action(
                    session=session,
                    workspace_id=workspace_id,
                    campaign=campaign,
                    run=run,
                    prompt=context.prompt,
                    source=source,
                    message=message,
                    account=account,
                    delivery_mode=delivery_mode,
                    now=now,
                )
            except AiProviderError as exc:
                result = await _create_failed_ai_comment_action(
                    session=session,
                    workspace_id=workspace_id,
                    campaign=campaign,
                    run=run,
                    source=source,
                    message=message,
                    account=account,
                    delivery_mode=delivery_mode,
                    error_message=str(exc),
                    now=now,
                )
            summary.actions_created += 1
            if result.get("ai_generated", True):
                summary.comments_generated += 1
            if result["action_status"] in {"succeeded", "generated"}:
                summary.successful_actions += 1
            if result["comment_status"] == "sent":
                summary.comments_sent += 1
                if account:
                    daily_sent_by_account[account.id] = daily_sent_by_account.get(account.id, 0) + 1
            elif result["comment_status"] == "draft":
                summary.comments_drafted += 1
            elif result["comment_status"] == "failed":
                summary.comments_failed += 1

            if update_cursors:
                updated_cursors[source.id] = _cursor_from_message(message)
            # Exclusion takes effect immediately, including the remaining posts
            # already loaded for this source in the current polling batch.
            current_config = _json_dict(campaign.config)
            if source.id not in current_config.get("source_ids", []) or (
                account
                and f"{account.id}:{source.id}" in _json_dict(current_config.get("blocked_account_source_pairs"))
            ):
                break
            if max_comments is not None and summary.successful_actions >= max_comments:
                summary.stop_reason = "max_comments"
                break

        if update_cursors and not summary.stop_reason and candidate_messages and not source_deferred:
            updated_cursors[source.id] = _cursor_from_message(max(candidate_messages, key=_message_sort_key))
        elif update_cursors and not _json_dict(updated_cursors.get(source.id)) and observed_messages:
            updated_cursors[source.id] = _cursor_from_message(observed_messages[0])

        if summary.stop_reason:
            break

    should_update_source_rotation = source_rotation_delta > 0 and len(context.sources) > len(sources_to_process)
    if update_cursors or interval_rule_active or should_update_source_rotation:
        config_updates: dict[str, Any] = {}
        if update_cursors:
            config_updates["source_cursors"] = updated_cursors
        if should_update_source_rotation:
            config_updates["source_rotation_index"] = (
                _safe_int(config_state.get("source_rotation_index"), default=0) + sources_processed
            ) % len(context.sources)
        if interval_rule_active:
            config_updates["comment_post_interval_counters"] = updated_interval_counters
            config_updates["comment_post_interval_targets"] = updated_interval_targets
            config_updates["comment_post_interval_state_version"] = COMMENT_POST_INTERVAL_STATE_VERSION
        elif interval_state_reset:
            config_updates["comment_post_interval_counters"] = {}
            config_updates["comment_post_interval_targets"] = {}
            config_updates["comment_post_interval_state_version"] = COMMENT_POST_INTERVAL_STATE_VERSION
        campaign.config = {**_json_dict(campaign.config), **config_updates}
    return summary


async def _try_prepare_source_comments(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun | None,
    source: TelegramSource,
    account: TelegramAccount | None,
    latest_limit: int,
    auto_join_sources: bool,
    auto_join_discussions: bool,
    auto_join_attempts: int,
    auto_join_max_per_tick: int,
    auto_join_wait_until: datetime | None,
    daily_attempts: int,
    daily_limit: int | None,
    now: datetime,
) -> SourceCommentPreparationResult:
    if not real_telegram_enabled() or not account or latest_limit <= 0 or not auto_join_discussions:
        return SourceCommentPreparationResult()
    if not auto_join_sources:
        session.add(
            CampaignAction(
                campaign_id=campaign.id,
                telegram_account_id=account.id,
                target_type="source",
                target_id=source.id,
                action_type="prepare_source_comments",
                status="failed",
                error_code="COMMENTS_CLOSED",
                error_message="Вступление в источник отключено политикой кампании.",
            )
        )
        await _archive_source_after_critical_error(
            session=session,
            workspace_id=workspace_id,
            campaign=campaign,
            run=run,
            source=source,
            account=account,
            error_code="COMMENTS_CLOSED",
            error_message=(
                "У источника нет подтвержденного обсуждения, а вступление в сам источник отключено. "
                "Источник отправлен в архив и удален из активных кампаний."
            ),
            now=now,
        )
        return SourceCommentPreparationResult(archived=True)
    if not _should_ensure_source_membership(
        source,
        account,
        auto_join_sources,
        auto_join_attempts,
        auto_join_max_per_tick,
        auto_join_wait_until,
        now,
        daily_attempts=daily_attempts,
        daily_limit=daily_limit,
        join_discussion=True,
    ):
        return SourceCommentPreparationResult()

    _record_auto_join_attempt(campaign, account, datetime.now(UTC), _policy_auto_join_interval_seconds(campaign.policy))
    try:
        await fetch_real_posts(
            session,
            workspace_id,
            source,
            account,
            latest_limit,
            ensure_membership=True,
            join_discussion=True,
        )
    except TelegramGatewayError as exc:
        _apply_gateway_error_account_risk(session, account, exc, now)
        friendly_error = _friendly_gateway_error(exc, source=source, account=account, action="read")
        if _is_source_access_block_error(exc):
            await _archive_source_after_critical_error(
                session=session,
                workspace_id=workspace_id,
                campaign=campaign,
                run=run,
                source=source,
                account=account,
                error_code=exc.code,
                error_message=friendly_error,
                now=now,
            )
            return SourceCommentPreparationResult(attempted=True, archived=True)
        if run:
            _add_run_event(
                session,
                run,
                "warning",
                "source_discussion_join_failed",
                friendly_error,
                {
                    "source_id": source.id,
                    "source_title": source.title,
                    "account_id": account.id,
                    "error_code": exc.code,
                },
            )
        session.add(
            CampaignAction(
                campaign_id=campaign.id,
                target_type="source",
                target_id=source.id,
                action_type="prepare_source_comments",
                status="failed",
                error_code=exc.code,
                error_message=friendly_error,
            )
        )
        return SourceCommentPreparationResult(attempted=True)

    if source.comments_enabled:
        return SourceCommentPreparationResult(attempted=True)

    await _archive_source_after_critical_error(
        session=session,
        workspace_id=workspace_id,
        campaign=campaign,
        run=run,
        source=source,
        account=account,
        error_code="COMMENTS_CLOSED",
        error_message=(
            "У источника нет подключенного обсуждения для комментариев. "
            "Автоматически вступить некуда, поэтому источник отправлен в архив и удален из активных кампаний."
        ),
        now=now,
    )
    return SourceCommentPreparationResult(attempted=True, archived=True)


async def _latest_messages(
    session: AsyncSession,
    workspace_id: str,
    source_id: str,
    latest_limit: int,
) -> list[TelegramMessage]:
    if latest_limit <= 0:
        return []
    result = await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.workspace_id == workspace_id, TelegramMessage.source_id == source_id)
        .order_by(TelegramMessage.published_at.desc(), TelegramMessage.created_at.desc())
        .limit(latest_limit)
    )
    return list(result.scalars().all())


def _select_candidate_messages(
    selection_mode: str,
    source_id: str,
    observed_messages: list[TelegramMessage],
    source_cursors: dict[str, Any],
) -> list[TelegramMessage]:
    if selection_mode != "new":
        return observed_messages

    cursor = _json_dict(source_cursors.get(source_id))
    if not cursor:
        return []

    messages = [message for message in observed_messages if _message_after_cursor(message, cursor)]
    return sorted(messages, key=_message_sort_key)


async def _without_existing_actions(
    session: AsyncSession,
    campaign_id: str,
    messages: list[TelegramMessage],
) -> list[TelegramMessage]:
    message_ids = [message.id for message in messages]
    action_result = await session.execute(
        select(CampaignAction.target_id).where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.target_type == "telegram_message",
            CampaignAction.target_id.in_(message_ids),
        )
    )
    comment_result = await session.execute(
        select(TelegramComment.post_message_id).where(
            TelegramComment.campaign_id == campaign_id,
            TelegramComment.post_message_id.in_(message_ids),
        )
    )
    acted_ids = set(action_result.scalars().all()) | set(comment_result.scalars().all())
    return [message for message in messages if message.id not in acted_ids]


async def _without_existing_reaction_actions(
    session: AsyncSession,
    campaign_id: str,
    messages: list[TelegramMessage],
) -> list[TelegramMessage]:
    if not messages:
        return []
    result = await session.execute(
        select(CampaignAction.target_id).where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status.in_(["queued", "succeeded", "sent", "generated", "failed"]),
        )
    )
    acted = set(result.scalars().all())
    selected: list[TelegramMessage] = []
    for message in messages:
        prefix = _legacy_reaction_target_prefix(message.source_id, message.tg_message_id)
        if message.id in acted or any(target_id.startswith(f"{prefix}:") for target_id in acted):
            continue
        selected.append(message)
    return selected


async def _reaction_targets_for_source(
    *,
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    source_ref: str,
    account: TelegramAccount,
    target_scope: str,
    latest_limit: int,
    comments_per_post: int,
    messages_per_source: int,
    refresh_latest: bool = False,
) -> list[ReactionTarget]:
    if target_scope == "comments":
        return await _reaction_comment_targets(
            session=session,
            workspace_id=workspace_id,
            source=source,
            source_ref=source_ref,
            account=account,
            latest_limit=latest_limit,
            comments_per_post=comments_per_post,
        )
    if target_scope == "chat_messages":
        return await _reaction_chat_message_targets(
            session=session,
            workspace_id=workspace_id,
            source=source,
            source_ref=source_ref,
            account=account,
            messages_per_source=messages_per_source,
        )
    messages = await _latest_messages(session, workspace_id, source.id, latest_limit)
    if (refresh_latest or len(messages) < latest_limit) and real_telegram_enabled():
        try:
            messages = await fetch_real_posts(session, workspace_id, source, account, latest_limit)
        except TelegramGatewayError:
            pass
    return [
        ReactionTarget(
            source_id=source.id,
            source_ref=source_ref,
            source_title=source.title,
            target_kind="telegram_message",
            target_tg_message_id=message.tg_message_id,
            local_message_id=message.id,
            text=message.text,
            published_at=_message_datetime(message),
            raw=_json_dict(message.raw),
        )
        for message in messages
    ]


async def _reaction_comment_targets(
    *,
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    source_ref: str,
    account: TelegramAccount,
    latest_limit: int,
    comments_per_post: int,
) -> list[ReactionTarget]:
    local_limit = max(latest_limit * comments_per_post, comments_per_post)
    result = await session.execute(
        select(TelegramComment)
        .where(
            TelegramComment.workspace_id == workspace_id,
            TelegramComment.source_id == source.id,
            TelegramComment.tg_comment_message_id.is_not(None),
        )
        .order_by(func.coalesce(TelegramComment.sent_at, TelegramComment.created_at).desc())
        .limit(local_limit)
    )
    local_comments = list(result.scalars().all())
    if local_comments:
        return [
            ReactionTarget(
                source_id=source.id,
                source_ref=source_ref,
                source_title=source.title,
                target_kind="telegram_comment",
                target_tg_message_id=str(comment.tg_comment_message_id),
                local_message_id=comment.post_message_id,
                text=comment.text,
                published_at=comment.sent_at or comment.created_at,
                raw={
                    **_json_dict(comment.raw),
                    "local_comment_id": comment.id,
                    "post_message_id": comment.post_message_id,
                    "reaction_peer_ref": _source_discussion_ref(source) or source_ref,
                },
            )
            for comment in local_comments
            if comment.tg_comment_message_id
        ]

    if not real_telegram_enabled():
        return []
    posts = await _latest_messages(session, workspace_id, source.id, latest_limit)
    targets: list[ReactionTarget] = []
    for post in posts:
        try:
            comments = await fetch_post_comments_with_account(
                session,
                account,
                source_ref,
                post.tg_message_id,
                comments_per_post,
            )
        except TelegramGatewayError:
            continue
        targets.extend(
            _reaction_target_from_gateway_comment(
                source=source,
                source_ref=source_ref,
                post=post,
                comment=comment,
            )
            for comment in comments
        )
    return targets


def _reaction_target_from_gateway_comment(
    *,
    source: TelegramSource,
    source_ref: str,
    post: TelegramMessage,
    comment: TelegramCommentInfo,
) -> ReactionTarget:
    return ReactionTarget(
        source_id=source.id,
        source_ref=source_ref,
        source_title=source.title,
        target_kind="telegram_comment",
        target_tg_message_id=comment.tg_comment_message_id,
        local_message_id=post.id,
        text=comment.text,
        published_at=_parse_datetime(comment.published_at) or _message_datetime(post),
        raw={
            "post_tg_message_id": post.tg_message_id,
            **_json_dict(comment.raw),
            "reaction_peer_ref": _source_discussion_ref(source) or source_ref,
        },
    )


async def _reaction_chat_message_targets(
    *,
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    source_ref: str,
    account: TelegramAccount,
    messages_per_source: int,
) -> list[ReactionTarget]:
    local_messages = await _latest_messages(session, workspace_id, source.id, messages_per_source)
    if local_messages:
        return [
            ReactionTarget(
                source_id=source.id,
                source_ref=source_ref,
                source_title=source.title,
                target_kind="chat_message",
                target_tg_message_id=message.tg_message_id,
                local_message_id=message.id,
                text=message.text,
                published_at=_message_datetime(message),
                raw=_json_dict(message.raw),
            )
            for message in local_messages
        ]

    if not real_telegram_enabled():
        return []
    try:
        messages = await fetch_chat_messages_with_account(session, account, source_ref, messages_per_source)
    except TelegramGatewayError:
        return []
    return [
        ReactionTarget(
            source_id=source.id,
            source_ref=source_ref,
            source_title=source.title,
            target_kind="chat_message",
            target_tg_message_id=message.tg_message_id,
            local_message_id=None,
            text=message.text,
            published_at=_parse_datetime(message.published_at),
            raw=_json_dict(message.raw),
        )
        for message in messages
        if not message.outgoing
    ]


def _select_candidate_reaction_targets(
    selection_mode: str,
    source_id: str,
    observed_targets: list[ReactionTarget],
    source_cursors: dict[str, Any],
) -> list[ReactionTarget]:
    if selection_mode != "new":
        return observed_targets

    cursor = _json_dict(source_cursors.get(source_id))
    if not cursor:
        return []
    targets = [target for target in observed_targets if _reaction_target_after_cursor(target, cursor)]
    return sorted(targets, key=_reaction_target_sort_key)


async def _without_existing_reaction_target_actions(
    session: AsyncSession,
    campaign_id: str,
    targets: list[ReactionTarget],
) -> list[ReactionTarget]:
    if not targets:
        return []
    result = await session.execute(
        select(CampaignAction.target_id).where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status.in_(["queued", "succeeded", "sent", "generated", "failed"]),
        )
    )
    acted = set(result.scalars().all())
    selected: list[ReactionTarget] = []
    for target in targets:
        prefixes = [_reaction_target_prefix(target.source_id, target.target_kind, target.target_tg_message_id)]
        if target.target_kind == "telegram_message":
            prefixes.append(_legacy_reaction_target_prefix(target.source_id, target.target_tg_message_id))
        if target.local_message_id in acted:
            continue
        if any(any(target_id.startswith(f"{prefix}:") for target_id in acted) for prefix in prefixes):
            continue
        selected.append(target)
    return selected


async def _existing_reaction_target_ids(session: AsyncSession, campaign_id: str) -> set[str]:
    result = await session.execute(
        select(CampaignAction.target_id).where(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status.in_(["queued", "succeeded", "sent", "generated", "failed"]),
        )
    )
    return {str(target_id) for target_id in result.scalars().all() if target_id}


def _select_reaction_for_target(
    *,
    reactions: list[str],
    reaction_rotation: str,
    reaction_index: int,
    target: ReactionTarget,
    reaction_weights: dict[str, int],
) -> tuple[str | None, int, str | None]:
    mode = (
        reaction_rotation
        if reaction_rotation in {"fixed", "random", "round_robin", "weighted", "ai_context"}
        else "round_robin"
    )
    if mode == "fixed":
        return reactions[0], reaction_index, None
    if mode == "round_robin":
        return reactions[reaction_index % len(reactions)], reaction_index + 1, None
    if mode == "weighted":
        return _weighted_reaction(reactions, reaction_weights), reaction_index, None
    if mode == "ai_context":
        reaction = _ai_context_reaction(reactions, target)
        return reaction, reaction_index, "ai_context" if not reaction else None
    return random.choice(reactions), reaction_index, None


def _weighted_reaction(reactions: list[str], reaction_weights: dict[str, int]) -> str:
    weighted = [(reaction, max(0, int(reaction_weights.get(reaction, 0) or 0))) for reaction in reactions]
    if not any(weight for _reaction, weight in weighted):
        return random.choice(reactions)
    return random.choices(
        [reaction for reaction, _weight in weighted], weights=[weight for _reaction, weight in weighted], k=1
    )[0]


def _ai_context_reaction(reactions: list[str], target: ReactionTarget) -> str | None:
    text = (target.text or "").lower()
    if any(word in text for word in ("трагедия", "погиб", "умер", "катастроф", "теракт", "война", "пожар")):
        return None
    if any(word in text for word in ("запуск", "релиз", "новинка", "старт", "огонь", "важно")) and "🔥" in reactions:
        return "🔥"
    if any(word in text for word in ("спасибо", "поддерж", "помог", "красиво", "тепло")) and "❤️" in reactions:
        return "❤️"
    if any(mark in text for mark in ("?", "опрос", "вопрос", "обсуж")) and "👀" in reactions:
        return "👀"
    if "👍" in reactions:
        return "👍"
    return reactions[0] if reactions else None


def _reaction_target_scope(value: Any) -> str:
    scope = str(value or "posts")
    return scope if scope in {"posts", "comments", "chat_messages"} else "posts"


def _reaction_peer_ref(target: ReactionTarget) -> str:
    raw = _json_dict(target.raw)
    peer_ref = raw.get("reaction_peer_ref") or raw.get("discussion_peer_ref") or raw.get("peer_ref")
    return str(peer_ref or target.source_ref)


def _source_discussion_ref(source: TelegramSource) -> str | None:
    if source.linked_discussion_username:
        return source_ref_from_fields(source.linked_discussion_username, None, source.linked_discussion_tg_id)
    if source.linked_discussion_tg_id:
        discussion_id = str(source.linked_discussion_tg_id).strip()
        if discussion_id and not discussion_id.startswith("-100"):
            return f"-100{discussion_id.lstrip('-')}"
        return discussion_id
    return None


def _reaction_source_ref(source: TelegramSource) -> str:
    return _source_peer_ref(source)


def _source_peer_ref(source: TelegramSource) -> str:
    if source.username or source.url or not source.tg_id:
        return source_ref_from_fields(source.username, source.url, source.tg_id)
    tg_id = str(source.tg_id).strip()
    if source.type == "group" and tg_id and not tg_id.startswith("-"):
        return f"-{tg_id}"
    if source.type == "channel" and tg_id and not tg_id.startswith("-100"):
        return f"-100{tg_id.lstrip('-')}"
    return tg_id


def _reaction_weights(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw_weight in value.items():
        reaction = str(key).strip()
        if not reaction:
            continue
        result[reaction] = _safe_int(raw_weight, default=0)
    return result


def _reaction_target_sort_key(target: ReactionTarget) -> tuple[datetime, str]:
    return target.published_at or datetime.min.replace(tzinfo=UTC), target.target_tg_message_id


def _reaction_target_after_cursor(target: ReactionTarget, cursor: dict[str, Any]) -> bool:
    cursor_at = _parse_datetime(cursor.get("last_seen_at"))
    if cursor_at and target.published_at:
        return target.published_at > cursor_at
    cursor_tg_message_id = cursor.get("last_seen_tg_message_id")
    return bool(cursor_tg_message_id and target.target_tg_message_id != cursor_tg_message_id)


def _cursor_from_reaction_target(target: ReactionTarget) -> dict[str, str | None]:
    return {
        "last_seen_message_id": target.local_message_id,
        "last_seen_tg_message_id": target.target_tg_message_id,
        "last_seen_target_kind": target.target_kind,
        "last_seen_at": _iso(target.published_at),
    }


def _legacy_reaction_target_prefix(source_id: str, tg_message_id: str) -> str:
    return f"{source_id}:{tg_message_id}"


def _reaction_target_prefix(source_id: str, target_kind: str, tg_message_id: str) -> str:
    raw = f"{source_id}:{target_kind}:{tg_message_id}"
    return f"rx:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _reaction_target_id(source_id: str, target_kind: str, tg_message_id: str, account_id: str) -> str:
    account_hash = hashlib.sha1(account_id.encode("utf-8")).hexdigest()[:10]
    return f"{_reaction_target_prefix(source_id, target_kind, tg_message_id)}:{account_hash}"


async def _create_failed_ai_comment_action(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun | None,
    source: TelegramSource,
    message: TelegramMessage,
    account: TelegramAccount | None,
    delivery_mode: str,
    error_message: str,
    now: datetime,
) -> dict[str, str | bool]:
    error_code = "AI_PROVIDER_REQUEST_ERROR"
    friendly_error = f"AI не сгенерировал комментарий для «{_source_log_name(source)}»: {error_message}"
    if run:
        _add_run_event(
            session,
            run,
            "error",
            "comment_generation_failed",
            friendly_error,
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                "account_id": account.id if account else None,
                "account_name": _account_log_name(account),
                "post_message_id": message.id,
                "post_tg_message_id": message.tg_message_id,
                "error_code": error_code,
            },
        )
    await record_operational_event(
        session,
        workspace_id=workspace_id,
        severity="warning",
        category="ai",
        event_type="comment_generation_failed",
        title="Комментарий не сгенерирован",
        message=friendly_error,
        source="campaigns.engine",
        entity_type="campaign",
        entity_id=campaign.id,
        dedupe_key=f"comment_generation_failed:{campaign.id}:{source.id}:{message.id}",
        payload={
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "run_id": run.id if run else None,
            "source_id": source.id,
            "source_title": source.title,
            "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
            "account_id": account.id if account else None,
            "account_name": _account_log_name(account),
            "post_message_id": message.id,
            "post_tg_message_id": message.tg_message_id,
            "error_code": error_code,
        },
        alert=False,
    )
    action = CampaignAction(
        campaign_id=campaign.id,
        telegram_account_id=account.id if account else None,
        target_type="telegram_message",
        target_id=message.id,
        action_type="generate_and_send_comment" if delivery_mode == "generate_and_send" else "generate_comment",
        status="failed",
        planned_at=now,
        result={"mode": delivery_mode, "stage": "ai_generation"},
        error_code=error_code,
        error_message=friendly_error,
    )
    session.add(action)
    await session.flush()
    session.add(
        TelegramComment(
            workspace_id=workspace_id,
            source_id=source.id,
            post_message_id=message.id,
            campaign_id=campaign.id,
            campaign_action_id=action.id,
            telegram_account_id=account.id if account else None,
            ai_generation_id=None,
            text="",
            status="failed",
            sent_at=None,
            error_code=error_code,
            error_message=friendly_error,
            raw={"mode": delivery_mode, "error": {"code": error_code, "message": error_message}},
        )
    )
    return {"action_status": "failed", "comment_status": "failed", "ai_generated": False}


async def _create_comment_action(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun | None = None,
    prompt: CommentPromptTemplate,
    source: TelegramSource,
    message: TelegramMessage,
    account: TelegramAccount | None,
    delivery_mode: str,
    now: datetime,
) -> dict[str, str]:
    generation_prompt = _prompt_with_campaign_length(prompt, campaign)
    if run:
        _add_run_event(
            session,
            run,
            "info",
            "comment_generation_started",
            f"Генерация комментария для «{_source_log_name(source)}» началась.",
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                "account_id": account.id if account else None,
                "account_name": _account_log_name(account),
                "post_message_id": message.id,
                "post_tg_message_id": message.tg_message_id,
            },
        )
    comment_text, token_usage = await generate_comment_text(
        session,
        generation_prompt,
        message.text or "",
        source.title,
        source.id,
        author_gender=normalize_author_gender(_json_dict(campaign.config).get("author_gender")),
    )
    moderation_status = _moderation_status(comment_text, prompt.blacklist_words)
    if run:
        _add_run_event(
            session,
            run,
            "success" if moderation_status == "passed" else "warning",
            "comment_generated",
            "Комментарий сгенерирован."
            if moderation_status == "passed"
            else "Комментарий сгенерирован, но остановлен модерацией.",
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                "account_id": account.id if account else None,
                "account_name": _account_log_name(account),
                "post_message_id": message.id,
                "post_tg_message_id": message.tg_message_id,
                "moderation_status": moderation_status,
            },
        )

    generation = AiGeneration(
        workspace_id=workspace_id,
        profile_id=prompt.ai_profile_id,
        comment_prompt_template_id=prompt.id,
        campaign_id=campaign.id,
        input={
            "source_id": source.id,
            "message_id": message.id,
            "prompt_version": prompt.version,
        },
        output_text=comment_text,
        moderation_status=moderation_status,
        token_usage=token_usage,
    )
    session.add(generation)
    await session.flush()

    blocked = moderation_status != "passed"
    action_status = "skipped" if blocked else ("succeeded" if delivery_mode == "generate_and_send" else "generated")
    comment_status = "failed" if blocked else ("sent" if delivery_mode == "generate_and_send" else "draft")
    tg_comment_message_id = None
    send_raw = {"mode": "scaffold"}
    error_code = "MODERATION_BLOCKED" if blocked else None
    error_message = "Generated comment contains blacklisted words" if blocked else None

    if not blocked and delivery_mode == "generate_and_send" and real_telegram_enabled():
        if not account:
            action_status = "failed"
            comment_status = "failed"
            error_code = "ACCOUNT_REQUIRED"
            error_message = "Telegram account is required to send a real comment"
        else:
            try:
                wait_reason = await _comment_account_send_wait_reason(
                    session,
                    workspace_id,
                    account,
                    _json_dict(campaign.policy),
                    datetime.now(UTC),
                    campaign=campaign,
                )
                if wait_reason:
                    raise TelegramGatewayError("COMMENT_SEND_DEFERRED", wait_reason)
                if run:
                    _add_run_event(
                        session,
                        run,
                        "info",
                        "comment_send_started",
                        f"Отправка комментария в «{_source_log_name(source)}» началась.",
                        {
                            "source_id": source.id,
                            "source_title": source.title,
                            "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                            "account_id": account.id,
                            "account_name": _account_log_name(account),
                            "post_message_id": message.id,
                            "post_tg_message_id": message.tg_message_id,
                        },
                    )
                _record_account_attempt(account, "comment_send_attempts_24h", datetime.now(UTC))
                send_result = await send_comment_with_account(
                    session=session,
                    account=account,
                    source_ref=source_ref_from_fields(source.username, source.url, source.tg_id),
                    post_tg_message_id=message.tg_message_id,
                    text=comment_text,
                )
                tg_comment_message_id = send_result.tg_message_id
                send_raw = send_result.raw
            except TelegramGatewayError as exc:
                wait_reason = await _comment_account_send_wait_reason(
                    session,
                    workspace_id,
                    account,
                    _json_dict(campaign.policy),
                    datetime.now(UTC),
                    campaign=campaign,
                )
                rebound = (
                    None
                    if wait_reason
                    else await _retry_comment_after_post_rebind(
                        session=session,
                        workspace_id=workspace_id,
                        source=source,
                        message=message,
                        account=account,
                        comment_text=comment_text,
                        original_error=exc,
                    )
                )
                if rebound:
                    message = rebound["message"]
                    send_result = rebound["send_result"]
                    tg_comment_message_id = send_result.tg_message_id
                    send_raw = send_result.raw
                else:
                    _apply_gateway_error_account_risk(session, account, exc, now, comment_send=True)
                    action_status = "failed"
                    comment_status = "failed"
                    error_code = exc.code
                    error_message = _friendly_gateway_error(exc, source=source, account=account, action="comment")
                    if _is_frozen_account_error(exc):
                        _remove_blocked_accounts_from_campaign(
                            session,
                            campaign,
                            [(account, error_message)],
                            run=run,
                        )
                    send_raw = {"mode": "telethon", "error": {"code": exc.code, **exc.raw}}
                    if exc.code == "UserBannedInChannelError":
                        _exclude_comment_account_source_pair(campaign, source, account, now)
                    elif _is_source_access_block_error(exc):
                        _exclude_comment_account_source_pair(
                            campaign, source, account, now, str(exc.raw.get("rpc_error", exc.code))
                        )
                    if run:
                        _add_run_event(
                            session,
                            run,
                            "error",
                            "comment_send_failed",
                            error_message,
                            {
                                "source_id": source.id,
                                "source_title": source.title,
                                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                                "account_id": account.id if account else None,
                                "account_name": _account_log_name(account),
                                "post_message_id": message.id,
                                "post_tg_message_id": message.tg_message_id,
                                "error_code": exc.code,
                            },
                        )
                    await record_operational_event(
                        session,
                        workspace_id=workspace_id,
                        severity="warning" if _is_joinable_comment_error(exc) else "error",
                        category="telegram",
                        event_type="comment_send_failed",
                        title="Комментарий не отправлен",
                        message=error_message,
                        source="campaigns.engine",
                        entity_type="campaign",
                        entity_id=campaign.id,
                        dedupe_key=f"comment_send_failed:{campaign.id}:{source.id}:{exc.code}",
                        payload={
                            "campaign_id": campaign.id,
                            "campaign_name": campaign.name,
                            "run_id": run.id if run else None,
                            "source_id": source.id,
                            "source_title": source.title,
                            "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                            "account_id": account.id if account else None,
                            "account_name": _account_log_name(account),
                            "error_code": exc.code,
                        },
                        alert=not _is_joinable_comment_error(exc),
                    )

    if run and not blocked and comment_status == "sent":
        _add_run_event(
            session,
            run,
            "success",
            "comment_sent",
            f"Комментарий отправлен в «{_source_log_name(source)}».",
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                "account_id": account.id if account else None,
                "account_name": _account_log_name(account),
                "post_message_id": message.id,
                "post_tg_message_id": message.tg_message_id,
                "comment_tg_message_id": tg_comment_message_id,
            },
        )
    elif run and not blocked and comment_status == "draft":
        _add_run_event(
            session,
            run,
            "success",
            "comment_drafted",
            f"Черновик комментария для «{_source_log_name(source)}» создан.",
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
                "account_id": account.id if account else None,
                "account_name": _account_log_name(account),
                "post_message_id": message.id,
                "post_tg_message_id": message.tg_message_id,
            },
        )

    action = CampaignAction(
        campaign_id=campaign.id,
        telegram_account_id=account.id if account else None,
        target_type="telegram_message",
        target_id=message.id,
        action_type="generate_and_send_comment" if delivery_mode == "generate_and_send" else "generate_comment",
        status=action_status,
        planned_at=now,
        executed_at=now if action_status in {"succeeded", "generated"} else None,
        result={"ai_generation_id": generation.id, "mode": delivery_mode},
        error_code=error_code,
        error_message=error_message,
    )
    session.add(action)
    await session.flush()

    fallback_comment_id = None
    if comment_status == "sent" and not real_telegram_enabled():
        fallback_comment_id = f"mock-comment-{action.id[:8]}"

    comment = TelegramComment(
        workspace_id=workspace_id,
        source_id=source.id,
        post_message_id=message.id,
        campaign_id=campaign.id,
        campaign_action_id=action.id,
        telegram_account_id=account.id if account else None,
        ai_generation_id=generation.id,
        tg_comment_message_id=tg_comment_message_id or fallback_comment_id,
        text=comment_text,
        status=comment_status,
        sent_at=now if comment_status == "sent" else None,
        error_code=action.error_code,
        error_message=action.error_message,
        raw=send_raw,
    )
    session.add(comment)
    return {"action_status": action_status, "comment_status": comment_status}


async def _retry_comment_after_post_rebind(
    *,
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    message: TelegramMessage,
    account: TelegramAccount,
    comment_text: str,
    original_error: TelegramGatewayError,
) -> dict[str, TelegramMessage | TelegramSendResult] | None:
    # Never substitute a different post or resend after a failed comment operation.
    return None


def _should_rebind_comment_post(exc: TelegramGatewayError) -> bool:
    error_text = f"{exc.code} {exc}".lower()
    if not any(marker in error_text for marker in ["msgidinvalid", "message id invalid"]):
        return False
    raw = exc.raw if isinstance(exc.raw, dict) else {}
    resolution = str(raw.get("comment_target_resolution") or "").lower()
    return not resolution or resolution in {"direct", "album_no_commentable_item"}


def _matching_rebound_post(message: TelegramMessage, posts: list[TelegramPostInfo]) -> TelegramPostInfo | None:
    current_id = str(message.tg_message_id or "")
    current_text = _normalize_post_match_text(message.text)
    if not current_text:
        return None
    candidates = [post for post in posts if str(post.tg_message_id) != current_id]
    for post in candidates:
        if _normalize_post_match_text(post.text) == current_text:
            return post
    for post in candidates:
        candidate_text = _normalize_post_match_text(post.text)
        if len(current_text) >= 120 and (current_text in candidate_text or candidate_text in current_text):
            return post
    return None


async def _apply_rebound_post(
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    message: TelegramMessage,
    post: TelegramPostInfo,
    original_error: TelegramGatewayError,
) -> TelegramMessage:
    existing_result = await session.execute(
        select(TelegramMessage).where(
            TelegramMessage.workspace_id == workspace_id,
            TelegramMessage.source_id == source.id,
            TelegramMessage.tg_message_id == post.tg_message_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    target = existing or message
    target.text = post.text
    target.published_at = post.published_at
    target.views_count = post.views_count
    target.forwards_count = post.forwards_count
    target.replies_count = post.replies_count
    target.raw = {
        **_json_dict(post.raw),
        "post_id_rebound": {
            "from_local_message_id": message.id,
            "from_tg_message_id": message.tg_message_id,
            "to_tg_message_id": post.tg_message_id,
            "reason": original_error.code,
        },
    }
    if not existing:
        target.tg_message_id = post.tg_message_id
    await session.flush()
    return target


def _normalize_post_match_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text[:2000]


def _prompt_with_campaign_length(
    prompt: CommentPromptTemplate,
    campaign: Campaign,
) -> CommentPromptTemplate:
    policy = _json_dict(campaign.policy)
    max_length = _safe_int(policy.get("max_length"), default=prompt.max_length)
    max_length = max(40, min(max_length, 1000))
    if max_length == prompt.max_length:
        return prompt
    runtime_prompt = copy.copy(prompt)
    runtime_prompt.max_length = max_length
    return runtime_prompt


def _is_source_access_block_error(exc: TelegramGatewayError) -> bool:
    if exc.code in {"SOURCE_ACCESS_DENIED", "SOURCE_READ_DENIED", "JOIN_APPROVAL_PENDING"}:
        return True
    error_text = f"{exc.code} {exc}".lower()
    if exc.code == "TELEGRAM_CONNECTION_FAILED":
        return False
    blocking_markers = [
        "channelprivate",
        "chatwriteforbidden",
        "lack permission",
        "not return a sent comment",
        "comment_send_forbidden",
        "write forbidden",
        "inviterequestsent",
    ]
    return any(marker in error_text for marker in blocking_markers)


def _exclude_comment_account_source_pair(
    campaign: Campaign,
    source: TelegramSource,
    account: TelegramAccount,
    now: datetime,
    error_code: str = "UserBannedInChannelError",
) -> None:
    config = _json_dict(campaign.config)
    assignment = {key: list(value) for key, value in _json_dict(config.get("source_assignment")).items()}
    if _source_assignment_mode(config) != "manual_pinned":
        assignment = {account.id: list(config.get("source_ids", []))}
    if source.id not in assignment.get(account.id, []):
        return
    assignment[account.id] = [source_id for source_id in assignment[account.id] if source_id != source.id]
    config["source_assignment"] = assignment
    still_assigned = any(source.id in source_ids for source_ids in assignment.values())
    if not still_assigned:
        config["source_ids"] = [source_id for source_id in config.get("source_ids", []) if source_id != source.id]
    exclusion = {
        "source_id": source.id,
        "account_id": account.id,
        "reason": "comment_write_denied_for_account",
        "error_code": error_code,
        "blocked_at": _iso(now),
    }
    if still_assigned:
        blocked_pairs = _json_dict(config.get("blocked_account_source_pairs"))
        blocked_pairs[f"{account.id}:{source.id}"] = exclusion
        config["blocked_account_source_pairs"] = blocked_pairs
    else:
        blocked = _json_dict(config.get("blocked_sources"))
        blocked[source.id] = exclusion
        config["blocked_sources"] = blocked
    campaign.config = config


async def _archive_source_after_critical_error(
    *,
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    run: CampaignRun | None,
    source: TelegramSource,
    account: TelegramAccount | None,
    error_code: str,
    error_message: str,
    now: datetime,
) -> None:
    if account and error_code in {
        "SOURCE_ACCESS_DENIED",
        "SOURCE_READ_DENIED",
        "JOIN_APPROVAL_PENDING",
        "ChannelPrivateError",
        "ChatWriteForbiddenError",
        "ChatAdminRequiredError",
        "ChatGuestSendForbiddenError",
    }:
        _exclude_comment_account_source_pair(campaign, source, account, now, error_code)
        return
    archive_reason = _source_archive_reason(error_code, error_message)
    archived_payload = {
        "source_id": source.id,
        "title": source.title,
        "username": source.username,
        "account_id": account.id if account else None,
        "error_code": error_code,
        "error_message": error_message,
        "blocked_at": _iso(now),
        "archived_at": _iso(now),
        "reason": archive_reason,
    }
    await blacklist_source(
        session,
        workspace_id=workspace_id,
        source=source,
        reason=archive_reason,
        error_code=error_code,
        error_message=error_message,
        campaign_id=campaign.id,
        account_id=account.id if account else None,
        blocked_by="system",
        now=now,
        details=archived_payload,
    )

    campaigns_result = await session.execute(
        select(Campaign).where(
            Campaign.workspace_id == workspace_id,
            Campaign.status != "deleted",
        )
    )
    for item in campaigns_result.scalars().all():
        config = {**_json_dict(item.config)}
        source_ids = [str(value) for value in config.get("source_ids", []) if isinstance(value, str)]
        changed = False
        if source.id in source_ids:
            config["source_ids"] = [source_id for source_id in source_ids if source_id != source.id]
            changed = True

        blocked_sources = {**_json_dict(config.get("blocked_sources"))}
        existing = _json_dict(blocked_sources.get(source.id))
        blocked_sources[source.id] = {**existing, **archived_payload}
        item.config = {**config, "blocked_sources": blocked_sources}

        if item.id == campaign.id or changed:
            item.updated_at = now

    await session.execute(delete(CampaignTarget).where(CampaignTarget.source_id == source.id))

    source.access_status = "blacklisted"
    source.comments_enabled = False
    source.source_metadata = {
        **_json_dict(source.source_metadata),
        "blacklisted": True,
        "blacklist_reason": archive_reason,
        "blacklisted_after_critical_error": archived_payload,
        "blocked_in_campaign": {
            "campaign_id": campaign.id,
            "account_id": account.id if account else None,
            "error_code": error_code,
            "error_message": error_message,
            "blocked_at": _iso(now),
        },
    }
    source.updated_at = now

    if run:
        _add_run_event(
            session,
            run,
            "error",
            "source_blacklisted_after_critical_error",
            f"Источник «{_source_log_name(source)}» добавлен в черный список и удален из активных кампаний: {error_message}",
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_username": source.username,
                "account_id": account.id if account else None,
                "error_code": error_code,
                "archive_reason": archive_reason,
            },
        )
    await record_operational_event(
        session,
        workspace_id=workspace_id,
        severity="warning",
        category="telegram",
        event_type="source_blacklisted_after_critical_error",
        title=f"Источник в черном списке: {_source_log_name(source)}",
        message=f"Источник добавлен в черный список и удален из активных кампаний, кампания продолжает работу. Причина: {error_message}",
        source="campaigns.engine",
        entity_type="telegram_source",
        entity_id=source.id,
        dedupe_key=f"source_blacklisted_after_critical_error:{source.id}:{error_code}",
        payload={
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "run_id": run.id if run else None,
            "source_id": source.id,
            "source_title": source.title,
            "source_username": source.username,
            "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
            "account_id": account.id if account else None,
            "account_name": _account_log_name(account),
            "error_code": error_code,
            "archive_reason": archive_reason,
        },
        alert=True,
    )


def _source_archive_reason(error_code: str, error_message: str) -> str:
    error_text = f"{error_code} {error_message}".lower()
    if "comments_closed" in error_text or "нет подключенного обсуждения" in error_text:
        return "source_comments_not_available"
    if "inviterequestsent" in error_text:
        return "discussion_join_approval_required"
    if any(marker in error_text for marker in ["userbannedinchannel", "banned from it"]):
        return "account_banned_in_source"
    if any(
        marker in error_text
        for marker in [
            "chatwriteforbidden",
            "comment_send_forbidden",
            "write forbidden",
            "lack permission",
            "not return a sent comment",
        ]
    ):
        return "account_cannot_write_comments"
    if any(
        marker in error_text
        for marker in ["channelprivate", "private", "invitehash", "usernameinvalid", "usernotparticipant"]
    ):
        return "source_not_accessible"
    return "critical_source_error"


async def _fail_run(
    session: AsyncSession,
    campaign: Campaign,
    run: CampaignRun,
    error_code: str,
    error_message: str,
    *,
    alert: bool = True,
) -> CampaignRun:
    now = datetime.now(UTC)
    run.status = "failed"
    run.finished_at = now
    run.heartbeat_at = now
    run.error_code = error_code[:120]
    run.error_message = error_message
    campaign.status = "failed"
    run.report = {
        **_json_dict(run.report),
        "status": "failed",
        "finished_at": _iso(now),
        "error": {"code": run.error_code, "message": error_message},
    }
    _add_run_event(
        session,
        run,
        "error",
        "run_failed",
        "Campaign run failed during one bounded tick.",
        {"error_code": run.error_code, "error_message": error_message},
    )
    await record_operational_event(
        session,
        workspace_id=run.workspace_id,
        severity="critical",
        category="campaigns",
        event_type="campaign_run_failed",
        title=f"Кампания остановилась: {campaign.name}",
        message=_friendly_run_failure_message(error_code, error_message),
        source="campaigns.engine",
        entity_type="campaign_run",
        entity_id=run.id,
        dedupe_key=f"campaign_run_failed:{run.id}:{run.error_code}",
        payload={
            "campaign_id": campaign.id,
            "run_id": run.id,
            "campaign_name": campaign.name,
            "error_code": run.error_code,
        },
        alert=alert,
    )
    await session.commit()
    await session.refresh(run)
    return run


def _is_joinable_comment_error(exc: TelegramGatewayError) -> bool:
    error_text = f"{exc.code} {exc}".lower()
    return any(
        marker in error_text
        for marker in [
            "source comments are not enabled",
            "comment discussion chat is not available",
            "chatwriteforbidden",
            "usernotparticipant",
            "not participant",
        ]
    )


def _friendly_run_failure_message(error_code: str, error_message: str) -> str:
    if "Connection to Telegram failed" in error_message or error_code == "ConnectionError":
        return "Не удалось подключиться к Telegram. Обычно это сеть, прокси или сессия Telegram-аккаунта."
    if "AI_PROVIDER" in error_code or "OpenAI" in error_message:
        return "AI-провайдер не ответил. Проверь токен/прокси OpenAI и фоновые worker-процессы."
    return error_message or "Кампания остановилась из-за ошибки фонового запуска."


def _friendly_stop_reason(reason: str) -> str:
    if reason == "daily_limit":
        return "Запуск остановлен безопасно: аккаунты достигли дневного лимита комментариев или сейчас не подходят для отправки."
    if reason == "config_updated":
        return "Запуск остановлен технически: настройки кампании изменились, после сохранения должен стартовать новый запуск."
    if reason == "campaign_archived":
        return "Запуск остановлен, потому что кампания отправлена в архив."
    if reason == "campaign_deleted":
        return "Запуск остановлен, потому что кампания удалена."
    return "Запуск остановлен вручную через API или кнопку «Стоп»."


async def _complete_run(
    session: AsyncSession,
    campaign: Campaign,
    run: CampaignRun,
    stop_reason: str,
    now: datetime,
) -> CampaignRun:
    status = "cancelled" if stop_reason == "manual_stop" else "completed"
    progress = {**_json_dict(run.progress), "stop_reason": stop_reason}
    run.status = status
    run.finished_at = now
    run.heartbeat_at = now
    run.progress = progress
    campaign.status = status
    run.report = _build_report(
        campaign,
        status,
        progress,
        _json_dict(run.stop_conditions),
        run.started_at or now,
        now,
        now,
        stop_reason,
    )
    _add_run_event(
        session,
        run,
        "info",
        "run_cancelled" if status == "cancelled" else "run_completed",
        "Campaign run reached a terminal stop condition.",
        {"stop_reason": stop_reason},
    )
    await session.commit()
    await session.refresh(run)
    return run


def _add_run_event(
    session: AsyncSession,
    run: CampaignRun,
    level: str,
    event_type: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        CampaignEvent(
            run_id=run.id,
            campaign_id=run.campaign_id,
            level=level,
            event_type=event_type,
            message=message,
            payload=payload,
        )
    )


def _post_selection_mode_from_config(config: dict[str, Any]) -> str:
    value = config.get("post_selection_mode")
    if value in {"new_posts", "historical_posts", "latest_post"}:
        return str(value)
    legacy_mode = config.get("mode", "comment_new_posts")
    if legacy_mode == "comment_new_posts":
        return "new_posts"
    return "historical_posts"


def _selection_mode_from_config(config: dict[str, Any]) -> str:
    post_selection_mode = _post_selection_mode_from_config(config)
    if post_selection_mode == "new_posts":
        return "new"
    if post_selection_mode == "latest_post":
        return "latest"
    return "recent"


def _delivery_mode_from_config(config: dict[str, Any]) -> str:
    value = config.get("delivery_mode")
    if value in {"draft_only", "generate_and_send"}:
        return str(value)
    return "generate_and_send" if config.get("mode") == "generate_and_send" else "draft_only"


def live_comment_delay_seconds(policy: dict[str, Any]) -> int:
    min_delay = _policy_delay_seconds(policy)
    try:
        max_delay = int(policy.get("delay_seconds_max", min_delay) or min_delay)
    except (TypeError, ValueError):
        max_delay = min_delay
    if max_delay < min_delay:
        max_delay = min_delay
    min_delay, max_delay, _snapshot = apply_behavior_delay_range(policy, (), min_delay, max_delay)
    return random.randint(min_delay, max_delay) if max_delay > min_delay else min_delay


def _has_meaningful_comment_progress(progress: dict[str, Any]) -> bool:
    return any(
        int(progress.get(key, 0) or 0) > 0
        for key in [
            "new_posts_found",
            "posts_skipped_by_interval",
            "comments_generated",
            "comments_sent",
            "comments_drafted",
            "comments_failed",
            "actions_created",
            "sources_skipped",
        ]
    )


def _configured_latest_limit(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("latest_posts_per_source", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, 500))


def _effective_latest_limit(config: dict[str, Any], selection_mode: str) -> int:
    configured = _configured_latest_limit(config)
    if selection_mode == "latest":
        return 1
    if configured > 0:
        return configured
    if selection_mode == "new":
        # 0 means no historical backfill. We still read a small live window to detect fresh posts.
        return 5
    return 1


def _max_comments(stop_conditions: dict[str, Any]) -> int | None:
    value = stop_conditions.get("max_comments", stop_conditions.get("max_reactions"))
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _remaining_max_comments(stop_conditions: dict[str, Any], progress: dict[str, Any]) -> int | None:
    limit = _max_comments(stop_conditions)
    if limit is None:
        return None
    completed = int(progress.get("successful_actions", 0) or 0)
    return max(0, limit - completed)


def _run_should_finish(campaign: Campaign, summary: CommentTickSummary) -> bool:
    if summary.stop_reason == "max_comments":
        return True
    return _selection_mode_from_config(_json_dict(campaign.config)) != "new"


def _comment_run_waiting_reason(campaign: Campaign, stop_reason: str | None) -> str | None:
    if campaign.type != "comment_posts" or stop_reason != "daily_limit":
        return None
    config = _json_dict(campaign.config)
    if _selection_mode_from_config(config) != "new":
        return None
    if config.get("continuous", True) is False:
        return None
    return "daily_limit"


def _terminal_stop_reason(
    run: CampaignRun,
    progress: dict[str, Any],
    stop_conditions: dict[str, Any],
    now: datetime,
    summary: CommentTickSummary | ChatTickSummary | ReactionTickSummary | None = None,
) -> str | None:
    if run.requested_stop_at or run.status == "stopping":
        return "manual_stop"
    if summary and summary.stop_reason:
        return summary.stop_reason
    max_comments = _max_comments(stop_conditions)
    if max_comments is not None and int(progress.get("successful_actions", 0) or 0) >= max_comments:
        return "max_comments"
    if _stop_at_reached(stop_conditions, now):
        return "stop_at_reached"
    if _run_for_minutes_reached(run, stop_conditions, now):
        return "run_for_minutes"
    return None


def _pending_reason(stop_conditions: dict[str, Any], now: datetime) -> str | None:
    if _outside_active_window(stop_conditions.get("active_window"), now):
        return "outside_active_window"
    return None


def _stop_at_reached(stop_conditions: dict[str, Any], now: datetime) -> bool:
    stop_at = _parse_datetime(stop_conditions.get("stop_at"))
    return bool(stop_at and now >= stop_at)


def _run_for_minutes_reached(run: CampaignRun, stop_conditions: dict[str, Any], now: datetime) -> bool:
    value = stop_conditions.get("run_for_minutes")
    if value is None or not run.started_at:
        return False
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return False
    return now >= run.started_at + timedelta(minutes=max(1, minutes))


def _outside_active_window(active_window: Any, now: datetime) -> bool:
    if not isinstance(active_window, dict):
        return False
    start = _parse_hhmm(active_window.get("start"))
    end = _parse_hhmm(active_window.get("end"))
    if not start or not end:
        return False
    current = now.time().replace(tzinfo=None, second=0, microsecond=0)
    if start <= end:
        return not (start <= current <= end)
    return not (current >= start or current <= end)


def _parse_hhmm(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError):
        return None


def _message_after_cursor(message: TelegramMessage, cursor: dict[str, Any]) -> bool:
    cursor_at = _parse_datetime(cursor.get("last_seen_at"))
    message_at = _message_datetime(message)
    if cursor_at and message_at:
        return message_at > cursor_at
    cursor_tg_message_id = cursor.get("last_seen_tg_message_id")
    return bool(cursor_tg_message_id and message.tg_message_id != cursor_tg_message_id)


def _cursor_from_message(message: TelegramMessage) -> dict[str, str | None]:
    message_at = _message_datetime(message)
    return {
        "last_seen_message_id": message.id,
        "last_seen_tg_message_id": message.tg_message_id,
        "last_seen_at": _iso(message_at),
    }


def _cursor_from_time(value: datetime) -> dict[str, str | None]:
    return {
        "last_seen_message_id": None,
        "last_seen_tg_message_id": None,
        "last_seen_at": _iso(value),
        "initialized_at": _iso(value),
    }


def _message_sort_key(message: TelegramMessage) -> datetime:
    return _message_datetime(message) or datetime.min.replace(tzinfo=UTC)


def _messages_after_polling_cutover(messages: list[TelegramMessage], config: dict) -> list[TelegramMessage]:
    cutover = _parse_datetime(config.get("polling_cutover_at"))
    if cutover is None:
        return messages
    return [message for message in messages if _message_sort_key(message) > cutover]


def _message_ready_after_delay(message: TelegramMessage, now: datetime, delay_seconds: int) -> bool:
    message_at = _message_datetime(message)
    if not message_at:
        return True
    return message_at <= now - timedelta(seconds=delay_seconds)


def _should_ensure_source_membership(
    source: TelegramSource,
    account: TelegramAccount | None,
    auto_join_sources: bool,
    auto_join_attempts: int,
    auto_join_max_per_tick: int,
    auto_join_wait_until: datetime | None,
    now: datetime,
    *,
    daily_attempts: int = 0,
    daily_limit: int | None = None,
    join_discussion: bool = False,
) -> bool:
    if not auto_join_sources or auto_join_max_per_tick <= 0:
        return False
    if daily_limit is not None and daily_attempts >= daily_limit:
        return False
    if auto_join_wait_until and now < auto_join_wait_until:
        return False
    if auto_join_attempts >= auto_join_max_per_tick:
        return False

    metadata = _json_dict(source.source_metadata)
    account_access = _json_dict(_json_dict(metadata.get("account_access")).get(account.id if account else ""))
    if account_access.get("access_checked") and (
        account_access.get("joined") or account_access.get("already_participant")
    ):
        if (
            join_discussion
            and source.comments_enabled
            and not (account_access.get("discussion_joined") or account_access.get("discussion_already_participant"))
        ):
            return True
        return False
    if (
        not account
        and not account_access
        and metadata.get("access_checked")
        and (metadata.get("joined") or metadata.get("already_participant"))
    ):
        if (
            join_discussion
            and source.comments_enabled
            and not (metadata.get("discussion_joined") or metadata.get("discussion_already_participant"))
        ):
            return True
        return False
    return True


def _account_auto_join_wait_until(account: TelegramAccount | None) -> datetime | None:
    if not account:
        return None
    return _parse_datetime(_json_dict(account.daily_limits).get("auto_join_wait_until"))


def _account_source_fetch_wait_until(account: TelegramAccount | None) -> datetime | None:
    if not account:
        return None
    return _parse_datetime(_json_dict(account.daily_limits).get("source_fetch_wait_until"))


def _account_auto_join_daily_attempts(account: TelegramAccount | None, now: datetime) -> int:
    if not account:
        return 0
    data = _json_dict(account.daily_limits)
    return len(_account_attempt_times(data, "auto_join_attempts_24h", now))


def _account_attempt_times(data: dict[str, Any], key: str, now: datetime) -> list[datetime]:
    values = data.get(key)
    if not isinstance(values, list):
        values = []
        if key == "auto_join_attempts_24h":
            # Keep legacy aggregate attempts until their last possible receipt ages out.
            last = _parse_datetime(data.get("last_auto_join_attempt_at"))
            if last is None:
                day = _parse_datetime(data.get("auto_join_daily_date"))
                last = day + timedelta(days=1) if day else None
            if last:
                values = [_iso(last)] * max(0, _safe_int(data.get("auto_join_daily_attempts")))
    return [at for value in values if (at := _parse_datetime(value)) and at > now - timedelta(hours=24)]


def _record_account_attempt(account: TelegramAccount, key: str, now: datetime) -> None:
    data = dict(_json_dict(account.daily_limits))
    data[key] = [_iso(at) for at in _account_attempt_times(data, key, now)] + [_iso(now)]
    account.daily_limits = data


def _record_auto_join_attempt(
    campaign: Campaign,
    account: TelegramAccount,
    now: datetime,
    interval_seconds: int,
) -> None:
    _record_account_attempt(account, "auto_join_attempts_24h", now)
    wait_until = max(_account_auto_join_wait_until(account) or now, now + timedelta(seconds=interval_seconds))
    account.daily_limits = {
        **_json_dict(account.daily_limits),
        "auto_join_wait_until": _iso(wait_until),
        "last_auto_join_attempt_at": _iso(now),
        "auto_join_daily_date": now.date().isoformat(),
        "auto_join_daily_attempts": _account_auto_join_daily_attempts(account, now),
    }
    campaign.config = {
        **_json_dict(campaign.config),
        "auto_join_wait_until": _iso(wait_until),
        "last_auto_join_attempt_at": _iso(now),
    }


def _policy_auto_join_daily_limit(policy: dict[str, Any]) -> int:
    value = policy.get("auto_join_daily_limit", policy.get("auto_join_max_per_day", 6))
    try:
        return max(0, min(int(value), 5000))
    except (TypeError, ValueError):
        return 6


def _policy_auto_join_interval_seconds(policy: dict[str, Any]) -> int:
    value = policy.get("auto_join_interval_seconds", 1800)
    try:
        return max(1800, min(int(value), 21600))
    except (TypeError, ValueError):
        return 1800


def _is_flood_wait_error(exc: TelegramGatewayError) -> bool:
    return exc.code in {"FloodWaitError", "FLOOD_WAIT"} or "wait of" in str(exc).lower()


def _is_account_setup_error(exc: TelegramGatewayError) -> bool:
    return exc.code in {
        "SESSION_NOT_FOUND",
        "SESSION_EMPTY",
        "SESSION_INVALID",
        "SESSION_UNAUTHORIZED",
        "TELEGRAM_API_NOT_CONFIGURED",
        "PROXY_NOT_FOUND",
    }


def _should_alert_source_fetch_error(
    exc: TelegramGatewayError,
    *,
    ensure_membership: bool,
) -> bool:
    if _is_flood_wait_error(exc):
        return False
    if _is_account_setup_error(exc):
        return False
    if ensure_membership and exc.code in {"InviteRequestSentError", "USER_ALREADY_PARTICIPANT"}:
        return False
    return True


def _friendly_gateway_error(
    exc: TelegramGatewayError,
    *,
    source: TelegramSource | None = None,
    account: TelegramAccount | None = None,
    action: str = "action",
) -> str:
    source_name = _source_log_name(source)
    account_name = _account_log_name(account)
    if _is_frozen_account_error(exc):
        return (
            f"{account_name}: Telegram заморозил аккаунт при обработке «{source_name}». "
            "Аккаунт отключен от кампаний; проверьте его вручную в официальном Telegram."
        )
    if _is_flood_wait_error(exc):
        wait_seconds = _telegram_gateway_flood_wait_seconds(exc)
        if wait_seconds is None:
            return f"{account_name}: Telegram запросил паузу неизвестной длительности. Аккаунт в карантине до ручной проверки."
        return f"{account_name}: Telegram включил лимит частых действий на {wait_seconds} сек. Подписки и отправка продолжатся после паузы."

    error_text = f"{exc.code} {exc}".lower()
    if exc.code == "SESSION_NOT_FOUND":
        return f"{account_name}: аккаунт не подключен к Telegram. В базе нет сохраненной сессии, поэтому кампания не может читать «{source_name}». Перелогиньте или замените аккаунт."
    if exc.code == "TELEGRAM_API_NOT_CONFIGURED":
        return "Telegram API не настроен на сервере. Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH, иначе real-режим не сможет читать источники и публиковать."
    if any(
        marker in error_text
        for marker in ["session_unauthorized", "session_invalid", "session_empty", "auth key", "unauthorized"]
    ):
        return f"{account_name}: сессия Telegram недействительна. Перелогиньте аккаунт, иначе кампания не сможет читать каналы и писать комментарии."
    if any(marker in error_text for marker in ["spam", "peerflood", "user restricted", "restricted", "privacy"]):
        return f"{account_name}: Telegram ограничил аккаунт для сообщений/комментариев. Замените аккаунт или дождитесь снятия ограничения."
    if "inviterequestsent" in error_text:
        return f"{account_name}: Telegram отправил заявку на вступление в приватное обсуждение «{source_name}», но без одобрения админом аккаунт не может писать комментарии. Источник пропущен до принятия заявки или выбора другого аккаунта."
    if any(marker in error_text for marker in ["msgidinvalid", "message id invalid"]):
        return f"{account_name}: Telegram не открыл комментарии к конкретному посту в «{source_name}». Обычно это значит, что у поста нет доступного обсуждения, комментарии закрыты для этого поста или Telegram не связал пост с discussion-thread. Пост пропущен, кампания продолжит новые посты."
    if any(
        marker in error_text
        for marker in ["channelprivate", "private", "invitehash", "usernameinvalid", "usernotparticipant"]
    ):
        return f"{account_name}: нет доступа к источнику «{source_name}». Канал приватный, ссылка недоступна или аккаунт не состоит в канале."
    if any(marker in error_text for marker in ["userbannedinchannel", "banned from it"]):
        return (
            f"{account_name}: Telegram отказал в отправке комментария в «{source_name}». "
            "Это может быть запрет в обсуждении или ограничение самого аккаунта; "
            "аккаунт поставлен на паузу до проверки."
        )
    if any(
        marker in error_text
        for marker in [
            "chatwriteforbidden",
            "comment_send_forbidden",
            "write forbidden",
            "lack permission",
            "not return a sent comment",
        ]
    ):
        if action == "comment":
            return f"{account_name}: не может отправить комментарий в «{source_name}». Нет права писать в обсуждении, аккаунт не вступил в чат обсуждений или Telegram отклонил комментарий для этого аккаунта."
        return f"{account_name}: не может подготовить источник «{source_name}». Нет доступа к каналу или обсуждению, поэтому комментарии по нему отключены в этой кампании."
    if any(marker in error_text for marker in ["proxy", "timeout", "connection", "network"]):
        return f"{account_name}: техническая ошибка соединения с Telegram при обработке «{source_name}». Проверьте прокси/сеть аккаунта и повторите запуск."
    if exc.code == "TELEGRAM_CONNECTION_FAILED":
        return f"{account_name}: не удалось подключиться к Telegram при обработке «{source_name}». Обычно это временный сбой прокси или сети аккаунта; источник пропущен, кампания продолжит следующий тик."

    raw_message = str(exc).strip() or exc.code
    return f"{account_name}: Telegram вернул ошибку при обработке «{source_name}»: {raw_message}."


def _source_log_name(source: TelegramSource | None) -> str:
    if not source:
        return "источник"
    return source.title or source.username or source.url or source.tg_id or source.id


def _account_log_name(account: TelegramAccount | None) -> str:
    if not account:
        return "Аккаунт не выбран"
    return account.display_name or account.username or account.id


def _policy_auto_join_max_per_tick(policy: dict[str, Any]) -> int:
    value = policy.get("auto_join_max_per_tick", 1)
    try:
        return max(0, min(int(value), 1))
    except (TypeError, ValueError):
        return 1


def _comment_daily_limit(policy: dict[str, Any]) -> int:
    value = policy.get("daily_limit_per_account", 10)
    try:
        return max(1, min(int(value), COMMENT_DAILY_LIMIT_HARD_CAP))
    except (TypeError, ValueError):
        return 10


def _comment_post_interval(policy: dict[str, Any]) -> int:
    value = policy.get("comment_post_interval", 1)
    try:
        return max(1, min(int(value), COMMENT_POST_INTERVAL_MAX))
    except (TypeError, ValueError):
        return 1


def _comment_post_interval_mode(policy: dict[str, Any]) -> str:
    return "random_range" if policy.get("comment_post_interval_mode") == "random_range" else "fixed"


def _comment_post_interval_range(policy: dict[str, Any]) -> tuple[int, int]:
    min_value = _bounded_interval_value(policy.get("comment_post_interval_min", policy.get("comment_post_interval", 1)))
    max_value = _bounded_interval_value(policy.get("comment_post_interval_max", min_value))
    if min_value > max_value:
        min_value, max_value = max_value, min_value
    return min_value, max_value


def _comment_post_interval_active(policy: dict[str, Any]) -> bool:
    if _comment_post_interval_mode(policy) == "random_range":
        min_value, max_value = _comment_post_interval_range(policy)
        return min_value > 1 or max_value > 1
    return _comment_post_interval(policy) > 1


def _comment_interval_state(config_state: dict[str, Any]) -> tuple[dict[str, int], dict[str, int], bool]:
    if config_state.get("comment_post_interval_state_version") != COMMENT_POST_INTERVAL_STATE_VERSION:
        has_legacy_state = bool(
            config_state.get("comment_post_interval_counters") or config_state.get("comment_post_interval_targets")
        )
        return {}, {}, has_legacy_state
    return (
        {
            key: _safe_int(value)
            for key, value in _json_dict(config_state.get("comment_post_interval_counters")).items()
        },
        {key: _safe_int(value) for key, value in _json_dict(config_state.get("comment_post_interval_targets")).items()},
        False,
    )


def _comment_post_interval_label(policy: dict[str, Any]) -> str:
    if _comment_post_interval_mode(policy) == "random_range":
        min_value, max_value = _comment_post_interval_range(policy)
        return f"комментировать случайно каждый {min_value}-{max_value}-й пост"
    return f"комментировать каждый {_comment_post_interval(policy)}-й пост"


def _comment_stop_words_enabled(policy: dict[str, Any]) -> bool:
    if "stop_words_enabled" in policy:
        return policy.get("stop_words_enabled") is not False
    return "stop_words" in policy


def _comment_stop_words(policy: dict[str, Any]) -> list[str]:
    if not _comment_stop_words_enabled(policy):
        return []
    raw = policy.get("stop_words", DEFAULT_COMMENT_STOP_WORDS)
    if isinstance(raw, str):
        items = re.split(r"[,;\n]+", raw)
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = list(DEFAULT_COMMENT_STOP_WORDS)

    words: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_stop_word(item)
        if normalized and normalized not in seen:
            words.append(normalized)
            seen.add(normalized)
    return words


def _normalize_stop_word(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _message_stop_word_match(message: TelegramMessage, policy: dict[str, Any]) -> str | None:
    text = _normalize_stop_word(message.text or "")
    if not text:
        return None
    for word in _comment_stop_words(policy):
        if re.search(rf"(?<![0-9a-zа-яе]){re.escape(word)}(?![0-9a-zа-яе])", text):
            return word
    return None


def _without_stop_word_messages(messages: list[TelegramMessage], policy: dict[str, Any]) -> list[TelegramMessage]:
    if not _comment_stop_words_enabled(policy) or not messages:
        return messages
    return [message for message in messages if not _message_stop_word_match(message, policy)]


def _comment_min_post_chars(policy: dict[str, Any]) -> int:
    try:
        return max(0, min(int(policy.get("min_post_chars", DEFAULT_COMMENT_MIN_POST_CHARS)), 5000))
    except (TypeError, ValueError):
        return DEFAULT_COMMENT_MIN_POST_CHARS


def _comment_min_media_post_chars(policy: dict[str, Any]) -> int:
    try:
        return max(0, min(int(policy.get("min_media_post_chars", DEFAULT_COMMENT_MIN_MEDIA_POST_CHARS)), 1000))
    except (TypeError, ValueError):
        return DEFAULT_COMMENT_MIN_MEDIA_POST_CHARS


def _message_text_chars(message: TelegramMessage) -> int:
    return len(str(message.text or "").strip())


def _message_has_media_context(message: TelegramMessage) -> bool:
    raw = message.raw if isinstance(message.raw, dict) else {}
    if raw.get("has_media") is True:
        return True
    if raw.get("grouped_id") or raw.get("album_id"):
        return True
    media_type = raw.get("media_type")
    if isinstance(media_type, str) and media_type.strip():
        return True
    for key in ("media", "photo", "video", "document", "webpage"):
        if raw.get(key):
            return True
    return False


def _comment_min_chars_for_message(message: TelegramMessage, policy: dict[str, Any]) -> int:
    if policy.get("allow_short_media_posts", True) is not False and _message_has_media_context(message):
        return _comment_min_media_post_chars(policy)
    return _comment_min_post_chars(policy)


def _message_passes_post_length(message: TelegramMessage, policy: dict[str, Any]) -> bool:
    min_chars = _comment_min_chars_for_message(message, policy)
    if min_chars <= 0:
        return True
    return _message_text_chars(message) >= min_chars


def _without_short_post_messages(messages: list[TelegramMessage], policy: dict[str, Any]) -> list[TelegramMessage]:
    if not messages:
        return messages
    return [message for message in messages if _message_passes_post_length(message, policy)]


def _bounded_interval_value(value: Any) -> int:
    try:
        return max(1, min(int(value), COMMENT_POST_INTERVAL_MAX))
    except (TypeError, ValueError):
        return 1


def _next_comment_fixed_interval_decision(
    counters: dict[str, Any],
    source_id: str,
    interval: int,
) -> tuple[bool, dict[str, int]]:
    next_counters = {key: _safe_int(value) for key, value in counters.items()}
    if interval <= 1:
        next_counters[source_id] = 0
        return True, next_counters
    if source_id not in next_counters:
        next_counters[source_id] = 0
        return True, next_counters
    skipped_since_comment = next_counters.get(source_id, 0)
    if skipped_since_comment >= interval - 1:
        next_counters[source_id] = 0
        return True, next_counters
    next_counters[source_id] = skipped_since_comment + 1
    return False, next_counters


def _next_comment_post_decision(
    *,
    policy: dict[str, Any],
    counters: dict[str, Any],
    targets: dict[str, Any],
    source_id: str,
) -> tuple[bool, dict[str, int], dict[str, int]]:
    if _comment_post_interval_mode(policy) != "random_range":
        should_comment, next_counters = _next_comment_fixed_interval_decision(
            counters,
            source_id,
            _comment_post_interval(policy),
        )
        return should_comment, next_counters, {key: _safe_int(value) for key, value in targets.items()}

    min_value, max_value = _comment_post_interval_range(policy)
    next_counters = {key: _safe_int(value) for key, value in counters.items()}
    next_targets = {key: _safe_int(value) for key, value in targets.items()}
    target = next_targets.get(source_id) or random.randint(min_value, max_value)
    target = max(min_value, min(target, max_value))
    if source_id not in next_counters:
        next_counters[source_id] = 0
        next_targets[source_id] = target
        return True, next_counters, next_targets

    skipped_since_comment = next_counters.get(source_id, 0)
    if skipped_since_comment >= target - 1:
        next_counters[source_id] = 0
        next_targets[source_id] = random.randint(min_value, max_value)
        return True, next_counters, next_targets

    next_counters[source_id] = skipped_since_comment + 1
    next_targets[source_id] = target
    return False, next_counters, next_targets


def _apply_comment_post_interval(
    messages: list[TelegramMessage],
    counters: dict[str, Any],
    targets: dict[str, Any],
    source_id: str,
    policy: dict[str, Any],
) -> tuple[list[TelegramMessage], int, dict[str, int], dict[str, int]]:
    if not _comment_post_interval_active(policy) or not messages:
        return (
            messages,
            0,
            {key: _safe_int(value) for key, value in counters.items()},
            {key: _safe_int(value) for key, value in targets.items()},
        )

    selected: list[TelegramMessage] = []
    skipped = 0
    next_counters: dict[str, int] = {key: _safe_int(value) for key, value in counters.items()}
    next_targets: dict[str, int] = {key: _safe_int(value) for key, value in targets.items()}
    for message in messages:
        should_comment, next_counters, next_targets = _next_comment_post_decision(
            policy=policy,
            counters=next_counters,
            targets=next_targets,
            source_id=source_id,
        )
        if should_comment:
            selected.append(message)
        else:
            skipped += 1
    return selected, skipped, next_counters, next_targets


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError):
        return max(0, default)


def _max_reactions(config: dict[str, Any]) -> int | None:
    stop_conditions = _json_dict(config.get("stop_conditions"))
    value = stop_conditions.get("max_reactions", stop_conditions.get("max_comments"))
    if value is None:
        return None


def _reaction_daily_limit(policy: dict[str, Any]) -> int | None:
    value = policy.get("daily_limit_per_account")
    if "daily_limit_per_account" not in policy or value is None or value == "":
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


async def _daily_reactions_by_account(
    session: AsyncSession,
    account_ids: list[str],
) -> dict[str, int]:
    if not account_ids:
        return {}
    now = datetime.now(UTC)
    day_start = now - timedelta(hours=24)
    result = await session.execute(
        select(CampaignAction.telegram_account_id, func.count(CampaignAction.id))
        .where(
            CampaignAction.telegram_account_id.in_(account_ids),
            CampaignAction.action_type == "set_reaction",
            CampaignAction.status.in_(["sent", "succeeded"]),
            CampaignAction.executed_at >= day_start,
        )
        .group_by(CampaignAction.telegram_account_id)
    )
    return {str(account_id): int(count) for account_id, count in result.all() if account_id}


async def _sent_comments_today_by_account(
    session: AsyncSession,
    workspace_id: str,
    account_ids: list[str],
    now: datetime,
) -> dict[str, int]:
    if not account_ids:
        return {}
    day_start = now - timedelta(hours=24)
    result = await session.execute(
        select(TelegramComment.telegram_account_id, func.count(TelegramComment.id))
        .where(
            TelegramComment.workspace_id == workspace_id,
            TelegramComment.telegram_account_id.in_(account_ids),
            TelegramComment.status == "sent",
            TelegramComment.sent_at >= day_start,
        )
        .group_by(TelegramComment.telegram_account_id)
    )
    return {str(account_id): int(count) for account_id, count in result.all() if account_id}


def _next_comment_account(
    accounts: list[TelegramAccount],
    account_cycle,
    daily_sent_by_account: dict[str, int],
    daily_limit: int,
) -> TelegramAccount | None:
    if not accounts or not account_cycle:
        return None
    for _ in range(len(accounts)):
        account = next(account_cycle)
        if not _account_can_send_comments(account):
            continue
        account_daily_limit = _comment_daily_limit_for_account(account, daily_limit)
        if daily_sent_by_account.get(account.id, 0) >= account_daily_limit:
            continue
        return account
    return None


def _account_can_send_comments(account: TelegramAccount) -> bool:
    data = _json_dict(account.daily_limits)
    checked_at = _parse_datetime(_json_dict(data.get("restriction_status")).get("checked_at"))
    now = datetime.now(UTC)
    return bool(
        not data.get("flood_wait_unknown")
        and not account.incubator_mode
        and account.status in COMMENT_ACCOUNT_ACTIVE_STATUSES
        and (
            owner_attested(account, "neurocommenting")
            or (account.restriction_status == "clear" and checked_at and now - timedelta(hours=24) <= checked_at <= now)
        )
    )


def _comment_daily_limit_for_account(account: TelegramAccount, policy_daily_limit: int) -> int:
    del account
    return policy_daily_limit


def _apply_gateway_error_account_risk(
    session: AsyncSession,
    account: TelegramAccount | None,
    exc: TelegramGatewayError,
    now: datetime,
    *,
    comment_send: bool = False,
) -> None:
    if not account:
        return
    now = datetime.now(UTC)
    data = dict(_json_dict(account.daily_limits))
    if _is_frozen_account_error(exc):
        account.status = "frozen"
        account.health_score = min(account.health_score, 10)
        data["restriction_status"] = {
            **_json_dict(data.get("restriction_status")),
            "status": "frozen",
            "label": "Telegram freeze",
            "checked_at": _iso(now),
            "raw_text": str(exc),
            "source": "campaign_runtime",
        }
        data["last_frozen_error_at"] = _iso(now)
        data["last_frozen_error_code"] = exc.code
        session.add(
            AccountHealthEvent(
                account_id=account.id,
                event_type="account_frozen_detected",
                severity="error",
                message="Telegram вернул frozen-ошибку во время кампании. Аккаунт отключен от дальнейших действий.",
                raw_error={"code": exc.code, **exc.raw},
            )
        )
    elif comment_send and exc.code == "UserBannedInChannelError":
        # Local discussion denial is not proof of a global account restriction.
        data["last_comment_write_block_at"] = _iso(now)
        session.add(
            AccountHealthEvent(
                account_id=account.id,
                event_type="comment_write_blocked",
                severity="warning",
                message="Запись в обсуждении запрещена; источник исключён, остальные продолжают работу.",
                raw_error={"code": exc.code},
            )
        )
    elif exc.code == "TELEGRAM_CONNECTION_FAILED":
        account.health_score = max(0, min(account.health_score, 60) - 10)
        data["last_telegram_connection_failed_at"] = _iso(now)
        data["telegram_connection_failed_count"] = _safe_int(data.get("telegram_connection_failed_count")) + 1
    elif _is_flood_wait_error(exc):
        account.health_score = max(0, min(account.health_score, 70) - 10)
        data["last_flood_wait_at"] = _iso(now)
        wait_seconds = _telegram_gateway_flood_wait_seconds(exc)
        data["last_flood_wait_seconds"] = wait_seconds
        if wait_seconds is None:
            if account.status not in {"frozen", "spam_blocked", "blocked"}:
                account.status = "quarantined"
            data["flood_wait_unknown"] = True
        else:
            deadline = now + timedelta(seconds=wait_seconds + 15)
            for key in ("auto_join_wait_until", "source_fetch_wait_until"):
                data[key] = _iso(max(_parse_datetime(data.get(key)) or now, deadline))
            data["last_auto_join_flood_wait_at"] = _iso(now)
            data["last_source_fetch_flood_wait_at"] = _iso(now)
    elif any(marker in f"{exc.code} {exc}".lower() for marker in ["reactionstoomany", "peerflood", "restricted"]):
        account.health_score = max(0, min(account.health_score, 60) - 15)
        data["last_action_limit_error_at"] = _iso(now)
        if "reaction" in f"{exc.code} {exc}".lower():
            data["last_reactions_too_many_at"] = _iso(now)
    account.daily_limits = data


def _is_frozen_account_error(exc: TelegramGatewayError) -> bool:
    text = f"{exc.code} {exc}".lower()
    return "frozen" in text or "frozen account" in text or "frozen accounts" in text


async def _comment_account_send_wait_reason(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount | None,
    policy: dict[str, Any],
    now: datetime,
    *,
    campaign: Campaign | None = None,
) -> str | None:
    if not account:
        return "account_required"
    # Serialize the account budget across polling and live-listener transactions.
    await session.flush()
    # PostgreSQL FK checks in the independent operation ledger need KEY SHARE.
    # NO KEY UPDATE still serializes this budget, without blocking ledger inserts.
    await session.refresh(account, with_for_update={"key_share": True})
    if not _account_can_send_comments(account):
        return "account_safety_blocked"
    now = datetime.now(UTC)
    wait_until = _account_source_fetch_wait_until(account)
    if wait_until and now < wait_until:
        return "telegram_flood_wait"
    await session.flush()
    counts = await _sent_comments_today_by_account(session, workspace_id, [account.id], now)
    attempts = _account_attempt_times(_json_dict(account.daily_limits), "comment_send_attempts_24h", now)
    legacy_sent = 0
    if attempts:
        legacy_sent = (
            await session.scalar(
                select(func.count(TelegramComment.id)).where(
                    TelegramComment.workspace_id == workspace_id,
                    TelegramComment.telegram_account_id == account.id,
                    TelegramComment.status == "sent",
                    TelegramComment.sent_at >= now - timedelta(hours=24),
                    TelegramComment.sent_at < min(attempts),
                )
            )
            or 0
        )
    inherited_attempts = []
    if campaign:
        inherited_attempts = [
            timestamp
            for value in campaign.config.get("replacement_comment_attempts", [])
            if (timestamp := _parse_datetime(value.get("at") if isinstance(value, dict) else value))
            and timestamp > now - timedelta(hours=24)
        ]
    if counts.get(account.id, 0) >= _comment_daily_limit(policy):
        return "daily_limit"
    attempt_limit = max(
        _comment_daily_limit(policy),
        _safe_int(policy.get("comment_attempt_limit_per_account"), default=_comment_daily_limit(policy)),
    )
    if legacy_sent + len(attempts) + len(inherited_attempts) >= attempt_limit:
        return "daily_attempt_limit" if "comment_attempt_limit_per_account" in policy else "daily_limit"
    interval = max(0, _safe_int(policy.get("min_send_interval_seconds"), default=0))
    if interval:
        latest = await session.scalar(
            select(func.max(TelegramComment.sent_at)).where(
                TelegramComment.workspace_id == workspace_id,
                TelegramComment.telegram_account_id == account.id,
                TelegramComment.status == "sent",
            )
        )
        from app.modules.telegram_safety.models import TelegramOperation

        # The independent transport receipt survives a caller transaction rollback.
        # An accepted send must still enforce spacing when its comment row was lost.
        receipt_at = await session.scalar(
            select(func.max(TelegramOperation.created_at)).where(
                TelegramOperation.account_id == account.id,
                TelegramOperation.method.in_(["SendMessageRequest", "SendMediaRequest"]),
                TelegramOperation.outcome == "accepted",
            )
        )
        inherited_latest = _parse_datetime(campaign.config.get("replacement_last_send_at")) if campaign else None
        timestamps = [
            value for value in (_parse_datetime(latest), _parse_datetime(receipt_at), inherited_latest) if value
        ]
        latest = max(timestamps) if timestamps else None
        if latest and now < latest + timedelta(seconds=interval):
            return "send_interval"
    return None


def _telegram_flood_wait_seconds(message: str) -> int | None:
    match = re.search(r"(?:wait of\s+|flood[ _-]*wait[\s:_]*)(\d+)\b(?!\.\d)", message or "", re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _telegram_gateway_flood_wait_seconds(exc: TelegramGatewayError) -> int | None:
    seconds = _json_dict(exc.raw).get("seconds")
    if type(seconds) is int and seconds >= 0:
        return seconds
    return _telegram_flood_wait_seconds(str(exc))


def _policy_delay_seconds(policy: dict[str, Any]) -> int:
    for key in ("delay_seconds_min", "delay_seconds"):
        value = policy.get(key)
        if isinstance(value, int | float) and value > 0:
            return int(value)
    return 0


def _message_datetime(message: TelegramMessage) -> datetime | None:
    value = message.published_at or message.created_at
    if value and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _empty_progress() -> dict[str, Any]:
    progress = {
        **CommentTickSummary().as_progress(),
        **ChatTickSummary().as_progress(),
        **ReactionTickSummary().as_progress(),
    }
    progress["ticks"] = 0
    progress["last_tick_at"] = None
    progress["last_tick"] = {}
    return progress


def _merge_progress(
    current: dict[str, Any],
    tick_progress: dict[str, Any],
    tick_finished_at: datetime,
) -> dict[str, Any]:
    merged = {**_empty_progress(), **current}
    for key in PROGRESS_COUNTER_KEYS:
        merged[key] = int(current.get(key, 0) or 0) + int(tick_progress.get(key, 0) or 0)
    merged["ticks"] = int(current.get("ticks", 0) or 0) + 1
    merged["last_tick_at"] = _iso(tick_finished_at)
    merged["last_tick"] = tick_progress
    merged["stop_reason"] = tick_progress.get("stop_reason") or current.get("stop_reason")
    return merged


def _build_report(
    campaign: Campaign,
    status: str,
    progress: dict[str, Any],
    stop_conditions: dict[str, Any],
    started_at: datetime,
    finished_at: datetime | None,
    last_tick_at: datetime | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "campaign_id": campaign.id,
        "post_selection_mode": _post_selection_mode_from_config(_json_dict(campaign.config)),
        "delivery_mode": _delivery_mode_from_config(_json_dict(campaign.config)),
        "mode": campaign.config.get("mode", "draft_only"),
        "status": status,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "last_tick_at": _iso(last_tick_at),
        "background": {
            "worker": "celery",
            "manual_tick_endpoint": "/api/v1/campaigns/runs/{run_id}/tick",
        },
        "stop_conditions": stop_conditions,
        "stop_reason": stop_reason or progress.get("stop_reason"),
        "progress": progress,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _moderation_status(text: str, blacklist_words: list[str]) -> str:
    lowered = text.lower()
    return "blocked" if any(word.lower() in lowered for word in blacklist_words) else "passed"
