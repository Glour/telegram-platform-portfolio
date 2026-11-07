import asyncio
import csv
import json
import random
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

from app.modules.telegram_gateway.service import (
    discover_channels_with_account,
    ensure_source_access_with_account,
    fetch_posts_with_account,
    source_ref_from_fields,
    telegram_client_for_account,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import RPCError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Message, User

from app.core.config import get_settings
from app.core.errors import bad_request
from app.modules.channel_map.service import atlas_channels, load_atlas
from app.modules.parsing.models import ParsedTelegramComment, ParsingJob, ParsingJobLog, ParsingResult
from app.modules.sources.models import TelegramMessage, TelegramSource, TelegramUser
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility
from app.modules.telegram_gateway.contracts import TelegramDialogSourceInfo

HEAVY_PARSING_JOB_TYPES = {
    "channel_search",
    "channel_validate",
    "group_search",
    "source_posts",
    "source_comments",
    "source_users",
    "message_users",
    "dialogs",
}
ACTIVE_JOB_STATUSES = {"queued", "running", "paused"}
STALE_ACTIVE_PARSING_JOB_AFTER = timedelta(hours=2)
STALE_AUTO_FAIL_STATUSES = {"queued", "running"}
BLOCKED_PARSING_ACCOUNT_STATUSES = {"archived", "banned", "blocked", "deleted", "frozen", "quarantined", "spam_blocked"}


async def create_parsing_job(
    session: AsyncSession,
    *,
    workspace_id: str,
    job_type: str,
    account: TelegramAccount | None = None,
    input_payload: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    progress_total: int = 0,
) -> ParsingJob:
    if account and job_type in HEAVY_PARSING_JOB_TYPES:
        await assert_parsing_account_available(session, workspace_id, account, job_type)
    job = ParsingJob(
        workspace_id=workspace_id,
        type=job_type,
        status="queued",
        account_id=account.id if account else None,
        input=_jsonable(input_payload or {}),
        filters=_jsonable(filters or {}),
        limits=_jsonable(limits or {}),
        progress_total=progress_total,
    )
    session.add(job)
    await session.flush()
    await add_parsing_job_log(session, job, "info", "Задача создана", {"type": job_type})
    await session.commit()
    await session.refresh(job)
    return job


async def start_parsing_job(session: AsyncSession, job: ParsingJob) -> None:
    now = datetime.now(UTC)
    job.status = "running"
    job.started_at = now
    await add_parsing_job_log(session, job, "info", "Задача запущена")
    await session.commit()


async def complete_parsing_job(
    session: AsyncSession,
    job: ParsingJob,
    *,
    found_count: int,
    saved_count: int | None = None,
    skipped_count: int = 0,
    progress_done: int | None = None,
) -> None:
    job.status = "completed"
    job.finished_at = datetime.now(UTC)
    job.found_count = found_count
    job.saved_count = found_count if saved_count is None else saved_count
    job.skipped_count = skipped_count
    job.progress_done = found_count if progress_done is None else progress_done
    if not job.progress_total:
        job.progress_total = job.progress_done
    await add_parsing_job_log(
        session,
        job,
        "info",
        "Задача завершена",
        {"found_count": found_count, "saved_count": job.saved_count, "skipped_count": skipped_count},
    )
    await session.commit()


async def fail_parsing_job(session: AsyncSession, job: ParsingJob, error: Exception, message: str) -> None:
    job.status = "failed"
    job.finished_at = datetime.now(UTC)
    job.error_code = type(error).__name__
    job.error_message = message
    await add_parsing_job_log(session, job, "error", message, {"error_code": job.error_code})
    await session.commit()


async def add_parsing_job_log(
    session: AsyncSession,
    job: ParsingJob,
    level: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> ParsingJobLog:
    row = ParsingJobLog(
        job_id=job.id,
        level=level,
        message=message,
        payload=payload,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def assert_parsing_account_available(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount,
    job_type: str,
) -> None:
    if account.workspace_id != workspace_id:
        raise bad_request("Аккаунт принадлежит другому workspace.")
    if account.status in BLOCKED_PARSING_ACCOUNT_STATUSES or account.restriction_status in {
        "limited",
        "blocked",
        "frozen",
    }:
        raise bad_request("Аккаунт нельзя использовать для тяжелого парсинга: статус ограничен или карантин.")
    eligibility = get_module_eligibility(account, "read_only_parsing")
    if eligibility.status == "blocked":
        raise bad_request(eligibility.message or "Аккаунт сейчас не подходит для парсинга.")
    result = await session.execute(
        select(ParsingJob)
        .where(
            ParsingJob.workspace_id == workspace_id,
            ParsingJob.account_id == account.id,
            ParsingJob.type.in_(HEAVY_PARSING_JOB_TYPES),
            ParsingJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(ParsingJob.created_at.asc())
    )
    active_jobs = list(result.scalars().all())
    now = datetime.now(UTC)
    stale_cutoff = now - STALE_ACTIVE_PARSING_JOB_AFTER
    blocking_jobs: list[ParsingJob] = []
    for active_job in active_jobs:
        last_seen_at = active_job.updated_at or active_job.started_at or active_job.created_at
        last_seen_at = _ensure_utc(last_seen_at)
        if active_job.status in STALE_AUTO_FAIL_STATUSES and last_seen_at < stale_cutoff:
            active_job.status = "failed"
            active_job.finished_at = now
            active_job.error_code = "StaleParsingJob"
            active_job.error_message = "Задача парсинга была автоматически снята как зависшая."
            await add_parsing_job_log(
                session,
                active_job,
                "warning",
                active_job.error_message,
                {"stale_after_seconds": int(STALE_ACTIVE_PARSING_JOB_AFTER.total_seconds())},
            )
            continue
        blocking_jobs.append(active_job)
    if active_jobs:
        await session.commit()
    if blocking_jobs:
        active_job = blocking_jobs[0]
        raise bad_request(
            "Этот аккаунт уже выполняет задачу парсинга "
            f"({active_job.type}, {active_job.id[:8]}). Дождитесь завершения или отмените задачу."
        )


async def persist_parsing_results(
    session: AsyncSession,
    *,
    job: ParsingJob,
    entity_type: str,
    items: list[dict[str, Any]],
    source_id: str | None = None,
) -> int:
    saved = 0
    for item in items:
        entity_ref = _result_entity_ref(entity_type, item)
        if entity_ref:
            existing = await session.execute(
                select(ParsingResult).where(
                    ParsingResult.job_id == job.id,
                    ParsingResult.entity_type == entity_type,
                    ParsingResult.entity_ref == entity_ref,
                )
            )
            if existing.scalar_one_or_none():
                continue
        result = ParsingResult(
            job_id=job.id,
            workspace_id=job.workspace_id,
            entity_type=entity_type,
            entity_ref=entity_ref,
            source_id=source_id,
            payload=_jsonable(item),
            score=_item_score(item),
            matched_keywords=item.get("matched_keywords") or [],
            first_seen_at=item.get("first_seen_at"),
            last_seen_at=item.get("last_activity_at") or item.get("published_at") or item.get("last_message_at"),
        )
        session.add(result)
        saved += 1
    await session.flush()
    return saved


async def export_parsing_results(
    session: AsyncSession,
    job: ParsingJob,
    *,
    export_format: str,
) -> tuple[str, str, str]:
    rows = await _job_result_payloads(session, job)
    export_format = export_format.lower()
    if export_format == "json":
        return (
            "application/json; charset=utf-8",
            f"parsing-job-{job.id}.json",
            json.dumps(rows, ensure_ascii=False, default=str, indent=2),
        )
    if export_format == "txt":
        lines = [_txt_line(row) for row in rows]
        return ("text/plain; charset=utf-8", f"parsing-job-{job.id}.txt", "\n".join(line for line in lines if line))
    if export_format != "csv":
        raise ValueError("Unsupported export format")
    headers = _csv_headers(rows)
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: _csv_value(row.get(header)) for header in headers})
    return ("text/csv; charset=utf-8", f"parsing-job-{job.id}.csv", "\ufeff" + buffer.getvalue())


async def create_mock_posts(
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    limit: int,
    seed_text: str | None = None,
) -> list[TelegramMessage]:
    existing_result = await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.workspace_id == workspace_id, TelegramMessage.source_id == source.id)
        .order_by(TelegramMessage.created_at.desc())
        .limit(limit)
    )
    existing = list(existing_result.scalars().all())
    if existing:
        return existing

    base_text = seed_text or (
        "Как Telegram-комментарии помогают привлекать внимание к экспертному контенту "
        "без прямой рекламы и навязчивых сообщений."
    )
    now = datetime.now(UTC)
    messages: list[TelegramMessage] = []
    for index in range(limit):
        message = TelegramMessage(
            workspace_id=workspace_id,
            source_id=source.id,
            tg_message_id=f"mock-{source.id[:8]}-{index + 1}",
            text=f"{base_text} Пост № {index + 1}.",
            published_at=now - timedelta(minutes=index * 15),
            views_count=100 + index * 17,
            replies_count=3 + index,
            raw={"mode": "mock_parser"},
        )
        session.add(message)
        messages.append(message)

    source.last_parsed_at = now
    await session.commit()
    for message in messages:
        await session.refresh(message)
    return messages


async def fetch_real_posts(
    session: AsyncSession,
    workspace_id: str,
    source: TelegramSource,
    account: TelegramAccount,
    limit: int,
    *,
    ensure_membership: bool = False,
    join_discussion: bool = False,
) -> list[TelegramMessage]:
    source_ref = source_ref_from_fields(source.username, source.url, source.tg_id)
    if ensure_membership:
        access = await ensure_source_access_with_account(
            session,
            account,
            source_ref,
            join_if_needed=True,
            join_discussion=join_discussion,
        )
        source.tg_id = str(access.tg_id) if access.tg_id else source.tg_id
        source.type = access.metadata.get("source_type", source.type)
        source.username = access.username or source.username
        source.title = access.title or source.title
        source.comments_enabled = access.comments_enabled
        source.linked_discussion_tg_id = (
            str(access.linked_discussion_tg_id) if access.linked_discussion_tg_id else source.linked_discussion_tg_id
        )
        source.linked_discussion_username = access.linked_discussion_username or source.linked_discussion_username
        existing_metadata = source.source_metadata or {}
        existing_account_access = existing_metadata.get("account_access")
        account_access = {
            **(existing_account_access if isinstance(existing_account_access, dict) else {}),
            account.id: access.metadata,
        }
        source.source_metadata = {**existing_metadata, **access.metadata, "account_access": account_access}
    posts = await fetch_posts_with_account(session, account, source_ref, limit)
    messages: list[TelegramMessage] = []
    now = datetime.now(UTC)

    for post in posts:
        existing_result = await session.execute(
            select(TelegramMessage).where(
                TelegramMessage.workspace_id == workspace_id,
                TelegramMessage.source_id == source.id,
                TelegramMessage.tg_message_id == post.tg_message_id,
            )
        )
        message = existing_result.scalar_one_or_none()
        if message:
            message.text = post.text
            message.published_at = post.published_at
            message.views_count = post.views_count
            message.forwards_count = post.forwards_count
            message.replies_count = post.replies_count
            message.raw = post.raw
        else:
            message = TelegramMessage(
                workspace_id=workspace_id,
                source_id=source.id,
                tg_message_id=post.tg_message_id,
                text=post.text,
                published_at=post.published_at,
                views_count=post.views_count,
                forwards_count=post.forwards_count,
                replies_count=post.replies_count,
                raw=post.raw,
            )
            session.add(message)
        messages.append(message)

    source.last_parsed_at = now
    source.access_status = "available"
    await session.commit()
    for message in messages:
        await session.refresh(message)
    return messages


async def parse_account_dialogs(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    entity_type: str,
    limit: int,
    keywords: list[str] | None = None,
    min_participants: int | None = None,
    max_participants: int | None = None,
    active_days: int | None = None,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
) -> list[dict[str, Any]]:
    normalized_keywords = _normalized_keywords(keywords or [])
    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    has_filters = bool(
        normalized_keywords or min_participants is not None or max_participants is not None or active_after is not None
    )
    dialog_scan_limit = max(limit * 10, limit) if has_filters else limit
    items: list[dict[str, Any]] = []
    async with telegram_client_for_account(session, account) as client:
        async for dialog in client.iter_dialogs(limit=dialog_scan_limit):
            entity = dialog.entity
            kind = _dialog_kind(entity)
            if entity_type != "all" and kind != entity_type.rstrip("s"):
                continue
            title = _entity_title(entity, getattr(dialog, "name", None))
            username = getattr(entity, "username", None)
            haystack = " ".join(part for part in [title, username] if part)
            matched_keywords = _matched_keywords(haystack, normalized_keywords)
            if normalized_keywords and not matched_keywords:
                continue
            participants_count = getattr(entity, "participants_count", None)
            if min_participants is not None and (
                participants_count is None or int(participants_count) < min_participants
            ):
                continue
            if (
                max_participants is not None
                and participants_count is not None
                and int(participants_count) > max_participants
            ):
                continue
            message = getattr(dialog, "message", None)
            message_date = getattr(message, "date", None)
            last_message_at = message_date.astimezone(UTC) if message_date else None
            if active_after and (last_message_at is None or last_message_at < active_after):
                continue
            items.append(
                {
                    "tg_id": str(getattr(entity, "id", "")) or None,
                    "type": kind,
                    "title": title,
                    "username": username,
                    "participants_count": participants_count,
                    "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
                    "last_message_at": last_message_at,
                    "raw": {
                        "entity_class": type(entity).__name__,
                        "is_channel": bool(getattr(entity, "broadcast", False)),
                        "is_group": bool(getattr(entity, "megagroup", False)),
                        "matched_keywords": matched_keywords,
                    },
                }
            )
            await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(items), limit)
            if len(items) >= limit:
                break
    return items


async def discover_account_channels_into_sources(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    dialog_limit: int | None = None,
    max_sources: int | None = None,
    min_participants: int | None = None,
    active_days: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    effective_dialog_limit = _bounded_int(
        dialog_limit,
        default=settings.telegram_channel_discovery_dialog_limit,
        minimum=1,
        maximum=5000,
    )
    effective_max_sources = _bounded_int(
        max_sources,
        default=settings.telegram_channel_discovery_max_sources,
        minimum=1,
        maximum=1000,
    )
    effective_min_participants = _bounded_int(
        min_participants,
        default=settings.telegram_channel_discovery_min_participants,
        minimum=0,
        maximum=10_000_000,
    )
    effective_active_days = _bounded_int(
        active_days,
        default=settings.telegram_channel_discovery_active_days,
        minimum=1,
        maximum=3650,
    )
    active_after = datetime.now(UTC) - timedelta(days=effective_active_days)
    discovered = await discover_channels_with_account(
        session,
        account,
        dialog_limit=effective_dialog_limit,
    )

    created = 0
    updated = 0
    skipped_reasons: dict[str, int] = {}
    source_ids: list[str] = []

    for source_info in discovered:
        reason = channel_discovery_skip_reason(
            source_info,
            min_participants=effective_min_participants,
            active_after=active_after,
        )
        if reason:
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        source, status = await _upsert_discovered_channel_source(session, account, source_info)
        source_ids.append(source.id)
        if status == "created":
            created += 1
        else:
            updated += 1
        if len(source_ids) >= effective_max_sources:
            break

    await session.commit()
    return {
        "status": "completed",
        "account_id": account.id,
        "workspace_id": account.workspace_id,
        "discovered": len(discovered),
        "created": created,
        "updated": updated,
        "skipped": sum(skipped_reasons.values()),
        "skipped_reasons": skipped_reasons,
        "source_ids": source_ids,
        "filters": {
            "dialog_limit": effective_dialog_limit,
            "max_sources": effective_max_sources,
            "min_participants": effective_min_participants,
            "active_days": effective_active_days,
            "public_username_required": True,
        },
    }


def channel_discovery_skip_reason(
    source_info: TelegramDialogSourceInfo,
    *,
    min_participants: int,
    active_after: datetime,
) -> str | None:
    if getattr(source_info, "source_type", None) != "channel":
        return "not_channel"
    if not (getattr(source_info, "username", None) or "").strip():
        return "private_or_no_username"

    metadata = getattr(source_info, "metadata", {}) or {}
    participants_count = _metadata_int(metadata.get("participants_count"))
    if participants_count is None or participants_count < min_participants:
        return "too_small_or_unknown"

    last_message_at = _metadata_datetime(metadata.get("last_message_at"))
    if last_message_at is None:
        return "activity_unknown"
    if last_message_at < active_after:
        return "inactive"
    return None


async def _upsert_discovered_channel_source(
    session: AsyncSession,
    account: TelegramAccount,
    source_info: TelegramDialogSourceInfo,
) -> tuple[TelegramSource, str]:
    tg_id = str(getattr(source_info, "tg_id", "") or "")
    username = (getattr(source_info, "username", None) or "").strip().lstrip("@").lower() or None
    identity_filters = []
    if tg_id:
        identity_filters.append(TelegramSource.tg_id == tg_id)
    if username:
        identity_filters.append(func.lower(TelegramSource.username) == username)

    source = None
    if identity_filters:
        result = await session.execute(
            select(TelegramSource)
            .where(TelegramSource.workspace_id == account.workspace_id, or_(*identity_filters))
            .limit(1)
        )
        source = result.scalar_one_or_none()

    status = "updated"
    if source is None:
        source = TelegramSource(workspace_id=account.workspace_id, type="channel")
        session.add(source)
        status = "created"

    metadata = getattr(source_info, "metadata", {}) or {}
    existing_metadata = source.source_metadata if isinstance(source.source_metadata, dict) else {}
    discovered_accounts = (
        existing_metadata.get("discovered_accounts")
        if isinstance(existing_metadata.get("discovered_accounts"), dict)
        else {}
    )
    discovered_accounts[account.id] = {
        "last_seen_at": datetime.now(UTC).isoformat(),
        "participants_count": metadata.get("participants_count"),
        "last_message_at": metadata.get("last_message_at"),
    }

    source.type = "channel"
    source.tg_id = tg_id or source.tg_id
    source.username = username or source.username
    source.title = getattr(source_info, "title", None) or source.title or username
    source.url = f"https://t.me/{username}" if username else source.url
    source.comments_enabled = bool(getattr(source_info, "comments_enabled", False))
    linked_id = getattr(source_info, "linked_discussion_tg_id", None)
    source.linked_discussion_tg_id = str(linked_id) if linked_id else source.linked_discussion_tg_id
    source.linked_discussion_username = (
        getattr(source_info, "linked_discussion_username", None) or source.linked_discussion_username
    )
    source.access_status = "available"
    source.source_metadata = {
        **existing_metadata,
        **metadata,
        "auto_discovered": True,
        "auto_discovery_source": "imported_account_dialogs",
        "last_discovered_at": datetime.now(UTC).isoformat(),
        "discovered_accounts": discovered_accounts,
    }
    await session.flush()
    return source, status


async def parse_source_users(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount,
    source_ref: str,
    *,
    limit: int,
    min_comments: int = 0,
    min_messages: int = 0,
    username_required: bool = False,
    active_days: int | None = None,
    keywords: list[str] | None = None,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
) -> list[dict[str, Any]]:
    users_by_id: dict[str, dict[str, Any]] = {}
    comment_counts: dict[str, int] = {}
    message_counts: dict[str, int] = {}
    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    normalized_keywords = _normalized_keywords(keywords or [])
    async with telegram_client_for_account(session, account) as client:
        entity = await client.get_entity(_normalize_ref(source_ref))
        try:
            async for user in client.iter_participants(entity, limit=limit):
                if not isinstance(user, User):
                    continue
                item = _user_item(user)
                if username_required and not item.get("username"):
                    continue
                item["raw"] = {
                    **item.get("raw", {}),
                    "source": "participants",
                    "filters": {
                        "min_comments": min_comments,
                        "min_messages": min_messages,
                        "username_required": username_required,
                        "active_days": active_days,
                        "keywords": normalized_keywords,
                    },
                }
                users_by_id[item["tg_user_id"]] = item
                await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(users_by_id), limit)
        except (RPCError, ValueError):
            # Telegram often hides full participant lists for channels without admin rights.
            # Fall back to visible active users from comments/messages instead of failing the whole run.
            pass

        if len(users_by_id) < limit:
            source_kind = _dialog_kind(entity)
            if source_kind == "channel":
                post_scan_limit = max(10, min(100, limit))
                comments_per_post = max(20, min(200, limit))
                async for post in client.iter_messages(entity, limit=post_scan_limit):
                    if not isinstance(post, Message):
                        continue
                    try:
                        async for message in client.iter_messages(entity, reply_to=post.id, limit=comments_per_post):
                            if len(users_by_id) >= limit:
                                break
                            await _collect_message_sender(
                                message,
                                users_by_id,
                                comment_counts,
                                message_counts,
                                username_required=username_required,
                                active_after=active_after,
                                keywords=normalized_keywords,
                                source="comments",
                            )
                            await _sleep_between_parse_items(
                                delay_seconds_min,
                                delay_seconds_max,
                                len(users_by_id),
                                limit,
                            )
                    except (RPCError, ValueError):
                        continue
                    if len(users_by_id) >= limit:
                        break
            else:
                message_scan_limit = max(limit * 3, 100)
                async for message in client.iter_messages(entity, limit=message_scan_limit):
                    if len(users_by_id) >= limit:
                        break
                    await _collect_message_sender(
                        message,
                        users_by_id,
                        comment_counts,
                        message_counts,
                        username_required=username_required,
                        active_after=active_after,
                        keywords=normalized_keywords,
                        source="messages",
                    )
                    await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(users_by_id), limit)

    users: list[dict[str, Any]] = []
    for item in users_by_id.values():
        comments_count = comment_counts.get(item["tg_user_id"], 0)
        messages_count = message_counts.get(item["tg_user_id"], 0)
        if min_comments and comments_count < min_comments:
            continue
        if min_messages and messages_count < min_messages:
            continue
        if normalized_keywords and not item.get("matched_keywords"):
            continue
        item["comments_count"] = max(int(item.get("comments_count") or 0), comments_count)
        item["messages_count"] = max(int(item.get("messages_count") or 0), messages_count)
        item["raw"] = {
            **item.get("raw", {}),
            "comments_count": item["comments_count"],
            "messages_count": item["messages_count"],
            "fallback_note": (
                "Если Telegram не дает список участников, "
                "собираем видимых активных пользователей из комментариев и сообщений."
            ),
        }
        users.append(item)
        await _upsert_telegram_user(session, workspace_id, item)
        if len(users) >= limit:
            break
    await session.commit()
    return users


def _bounded_int(value: int | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _normalized_delay_bounds(
    delay_seconds_min: float | None,
    delay_seconds_max: float | None,
) -> tuple[float, float] | None:
    minimum = float(delay_seconds_min or 0)
    maximum = float(delay_seconds_max or 0)
    minimum = max(0.0, min(minimum, 60.0))
    maximum = max(0.0, min(maximum, 300.0))
    if minimum == 0 and maximum == 0:
        return None
    if maximum < minimum:
        maximum = minimum
    return minimum, maximum


async def _sleep_between_parse_items(
    delay_seconds_min: float | None,
    delay_seconds_max: float | None,
    current_count: int,
    target_count: int,
) -> None:
    bounds = _normalized_delay_bounds(delay_seconds_min, delay_seconds_max)
    if bounds is None or current_count <= 0 or current_count >= target_count:
        return
    await asyncio.sleep(random.uniform(*bounds))


def _metadata_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "да"}:
            return True
        if normalized in {"0", "false", "no", "n", "нет"}:
            return False
    return None


def _metadata_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def parse_source_comments(
    session: AsyncSession,
    account: TelegramAccount,
    source_ref: str,
    *,
    post_tg_message_id: str | None,
    post_limit: int,
    comments_per_post: int,
    keywords: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    workspace_id: str | None = None,
    source_id: str | None = None,
    job_id: str | None = None,
    ensure_membership: bool = False,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
) -> list[dict[str, Any]]:
    if ensure_membership:
        await ensure_source_access_with_account(
            session,
            account,
            source_ref,
            join_if_needed=True,
            join_discussion=False,
        )

    normalized_keywords = _normalized_keywords(keywords or [])
    comments: list[dict[str, Any]] = []
    async with telegram_client_for_account(session, account) as client:
        entity = await client.get_entity(_normalize_ref(source_ref))
        post_ids: list[str] = []
        if post_tg_message_id:
            post_ids = [post_tg_message_id]
        else:
            async for message in client.iter_messages(entity, limit=post_limit):
                if isinstance(message, Message):
                    post_ids.append(str(message.id))
                    await _sleep_between_parse_items(
                        delay_seconds_min,
                        delay_seconds_max,
                        len(post_ids),
                        post_limit,
                    )

        for post_id in post_ids:
            try:
                async for message in client.iter_messages(entity, reply_to=int(post_id), limit=comments_per_post):
                    if not isinstance(message, Message):
                        continue
                    text = (message.message or "").strip()
                    if not text:
                        continue
                    published_at = message.date.astimezone(UTC) if message.date else None
                    if not _date_in_range(published_at, date_from=date_from, date_to=date_to):
                        continue
                    matched_keywords = _matched_keywords(text, normalized_keywords)
                    if normalized_keywords and not matched_keywords:
                        continue
                    author_username = None
                    author_display_name = None
                    try:
                        sender = await message.get_sender()
                        if isinstance(sender, User):
                            author_username = getattr(sender, "username", None)
                            author_display_name = _user_item(sender).get("display_name")
                    except (RPCError, ValueError):
                        pass
                    comments.append(
                        {
                            "post_tg_message_id": post_id,
                            "comment_tg_message_id": str(message.id),
                            "text": text,
                            "author_tg_id": str(message.sender_id) if message.sender_id else None,
                            "author_username": author_username,
                            "author_display_name": author_display_name,
                            "published_at": published_at,
                            "matched_keywords": matched_keywords,
                            "raw": {
                                "mode": "telethon",
                                "views": int(message.views or 0),
                                "forwards": int(message.forwards or 0),
                            },
                        }
                    )
                    await _sleep_between_parse_items(
                        delay_seconds_min,
                        delay_seconds_max,
                        len(comments),
                        post_limit * comments_per_post,
                    )
            except (RPCError, ValueError):
                continue
    if workspace_id:
        await _persist_parsed_comments(
            session,
            workspace_id=workspace_id,
            source_id=source_id,
            job_id=job_id,
            comments=comments,
        )
        await session.commit()
    return comments


async def parse_message_users(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount,
    source_ref: str,
    *,
    keywords: list[str],
    keywords_mode: str = "any",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    message_limit: int,
    user_limit: int,
    min_messages: int = 1,
    username_required: bool = False,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
) -> list[dict[str, Any]]:
    normalized_keywords = _normalized_keywords(keywords)
    users: dict[str, dict[str, Any]] = {}
    scanned_messages = 0
    async with telegram_client_for_account(session, account) as client:
        entity = await client.get_entity(_normalize_ref(source_ref))
        async for message in client.iter_messages(entity, limit=message_limit):
            scanned_messages += 1
            if not isinstance(message, Message):
                continue
            published_at = message.date.astimezone(UTC) if message.date else None
            if not _date_in_range(published_at, date_from=date_from, date_to=date_to):
                continue
            text = (message.message or "").strip()
            matched = _matched_keywords(text, normalized_keywords, mode=keywords_mode)
            if normalized_keywords and not matched:
                continue
            try:
                sender = await message.get_sender()
            except (RPCError, ValueError):
                continue
            if not isinstance(sender, User) or bool(getattr(sender, "bot", False)):
                continue
            item = _user_item(sender)
            if username_required and not item.get("username"):
                continue
            user_id = item["tg_user_id"]
            current = users.get(user_id)
            if not current:
                current = {
                    **item,
                    "messages_count": 0,
                    "matched_messages_count": 0,
                    "matched_keywords": [],
                    "first_seen_at": published_at,
                    "last_activity_at": published_at,
                    "sample_messages": [],
                    "raw": {**item.get("raw", {}), "source": "message_users"},
                }
                users[user_id] = current
            current["messages_count"] += 1
            current["matched_messages_count"] += 1
            current["matched_keywords"] = sorted(set([*current["matched_keywords"], *matched]))
            current["first_seen_at"] = _earliest_datetime(current.get("first_seen_at"), published_at)
            current["last_activity_at"] = _latest_datetime(current.get("last_activity_at"), published_at)
            if text and len(current["sample_messages"]) < 3:
                current["sample_messages"].append(text[:280])
            await _sleep_between_parse_items(
                delay_seconds_min,
                delay_seconds_max,
                scanned_messages,
                message_limit,
            )

    result: list[dict[str, Any]] = []
    for item in sorted(
        users.values(),
        key=lambda row: (
            int(row.get("matched_messages_count") or 0),
            row.get("last_activity_at") or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    ):
        if int(item.get("matched_messages_count") or 0) < min_messages:
            continue
        await _upsert_telegram_user(session, workspace_id, item, source_ref=source_ref)
        result.append(item)
        if len(result) >= user_limit:
            break
    await session.commit()
    return result


async def channel_search_from_atlas(
    session: AsyncSession,
    workspace_id: str,
    *,
    keywords: list[str],
    category_id: int | None,
    subcategory_id: int | None,
    min_participants: int | None,
    max_participants: int | None,
    comments_required: bool,
    limit: int,
    keyword_suffixes: list[str] | None = None,
    comments_mode: str = "any",
    language_code: str | None = None,
    country_code: str | None = None,
    min_engagement_score: int | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    atlas = await load_atlas()
    queries = _expanded_keyword_queries(keywords, keyword_suffixes) or [None]
    effective_comments_mode = _effective_comments_mode(comments_mode, comments_required)
    level = "micro" if subcategory_id is not None else "macro"
    selected_category_id = subcategory_id if subcategory_id is not None else category_id
    items: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    skipped_reasons: dict[str, int] = {}
    for query in queries:
        _, channels = atlas_channels(
            atlas,
            level=level,
            category_id=selected_category_id,
            query=query,
            limit=max(limit * 3, limit),
            offset=0,
        )
        for channel in channels:
            ref = str(channel.get("ref") or channel.get("username") or channel.get("channel_id") or "").lower()
            if ref in seen_refs:
                continue
            participants = int(channel.get("participants_count") or 0)
            engagement_score = _metadata_int(
                channel.get("engagement_score") or channel.get("spam_suitability_score") or channel.get("quality_score")
            )
            channel_language = (channel.get("language_code") or channel.get("lang") or "").strip() or None
            channel_country = (channel.get("country_code") or channel.get("country") or "").strip() or None
            comments_enabled = _metadata_bool(channel.get("comments_enabled"))
            if min_participants is not None and participants < min_participants:
                skipped_reasons["too_few_participants"] = skipped_reasons.get("too_few_participants", 0) + 1
                continue
            if max_participants is not None and participants > max_participants:
                skipped_reasons["too_many_participants"] = skipped_reasons.get("too_many_participants", 0) + 1
                continue
            if language_code and channel_language and channel_language.lower() != language_code.lower():
                skipped_reasons["language_mismatch"] = skipped_reasons.get("language_mismatch", 0) + 1
                continue
            if country_code and channel_country and channel_country.lower() != country_code.lower():
                skipped_reasons["country_mismatch"] = skipped_reasons.get("country_mismatch", 0) + 1
                continue
            if (
                min_engagement_score is not None
                and engagement_score is not None
                and engagement_score < min_engagement_score
            ):
                skipped_reasons["low_engagement"] = skipped_reasons.get("low_engagement", 0) + 1
                continue
            if not _comments_mode_matches(comments_enabled, effective_comments_mode):
                skipped_reasons["comments_mismatch"] = skipped_reasons.get("comments_mismatch", 0) + 1
                continue
            seen_refs.add(ref)
            status = "needs_telegram_validation" if comments_enabled is None else "found"
            item = {
                "title": channel.get("title"),
                "username": channel.get("username"),
                "ref": channel.get("ref"),
                "tg_id": channel.get("channel_id"),
                "participants_count": participants,
                "language_code": channel_language,
                "country_code": channel_country,
                "category": channel.get("cluster_label"),
                "subcategory": channel.get("sub_cluster_label"),
                "comments_enabled": comments_enabled,
                "linked_discussion": None,
                "engagement_score": engagement_score,
                "source_risk_level": "unknown",
                "status": status,
                "raw": {**channel, "source": "atlas", "query": query},
            }
            items.append(item)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    if job and not items and skipped_reasons:
        await add_parsing_job_log(
            session,
            job,
            "warning",
            "База каналов не дала результатов после фильтров.",
            {"skipped_reasons": skipped_reasons},
        )
        await session.commit()
    if job:
        await persist_parsing_results(session, job=job, entity_type="channel", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    elif session is not None:
        await session.commit()
    return items


async def channel_search_from_telegram(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    keywords: list[str],
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    comments_required: bool,
    limit: int,
    keyword_suffixes: list[str] | None = None,
    comments_mode: str = "any",
    language_code: str | None = None,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
    log_job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    queries = _expanded_keyword_queries(keywords, keyword_suffixes)
    if not queries:
        raise bad_request("Для Telegram search укажите хотя бы одно ключевое слово.")
    effective_comments_mode = _effective_comments_mode(comments_mode, comments_required)
    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    items: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    per_query_limit = max(10, min(100, limit * 3))

    async with telegram_client_for_account(session, account) as client:
        for query in queries:
            try:
                found = await client(SearchRequest(q=query, limit=per_query_limit))
            except Exception as exc:
                warning_job = log_job or job
                if warning_job:
                    await add_parsing_job_log(
                        session,
                        warning_job,
                        "warning",
                        "Telegram search пропустил один запрос.",
                        {"query": query, "error_code": type(exc).__name__, "error_message": str(exc)},
                    )
                    await session.commit()
                continue
            for entity in getattr(found, "chats", []) or []:
                if not isinstance(entity, Channel) or not getattr(entity, "broadcast", False):
                    continue
                ref = f"@{entity.username}" if getattr(entity, "username", None) else str(getattr(entity, "id", ""))
                if not ref or ref in seen_refs:
                    continue

                full_chat = None
                linked_id = None
                try:
                    full = await client(GetFullChannelRequest(entity))
                    full_chat = getattr(full, "full_chat", None)
                    linked_id = getattr(full_chat, "linked_chat_id", None) if full_chat else None
                except (RPCError, ValueError):
                    full_chat = None

                participants = getattr(full_chat, "participants_count", None) if full_chat else None
                if participants is None:
                    participants = getattr(entity, "participants_count", None)
                participants_int = _metadata_int(participants)
                if min_participants is not None and (participants_int is None or participants_int < min_participants):
                    continue
                if (
                    max_participants is not None
                    and participants_int is not None
                    and participants_int > max_participants
                ):
                    continue

                comments_enabled = bool(linked_id) if full_chat else None
                if not _comments_mode_matches(comments_enabled, effective_comments_mode):
                    continue

                about = getattr(full_chat, "about", None) if full_chat else None
                detected_language = _detect_text_language(" ".join(part for part in [entity.title, about] if part))
                if language_code and detected_language and detected_language.lower() != language_code.lower():
                    continue

                last_message_at = await _latest_channel_message_at(client, entity)
                if active_after and (last_message_at is None or last_message_at < active_after):
                    continue

                seen_refs.add(ref)
                item = {
                    "title": getattr(entity, "title", None) or ref,
                    "username": getattr(entity, "username", None),
                    "ref": ref,
                    "tg_id": str(getattr(entity, "id", "") or "") or None,
                    "participants_count": participants_int,
                    "language_code": detected_language,
                    "country_code": None,
                    "category": None,
                    "subcategory": None,
                    "comments_enabled": comments_enabled,
                    "linked_discussion": str(linked_id) if linked_id else None,
                    "last_message_at": last_message_at,
                    "engagement_score": _activity_score(last_message_at),
                    "source_risk_level": "unknown",
                    "status": "found" if comments_enabled is not None else "needs_telegram_validation",
                    "raw": {
                        "source": "telegram_search",
                        "query": query,
                        "about": about,
                        "entity_class": type(entity).__name__,
                    },
                }
                items.append(item)
                await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(items), limit)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break

    if job:
        await persist_parsing_results(session, job=job, entity_type="channel", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    else:
        await session.commit()
    return items


async def channel_search_mixed(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount,
    *,
    keywords: list[str],
    keyword_suffixes: list[str] | None,
    category_id: int | None,
    subcategory_id: int | None,
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    comments_required: bool,
    comments_mode: str,
    limit: int,
    language_code: str | None = None,
    country_code: str | None = None,
    min_engagement_score: int | None = None,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    telegram_limit = max(limit, min(200, limit * 2))
    telegram_items = []
    if _expanded_keyword_queries(keywords, keyword_suffixes):
        try:
            telegram_items = await channel_search_from_telegram(
                session,
                account,
                keywords=keywords,
                keyword_suffixes=keyword_suffixes,
                min_participants=min_participants,
                max_participants=max_participants,
                active_days=active_days,
                language_code=language_code,
                comments_required=comments_required,
                comments_mode=comments_mode,
                limit=telegram_limit,
                delay_seconds_min=delay_seconds_min,
                delay_seconds_max=delay_seconds_max,
                log_job=job,
            )
        except Exception as exc:
            if job:
                await add_parsing_job_log(
                    session,
                    job,
                    "warning",
                    "Telegram global search не отработал, добираем источники из базы каналов.",
                    {"error_code": type(exc).__name__, "error_message": str(exc)},
                )
                await session.commit()
    atlas_limit = max(limit - len(telegram_items), min(200, limit * 2))
    atlas_items = []
    if len(telegram_items) < limit:
        effective_comments_mode = _effective_comments_mode(comments_mode, comments_required)
        atlas_comments_required = comments_required
        atlas_comments_mode = comments_mode
        if effective_comments_mode == "open":
            atlas_comments_required = False
            atlas_comments_mode = "any"
        atlas_items = await channel_search_from_atlas(
            session,
            workspace_id,
            keywords=keywords,
            keyword_suffixes=keyword_suffixes,
            category_id=None,
            subcategory_id=None,
            min_participants=min_participants,
            max_participants=max_participants,
            language_code=language_code,
            country_code=country_code,
            comments_required=atlas_comments_required,
            comments_mode=atlas_comments_mode,
            min_engagement_score=min_engagement_score,
            limit=atlas_limit,
        )
        if effective_comments_mode == "open":
            confirmed_atlas_items = [
                item
                for item in atlas_items
                if _comments_mode_matches(item.get("comments_enabled"), effective_comments_mode)
            ]
            validation_candidates = [
                item
                for item in atlas_items
                if not _comments_mode_matches(item.get("comments_enabled"), effective_comments_mode)
            ]
            validated_atlas_items = await _validate_channel_search_items_with_account(
                session,
                account,
                validation_candidates,
                comments_mode=effective_comments_mode,
                min_participants=min_participants,
                max_participants=max_participants,
                active_days=active_days,
                language_code=language_code,
                limit=max(0, limit - len(telegram_items) - len(confirmed_atlas_items)),
                delay_seconds_min=delay_seconds_min,
                delay_seconds_max=delay_seconds_max,
                job=job,
            )
            atlas_items = [*confirmed_atlas_items, *validated_atlas_items]
    items = _merge_channel_search_items([*telegram_items, *atlas_items], limit=limit)
    if job:
        await persist_parsing_results(session, job=job, entity_type="channel", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    else:
        await session.commit()
    return items


async def _validate_channel_search_items_with_account(
    session: AsyncSession,
    account: TelegramAccount,
    items: list[dict[str, Any]],
    *,
    comments_mode: str,
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    language_code: str | None,
    limit: int,
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0 or not items:
        return []

    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    validated: list[dict[str, Any]] = []
    skipped_reasons: dict[str, int] = {}

    async with telegram_client_for_account(session, account) as client:
        for item in items:
            ref = str(item.get("ref") or item.get("username") or item.get("tg_id") or "").strip()
            if not ref:
                skipped_reasons["missing_ref"] = skipped_reasons.get("missing_ref", 0) + 1
                continue
            try:
                entity = await client.get_entity(_normalize_ref(ref))
                if not isinstance(entity, Channel) or not getattr(entity, "broadcast", False):
                    skipped_reasons["not_channel"] = skipped_reasons.get("not_channel", 0) + 1
                    continue
                full = await client(GetFullChannelRequest(entity))
                full_chat = getattr(full, "full_chat", None)
                linked_id = getattr(full_chat, "linked_chat_id", None) if full_chat else None
            except (RPCError, ValueError) as exc:
                skipped_reasons[type(exc).__name__] = skipped_reasons.get(type(exc).__name__, 0) + 1
                continue

            comments_enabled = bool(linked_id) if full_chat else None
            if not _comments_mode_matches(comments_enabled, comments_mode):
                skipped_reasons["comments_mismatch"] = skipped_reasons.get("comments_mismatch", 0) + 1
                continue

            participants = getattr(full_chat, "participants_count", None) if full_chat else None
            if participants is None:
                participants = getattr(entity, "participants_count", None)
            participants_int = _metadata_int(participants)
            if min_participants is not None and (participants_int is None or participants_int < min_participants):
                skipped_reasons["too_few_participants"] = skipped_reasons.get("too_few_participants", 0) + 1
                continue
            if max_participants is not None and participants_int is not None and participants_int > max_participants:
                skipped_reasons["too_many_participants"] = skipped_reasons.get("too_many_participants", 0) + 1
                continue

            about = getattr(full_chat, "about", None) if full_chat else None
            detected_language = _detect_text_language(
                " ".join(part for part in [getattr(entity, "title", None), about] if part)
            )
            if language_code and detected_language and detected_language.lower() != language_code.lower():
                skipped_reasons["language_mismatch"] = skipped_reasons.get("language_mismatch", 0) + 1
                continue

            last_message_at = await _latest_channel_message_at(client, entity)
            if active_after and (last_message_at is None or last_message_at < active_after):
                skipped_reasons["inactive"] = skipped_reasons.get("inactive", 0) + 1
                continue

            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            validated.append(
                {
                    **item,
                    "title": getattr(entity, "title", None) or item.get("title") or ref,
                    "username": getattr(entity, "username", None) or item.get("username"),
                    "ref": f"@{entity.username}" if getattr(entity, "username", None) else item.get("ref") or ref,
                    "tg_id": str(getattr(entity, "id", "") or "") or item.get("tg_id"),
                    "participants_count": participants_int,
                    "language_code": detected_language or item.get("language_code"),
                    "comments_enabled": comments_enabled,
                    "linked_discussion": str(linked_id) if linked_id else None,
                    "last_message_at": last_message_at,
                    "engagement_score": _activity_score(last_message_at) or item.get("engagement_score"),
                    "status": "found",
                    "raw": {
                        **raw,
                        "telegram_validation": {
                            "comments_checked": True,
                            "linked_discussion_tg_id": str(linked_id) if linked_id else None,
                            "source": "atlas_telegram_validation",
                        },
                    },
                }
            )
            await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(validated), limit)
            if len(validated) >= limit:
                break

    if job and skipped_reasons:
        await add_parsing_job_log(
            session,
            job,
            "info",
            "Кандидаты из базы проверены через Telegram.",
            {"skipped_reasons": skipped_reasons, "validated": len(validated)},
        )
        await session.commit()
    return validated


async def group_search_from_account_dialogs(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    keywords: list[str],
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    limit: int,
    keyword_suffixes: list[str] | None = None,
    min_activity_score: int | None = None,
    access_type: str = "any",
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    normalized_keywords = _normalized_keywords([*keywords, *(keyword_suffixes or [])])
    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    items: list[dict[str, Any]] = []
    dialog_limit = max(limit * 10, 100)
    async with telegram_client_for_account(session, account) as client:
        async for dialog in client.iter_dialogs(limit=dialog_limit):
            entity = dialog.entity
            if _dialog_kind(entity) != "group":
                continue
            title = _entity_title(entity, getattr(dialog, "name", None))
            username = getattr(entity, "username", None)
            is_public = bool(username)
            if access_type == "public" and not is_public:
                continue
            if access_type == "private" and is_public:
                continue
            haystack = " ".join(part for part in [title, username] if part)
            matched_keywords = _matched_keywords(haystack, normalized_keywords)
            if normalized_keywords and not matched_keywords:
                continue
            participants_count = getattr(entity, "participants_count", None)
            if min_participants is not None and (
                participants_count is None or int(participants_count) < min_participants
            ):
                continue
            if (
                max_participants is not None
                and participants_count is not None
                and int(participants_count) > max_participants
            ):
                continue
            message = getattr(dialog, "message", None)
            last_message_at = getattr(message, "date", None)
            last_message_at = last_message_at.astimezone(UTC) if last_message_at else None
            if active_after and (last_message_at is None or last_message_at < active_after):
                continue
            activity_score = _activity_score(last_message_at)
            if min_activity_score is not None and (activity_score is None or activity_score < min_activity_score):
                continue
            tg_id = str(getattr(entity, "id", "") or "")
            ref = f"@{username}" if username else tg_id
            item = {
                "title": title,
                "username": username,
                "ref": ref,
                "tg_id": tg_id or None,
                "participants_count": participants_count,
                "last_message_at": last_message_at,
                "activity_score": activity_score,
                "matched_keywords": matched_keywords,
                "source": "account_dialogs",
                "raw": {
                    "entity_class": type(entity).__name__,
                    "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
                    "access_type": "public" if is_public else "private",
                },
            }
            items.append(item)
            await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(items), limit)
            if len(items) >= limit:
                break
    if job:
        await persist_parsing_results(session, job=job, entity_type="group", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    else:
        await session.commit()
    return items


async def group_search_from_telegram(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    keywords: list[str],
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    limit: int,
    keyword_suffixes: list[str] | None = None,
    min_activity_score: int | None = None,
    access_type: str = "any",
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    queries = _expanded_keyword_queries(keywords, keyword_suffixes)
    if not queries:
        raise bad_request("Для поиска групп укажите хотя бы одно ключевое слово.")

    normalized_keywords = _normalized_keywords([*keywords, *(keyword_suffixes or [])])
    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    per_query_limit = max(10, min(100, limit * 3))
    items: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    async with telegram_client_for_account(session, account) as client:
        for query in queries:
            found = await client(SearchRequest(q=query, limit=per_query_limit))
            for entity in getattr(found, "chats", []) or []:
                if _dialog_kind(entity) != "group":
                    continue

                username = getattr(entity, "username", None)
                is_public = bool(username)
                if access_type == "public" and not is_public:
                    continue
                if access_type == "private" and is_public:
                    continue

                tg_id = str(getattr(entity, "id", "") or "")
                ref = f"@{username}" if username else tg_id
                if not ref or ref in seen_refs:
                    continue

                full_chat = None
                if isinstance(entity, Channel):
                    try:
                        full = await client(GetFullChannelRequest(entity))
                        full_chat = getattr(full, "full_chat", None)
                    except (RPCError, ValueError):
                        full_chat = None

                participants = getattr(full_chat, "participants_count", None) if full_chat else None
                if participants is None:
                    participants = getattr(entity, "participants_count", None)
                participants_int = _metadata_int(participants)
                if min_participants is not None and (participants_int is None or participants_int < min_participants):
                    continue
                if (
                    max_participants is not None
                    and participants_int is not None
                    and participants_int > max_participants
                ):
                    continue

                last_message_at = await _latest_channel_message_at(client, entity)
                if active_after and (last_message_at is None or last_message_at < active_after):
                    continue
                activity_score = _activity_score(last_message_at)
                if min_activity_score is not None and (activity_score is None or activity_score < min_activity_score):
                    continue

                title = _entity_title(entity, None)
                haystack = " ".join(part for part in [title, username] if part)
                matched_keywords = _matched_keywords(haystack, normalized_keywords)
                if not matched_keywords:
                    matched_keywords = [query]

                seen_refs.add(ref)
                item = {
                    "title": title or ref,
                    "username": username,
                    "ref": ref,
                    "tg_id": tg_id or None,
                    "participants_count": participants_int,
                    "last_message_at": last_message_at,
                    "activity_score": activity_score,
                    "matched_keywords": matched_keywords,
                    "source": "telegram_search",
                    "raw": {
                        "source": "telegram_search",
                        "query": query,
                        "entity_class": type(entity).__name__,
                        "access_type": "public" if is_public else "private",
                    },
                }
                items.append(item)
                await _sleep_between_parse_items(delay_seconds_min, delay_seconds_max, len(items), limit)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break

    if job:
        await persist_parsing_results(session, job=job, entity_type="group", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    else:
        await session.commit()
    return items


async def group_search_mixed(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    keywords: list[str],
    min_participants: int | None,
    max_participants: int | None,
    active_days: int | None,
    limit: int,
    keyword_suffixes: list[str] | None = None,
    min_activity_score: int | None = None,
    access_type: str = "any",
    delay_seconds_min: float | None = None,
    delay_seconds_max: float | None = None,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    telegram_limit = max(limit, min(200, limit * 2))
    dialog_limit = max(limit, min(200, limit * 2))
    telegram_items = await group_search_from_telegram(
        session,
        account,
        keywords=keywords,
        keyword_suffixes=keyword_suffixes,
        min_participants=min_participants,
        max_participants=max_participants,
        active_days=active_days,
        min_activity_score=min_activity_score,
        access_type=access_type,
        limit=telegram_limit,
        delay_seconds_min=delay_seconds_min,
        delay_seconds_max=delay_seconds_max,
    )
    dialog_items = await group_search_from_account_dialogs(
        session,
        account,
        keywords=keywords,
        keyword_suffixes=keyword_suffixes,
        min_participants=min_participants,
        max_participants=max_participants,
        active_days=active_days,
        min_activity_score=min_activity_score,
        access_type=access_type,
        limit=dialog_limit,
        delay_seconds_min=delay_seconds_min,
        delay_seconds_max=delay_seconds_max,
    )
    items = _merge_channel_search_items([*telegram_items, *dialog_items], limit=limit)
    if job:
        await persist_parsing_results(session, job=job, entity_type="group", items=items)
        await complete_parsing_job(session, job, found_count=len(items))
    else:
        await session.commit()
    return items


async def validate_channel_sources(
    session: AsyncSession,
    workspace_id: str,
    account: TelegramAccount,
    *,
    source_ids: list[str],
    refs: list[str],
    comments_required: bool,
    min_participants: int | None,
    active_days: int | None,
    limit: int,
    job: ParsingJob | None = None,
) -> list[dict[str, Any]]:
    normalized_refs = [_normalize_ref(ref) for ref in refs if str(ref or "").strip()]
    sources_by_ref: dict[str, TelegramSource | None] = {}
    if source_ids:
        result = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.id.in_(source_ids[:limit]),
            )
        )
        for source in result.scalars().all():
            ref = source_ref_for_source(source)
            normalized_refs.append(ref)
            sources_by_ref[_normalize_ref(ref)] = source
    if not normalized_refs:
        result = await session.execute(
            select(TelegramSource)
            .where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.type.in_(["channel", "group"]),
            )
            .order_by(TelegramSource.updated_at.desc())
            .limit(limit)
        )
        for source in result.scalars().all():
            ref = source_ref_for_source(source)
            normalized_refs.append(ref)
            sources_by_ref[_normalize_ref(ref)] = source

    active_after = datetime.now(UTC) - timedelta(days=active_days) if active_days else None
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in normalized_refs:
        if ref in seen or len(items) >= limit:
            continue
        seen.add(ref)
        source = sources_by_ref.get(ref)
        try:
            access = await ensure_source_access_with_account(
                session,
                account,
                ref,
                join_if_needed=False,
                join_discussion=False,
            )
            metadata = access.metadata or {}
            participants = _metadata_int(metadata.get("participants_count"))
            last_message_at = _metadata_datetime(metadata.get("last_message_at"))
            comments_enabled = bool(access.comments_enabled)
            filtered_reason = None
            if min_participants is not None and (participants is None or participants < min_participants):
                filtered_reason = "too_small_or_unknown"
            if active_after and (last_message_at is None or last_message_at < active_after):
                filtered_reason = "inactive_or_unknown"
            if comments_required and not comments_enabled:
                filtered_reason = "comments_unavailable"
            if source:
                source.tg_id = str(access.tg_id) if access.tg_id else source.tg_id
                source.username = access.username or source.username
                source.title = access.title or source.title
                source.comments_enabled = comments_enabled
                source.participants_count = participants or source.participants_count
                source.last_message_at = last_message_at or source.last_message_at
                source.linked_discussion_tg_id = (
                    str(access.linked_discussion_tg_id)
                    if access.linked_discussion_tg_id
                    else source.linked_discussion_tg_id
                )
                source.linked_discussion_username = (
                    access.linked_discussion_username or source.linked_discussion_username
                )
                source.access_status = "available" if not filtered_reason else filtered_reason
                source.last_parsed_at = datetime.now(UTC)
                source.source_metadata = {**(source.source_metadata or {}), **metadata}
            items.append(
                {
                    "title": access.title or (source.title if source else None) or ref,
                    "username": access.username or (source.username if source else None),
                    "ref": source_ref_from_fields(access.username, None, access.tg_id) if access.tg_id else ref,
                    "tg_id": str(access.tg_id) if access.tg_id else None,
                    "participants_count": participants,
                    "comments_enabled": comments_enabled,
                    "linked_discussion": access.linked_discussion_username,
                    "last_message_at": last_message_at,
                    "source_risk_level": "low" if not filtered_reason else "needs_attention",
                    "status": "valid" if not filtered_reason else filtered_reason,
                    "raw": metadata,
                }
            )
        except Exception as exc:
            if source:
                source.access_status = type(exc).__name__
                source.last_parsed_at = datetime.now(UTC)
            items.append(
                {
                    "title": source.title if source else ref,
                    "username": source.username if source else None,
                    "ref": ref,
                    "tg_id": source.tg_id if source else None,
                    "participants_count": None,
                    "comments_enabled": None,
                    "linked_discussion": None,
                    "last_message_at": None,
                    "source_risk_level": "high",
                    "status": type(exc).__name__,
                    "raw": {"error": str(exc)},
                }
            )
    if job:
        await persist_parsing_results(session, job=job, entity_type="channel", items=items)
        await complete_parsing_job(
            session,
            job,
            found_count=len(items),
            saved_count=sum(1 for item in items if item.get("status") == "valid"),
            skipped_count=sum(1 for item in items if item.get("status") != "valid"),
        )
    else:
        await session.commit()
    return items


def source_ref_for_source(source: TelegramSource) -> str:
    return source_ref_from_fields(source.username, source.url, source.tg_id)


async def _upsert_telegram_user(
    session: AsyncSession,
    workspace_id: str,
    item: dict[str, Any],
    *,
    source_ref: str | None = None,
) -> None:
    result = await session.execute(
        select(TelegramUser).where(
            TelegramUser.workspace_id == workspace_id,
            TelegramUser.tg_user_id == item["tg_user_id"],
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        row = TelegramUser(workspace_id=workspace_id, tg_user_id=item["tg_user_id"])
        session.add(row)
    row.username = item.get("username")
    row.first_name = item.get("first_name")
    row.last_name = item.get("last_name")
    row.is_bot = bool(item.get("is_bot"))
    row.language_code = item.get("language_code")
    now = datetime.now(UTC)
    row.first_seen_at = row.first_seen_at or item.get("first_seen_at") or now
    row.last_seen_at = now
    row.last_activity_at = item.get("last_activity_at") or row.last_activity_at
    row.comments_count = max(
        int(row.comments_count or 0),
        int(item.get("comments_count") or item.get("raw", {}).get("comments_count") or 0),
    )
    row.messages_count = max(int(row.messages_count or 0), int(item.get("messages_count") or 0))
    row.matched_keywords = sorted(set([*(row.matched_keywords or []), *(item.get("matched_keywords") or [])]))
    source_refs = set(row.source_refs or [])
    if source_ref:
        source_refs.add(source_ref)
    row.source_refs = sorted(source_refs)
    row.is_restricted = bool(item.get("raw", {}).get("scam") or item.get("raw", {}).get("fake"))
    row.user_metadata = item.get("raw") or {}


async def _persist_parsed_comments(
    session: AsyncSession,
    *,
    workspace_id: str,
    source_id: str | None,
    job_id: str | None,
    comments: list[dict[str, Any]],
) -> int:
    saved = 0
    for item in comments:
        existing = None
        if source_id:
            existing_result = await session.execute(
                select(ParsedTelegramComment).where(
                    ParsedTelegramComment.source_id == source_id,
                    ParsedTelegramComment.comment_tg_message_id == item["comment_tg_message_id"],
                )
            )
            existing = existing_result.scalar_one_or_none()
        row = existing or ParsedTelegramComment(
            workspace_id=workspace_id,
            source_id=source_id,
            post_tg_message_id=item["post_tg_message_id"],
            comment_tg_message_id=item["comment_tg_message_id"],
        )
        if not existing:
            session.add(row)
            saved += 1
        row.job_id = job_id or row.job_id
        row.text = item["text"]
        row.author_tg_id = item.get("author_tg_id")
        row.author_username = item.get("author_username")
        row.author_display_name = item.get("author_display_name")
        row.published_at = item.get("published_at")
        row.matched_keywords = item.get("matched_keywords") or []
        row.raw = item.get("raw") or {}
    await session.flush()
    return saved


async def _collect_message_sender(
    message: Message,
    users_by_id: dict[str, dict[str, Any]],
    comment_counts: dict[str, int],
    message_counts: dict[str, int],
    *,
    username_required: bool,
    active_after: datetime | None,
    keywords: list[str] | None = None,
    source: str,
) -> None:
    if not isinstance(message, Message):
        return
    message_date = message.date.astimezone(UTC) if message.date else None
    if active_after and message_date and message_date < active_after:
        return
    text = (message.message or "").strip()
    matched_keywords = _matched_keywords(text, keywords or [])
    if keywords and not matched_keywords:
        return
    try:
        sender = await message.get_sender()
    except (RPCError, ValueError):
        return
    if not isinstance(sender, User) or bool(getattr(sender, "bot", False)):
        return
    item = _user_item(sender)
    if username_required and not item.get("username"):
        return
    user_id = item["tg_user_id"]
    if source == "comments":
        comment_counts[user_id] = comment_counts.get(user_id, 0) + 1
    else:
        message_counts[user_id] = message_counts.get(user_id, 0) + 1
    existing = users_by_id.get(user_id)
    if existing:
        existing["matched_keywords"] = sorted(set([*(existing.get("matched_keywords") or []), *matched_keywords]))
        existing["last_activity_at"] = _latest_datetime(existing.get("last_activity_at"), message_date)
        if text:
            samples = list(existing.get("sample_messages") or [])
            if len(samples) < 3:
                samples.append(text[:280])
            existing["sample_messages"] = samples
        existing["raw"] = {
            **existing.get("raw", {}),
            "source": existing.get("raw", {}).get("source") or source,
            "last_activity_at": message_date.isoformat() if message_date else None,
        }
        return
    item["matched_keywords"] = matched_keywords
    item["messages_count"] = 0 if source == "comments" else 1
    item["comments_count"] = 1 if source == "comments" else 0
    item["first_seen_at"] = message_date
    item["last_activity_at"] = message_date
    item["sample_messages"] = [text[:280]] if text else []
    item["raw"] = {
        **item.get("raw", {}),
        "source": source,
        "last_activity_at": message_date.isoformat() if message_date else None,
    }
    users_by_id[user_id] = item


def _dialog_kind(entity: Any) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or type(entity).__name__ == "Chat":
        return "group"
    if type(entity).__name__ == "User":
        return "dialog"
    return "dialog"


def _entity_title(entity: Any, fallback: str | None) -> str | None:
    title = getattr(entity, "title", None)
    if title:
        return title
    first_name = getattr(entity, "first_name", None) or ""
    last_name = getattr(entity, "last_name", None) or ""
    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
    return display_name or fallback or getattr(entity, "username", None)


def _user_item(user: User) -> dict[str, Any]:
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    display_name = " ".join(part for part in [first_name, last_name] if part).strip() or getattr(user, "username", None)
    return {
        "tg_user_id": str(user.id),
        "username": getattr(user, "username", None),
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "is_bot": bool(getattr(user, "bot", False)),
        "language_code": getattr(user, "lang_code", None),
        "raw": {
            "verified": bool(getattr(user, "verified", False)),
            "premium": bool(getattr(user, "premium", False)),
            "scam": bool(getattr(user, "scam", False)),
            "fake": bool(getattr(user, "fake", False)),
        },
    }


def _normalize_ref(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        return value
    if value.startswith("t.me/"):
        return f"https://{value}"
    if value.startswith("@"):
        return value[1:]
    return value


def _normalized_keywords(keywords: list[str]) -> list[str]:
    normalized: list[str] = []
    for keyword in keywords:
        value = str(keyword or "").strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _expanded_keyword_queries(keywords: list[str], suffixes: list[str] | None = None) -> list[str]:
    base_keywords = [str(keyword or "").strip() for keyword in keywords if str(keyword or "").strip()]
    clean_suffixes = [str(suffix or "").strip() for suffix in suffixes or [] if str(suffix or "").strip()]
    queries: list[str] = []
    for keyword in base_keywords:
        if keyword not in queries:
            queries.append(keyword)
        for suffix in clean_suffixes:
            combined = f"{keyword} {suffix}".strip()
            if combined and combined not in queries:
                queries.append(combined)
    return queries


def _effective_comments_mode(comments_mode: str, comments_required: bool) -> str:
    if comments_required:
        return "open"
    return comments_mode if comments_mode in {"any", "open", "closed"} else "any"


def _comments_mode_matches(comments_enabled: bool | None, comments_mode: str) -> bool:
    if comments_mode == "open":
        return comments_enabled is True
    if comments_mode == "closed":
        return comments_enabled is False
    return True


def _channel_search_identity(item: dict[str, Any]) -> str:
    username = str(item.get("username") or "").strip().lower().lstrip("@")
    if username:
        return f"username:{username}"
    tg_id = str(item.get("tg_id") or "").strip()
    if tg_id:
        return f"tg:{tg_id}"
    return f"ref:{_normalize_ref(str(item.get('ref') or '')).lower()}"


def _merge_channel_search_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        identity = _channel_search_identity(item)
        if not identity:
            continue
        current = merged.get(identity)
        if current is None:
            merged[identity] = item
            order.append(identity)
            continue
        current_raw = current.get("raw") if isinstance(current.get("raw"), dict) else {}
        item_raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        current["raw"] = {
            **item_raw,
            **current_raw,
            "merged_sources": sorted(
                {
                    str(current_raw.get("source") or "atlas"),
                    str(item_raw.get("source") or "atlas"),
                }
            ),
        }
        for key in ("participants_count", "comments_enabled", "linked_discussion", "last_message_at", "language_code"):
            if current.get(key) is None and item.get(key) is not None:
                current[key] = item.get(key)
        if current.get("status") == "needs_telegram_validation" and item.get("status") == "found":
            current["status"] = "found"
    return [merged[identity] for identity in order[:limit]]


def _matched_keywords(text: str, keywords: list[str], *, mode: str = "any") -> list[str]:
    if not keywords:
        return []
    haystack = (text or "").lower()
    if mode == "phrase":
        phrase = " ".join(keywords).strip()
        return [phrase] if phrase and phrase in haystack else []
    matched = [keyword for keyword in keywords if keyword in haystack]
    if mode == "all" and len(matched) != len(keywords):
        return []
    return matched


def _date_in_range(value: datetime | None, *, date_from: datetime | None, date_to: datetime | None) -> bool:
    if value is None:
        return True
    if date_from and value < _ensure_utc(date_from):
        return False
    if date_to and value > _ensure_utc(date_to):
        return False
    return True


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _earliest_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _latest_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _result_entity_ref(entity_type: str, item: dict[str, Any]) -> str | None:
    if entity_type == "user":
        return str(item.get("tg_user_id") or item.get("username") or "")
    if entity_type == "comment":
        return str(item.get("comment_tg_message_id") or "")
    if entity_type == "message":
        return str(item.get("tg_message_id") or "")
    if entity_type in {"source", "channel", "group"}:
        return str(item.get("ref") or item.get("username") or item.get("tg_id") or "")
    return str(item.get("id") or item.get("entity_ref") or "") or None


def _item_score(item: dict[str, Any]) -> float | None:
    for key in ["score", "activity_score", "spam_suitability_score", "engagement_score"]:
        value = item.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def _activity_score(last_message_at: datetime | None) -> int | None:
    if not last_message_at:
        return None
    age_hours = max(0, (datetime.now(UTC) - last_message_at).total_seconds() / 3600)
    if age_hours <= 24:
        return 100
    if age_hours <= 24 * 7:
        return 80
    if age_hours <= 24 * 30:
        return 55
    if age_hours <= 24 * 90:
        return 30
    return 10


async def _latest_channel_message_at(client: Any, entity: Channel) -> datetime | None:
    try:
        async for message in client.iter_messages(entity, limit=1):
            message_date = getattr(message, "date", None)
            if isinstance(message_date, datetime):
                return message_date.astimezone(UTC)
            return None
    except (RPCError, ValueError):
        return None
    return None


def _detect_text_language(text: str) -> str | None:
    compact = (text or "").strip()
    if not compact:
        return None
    cyrillic = sum(1 for char in compact.lower() if "а" <= char <= "я" or char == "ё")
    latin = sum(1 for char in compact.lower() if "a" <= char <= "z")
    if cyrillic >= 3 and cyrillic >= latin:
        return "ru"
    if latin >= 3 and latin > cyrillic:
        return "en"
    return None


async def _job_result_payloads(session: AsyncSession, job: ParsingJob) -> list[dict[str, Any]]:
    result = await session.execute(
        select(ParsingResult).where(ParsingResult.job_id == job.id).order_by(ParsingResult.created_at.asc())
    )
    return [row.payload for row in result.scalars().all()]


def _csv_headers(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "entity_type",
        "title",
        "display_name",
        "username",
        "tg_user_id",
        "ref",
        "participants_count",
        "messages_count",
        "matched_messages_count",
        "comments_count",
        "matched_keywords",
        "published_at",
        "text",
    ]
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys and not isinstance(row.get(key), dict | list):
                keys.append(key)
    return [key for key in preferred if key in keys] + [key for key in keys if key not in preferred]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | dict):
        return json.dumps(_jsonable(value), ensure_ascii=False)
    return str(value)


def _txt_line(row: dict[str, Any]) -> str:
    return str(
        row.get("username")
        or row.get("ref")
        or row.get("display_name")
        or row.get("title")
        or row.get("text")
        or row.get("tg_user_id")
        or ""
    )
