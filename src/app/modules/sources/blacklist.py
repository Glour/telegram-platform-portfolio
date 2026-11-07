from datetime import UTC, datetime
from typing import Any

from app.modules.telegram_gateway.service import source_ref_from_fields
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sources.models import TelegramSource, TelegramSourceBlacklistEntry

BLACKLISTED_SOURCE_STATUS = "blacklisted"
ACTIVE_BLACKLIST_STATUS = "active"
RESOLVED_BLACKLIST_STATUS = "resolved"


async def blacklist_source(
    session: AsyncSession,
    *,
    workspace_id: str,
    source: TelegramSource,
    reason: str,
    error_code: str | None = None,
    error_message: str | None = None,
    campaign_id: str | None = None,
    account_id: str | None = None,
    blocked_by: str = "system",
    now: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> TelegramSourceBlacklistEntry:
    blocked_at = now or datetime.now(UTC)
    source_ref = source_ref_from_fields(source.username, source.url, source.tg_id)
    result = await session.execute(
        select(TelegramSourceBlacklistEntry).where(
            TelegramSourceBlacklistEntry.workspace_id == workspace_id,
            TelegramSourceBlacklistEntry.source_id == source.id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        entry = TelegramSourceBlacklistEntry(
            workspace_id=workspace_id,
            source_id=source.id,
            source_ref=source_ref,
            source_type=source.type,
            status=ACTIVE_BLACKLIST_STATUS,
            reason=reason,
            error_code=error_code,
            error_message=error_message,
            campaign_id=campaign_id,
            account_id=account_id,
            blocked_by=blocked_by,
            first_blocked_at=blocked_at,
            last_seen_at=blocked_at,
            details=details or {},
        )
        session.add(entry)
    else:
        entry.source_ref = source_ref
        entry.source_type = source.type
        entry.status = ACTIVE_BLACKLIST_STATUS
        entry.reason = reason
        entry.error_code = error_code
        entry.error_message = error_message
        entry.campaign_id = campaign_id or entry.campaign_id
        entry.account_id = account_id or entry.account_id
        entry.blocked_by = blocked_by
        entry.last_seen_at = blocked_at
        entry.resolved_at = None
        entry.details = {**(entry.details or {}), **(details or {})}

    source.access_status = BLACKLISTED_SOURCE_STATUS
    source.comments_enabled = False
    metadata = dict(source.source_metadata or {})
    metadata.update(
        {
            "blacklisted": True,
            "blacklist_reason": reason,
            "blacklisted_at": blocked_at.isoformat(),
            "last_error": {"code": error_code, "message": error_message},
        }
    )
    source.source_metadata = metadata
    return entry


async def resolve_source_blacklist(
    session: AsyncSession,
    *,
    workspace_id: str,
    source: TelegramSource,
    now: datetime | None = None,
) -> TelegramSourceBlacklistEntry | None:
    resolved_at = now or datetime.now(UTC)
    result = await session.execute(
        select(TelegramSourceBlacklistEntry).where(
            TelegramSourceBlacklistEntry.workspace_id == workspace_id,
            TelegramSourceBlacklistEntry.source_id == source.id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry:
        entry.status = RESOLVED_BLACKLIST_STATUS
        entry.resolved_at = resolved_at

    metadata = dict(source.source_metadata or {})
    metadata["blacklisted"] = False
    metadata["blacklist_resolved_at"] = resolved_at.isoformat()
    source.source_metadata = metadata
    source.access_status = "pending_check"
    return entry
