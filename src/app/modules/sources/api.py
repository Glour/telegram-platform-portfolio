import re
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlparse

from app.modules.telegram_gateway.service import (
    TelegramGatewayError,
    inspect_source_with_account,
    real_telegram_enabled,
    source_ref_from_fields,
)
from fastapi import APIRouter, Query, Response, status
from sqlalchemy import case, delete, func, select, update

from app.api.deps import SessionDep, WorkspaceIdDep
from app.core.errors import bad_request, not_found
from app.modules.campaigns.models import Campaign, CampaignTarget
from app.modules.channel_map.service import atlas_channel_metadata_by_refs, load_atlas
from app.modules.comments.models import TelegramComment
from app.modules.operations.service import record_operational_event
from app.modules.sources.blacklist import blacklist_source, resolve_source_blacklist
from app.modules.sources.models import (
    TelegramMessage,
    TelegramSource,
    TelegramSourceBlacklistEntry,
    TelegramSourceFolder,
)
from app.modules.sources.safety import source_metadata, source_safety
from app.modules.sources.schemas import (
    MessageResponse,
    SourceBlacklistEntryResponse,
    SourceBlacklistRequest,
    SourceBulkArchiveInvalidRequest,
    SourceBulkArchiveInvalidResponse,
    SourceBulkCreate,
    SourceBulkCreateResponse,
    SourceCreate,
    SourceFolderAssignRequest,
    SourceFolderCreate,
    SourceFolderPatch,
    SourceFolderResponse,
    SourcePatch,
    SourceResponse,
    SourceSafetySummaryResponse,
)
from app.modules.telegram_accounts.models import TelegramAccount, TelegramSession

router = APIRouter()

SOURCE_INVALID_STATUS_BY_REASON = {
    "source_comments_not_available": "invalid_comments_closed",
    "source_not_accessible": "invalid_private",
    "discussion_join_approval_required": "invalid_join_request",
    "account_banned_in_source": "invalid_account_banned",
    "account_cannot_write_comments": "invalid_write_forbidden",
    "source_not_found": "invalid_not_found",
    "source_not_channel": "invalid_not_channel",
}

SOURCE_MANUAL_ACCESS_STATUSES = {
    "available",
    "pending_check",
    "invalid_comments_closed",
    "invalid_private",
    "invalid_join_request",
    "invalid_account_banned",
    "invalid_write_forbidden",
    "invalid_not_found",
    "invalid_not_channel",
    "failed",
    "archived",
}


@router.get("", response_model=list[SourceResponse])
async def list_sources(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    type: Annotated[str | None, Query()] = None,
    comments_enabled: Annotated[bool | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    include_blacklisted: Annotated[bool, Query()] = False,
    folder_id: Annotated[str | None, Query()] = None,
) -> list[TelegramSource]:
    statement = select(TelegramSource).where(TelegramSource.workspace_id == workspace_id)
    if not include_archived:
        statement = statement.where(TelegramSource.access_status != "archived")
    if not include_blacklisted:
        statement = statement.where(TelegramSource.access_status != "blacklisted")
    if type:
        statement = statement.where(TelegramSource.type == type)
    if comments_enabled is not None:
        statement = statement.where(TelegramSource.comments_enabled.is_(comments_enabled))
    if folder_id is not None:
        statement = statement.where(TelegramSource.folder_id == (folder_id or None))
    result = await session.execute(statement.order_by(TelegramSource.created_at.desc()))
    sources = list(result.scalars().all())
    await _backfill_channel_map_metadata(session, sources)
    return sources


@router.post("", response_model=SourceResponse, status_code=201)
async def create_source(payload: SourceCreate, session: SessionDep, workspace_id: WorkspaceIdDep) -> TelegramSource:
    if payload.folder_id:
        await _get_source_folder(session, workspace_id, payload.folder_id)
    source = TelegramSource(
        workspace_id=workspace_id,
        folder_id=payload.folder_id,
        type=payload.type,
        username=payload.username,
        url=payload.url,
        title=payload.title or payload.username or payload.url,
        access_status="pending_check",
    )
    session.add(source)
    await record_operational_event(
        session,
        workspace_id=workspace_id,
        severity="info",
        category="product",
        event_type="source_added",
        title="Добавлен источник",
        message="В кабинет добавлен новый Telegram-источник.",
        source="sources.api",
        entity_type="telegram_source",
        entity_id=source.id,
        payload={"source_type": source.type},
    )
    await session.commit()
    await session.refresh(source)
    return source


@router.post("/bulk", response_model=SourceBulkCreateResponse, status_code=201)
async def bulk_create_sources(
    payload: SourceBulkCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict:
    refs = _split_source_refs(payload.refs)
    if not refs:
        raise bad_request("At least one source ref is required")
    if payload.folder_id:
        await _get_source_folder(session, workspace_id, payload.folder_id)

    created = 0
    reused = 0
    items = []
    seen: dict[tuple[str, str], TelegramSource] = {}
    channel_metadata_by_ref = await _channel_map_metadata_for_refs(refs, payload.type)

    for ref in refs:
        username, url, title = _normalize_source_ref(ref)
        if not username and not url:
            continue
        channel_map_metadata = (
            channel_metadata_by_ref.get(_comparable_source_ref(ref))
            or channel_metadata_by_ref.get(_comparable_source_ref(username or ""))
            or channel_metadata_by_ref.get(_comparable_source_ref(url or ""))
        )

        key = ("username", username) if username else ("url", url or "")
        source = seen.get(key)
        status = "reused" if source else "created"

        if source is None:
            source = await _find_existing_source(session, workspace_id, username, url)
            if source and source.access_status == "archived":
                status = "archived"
            elif source and source.access_status == "blacklisted":
                status = "blacklisted"
            elif source:
                status = "reused"
            else:
                source = TelegramSource(
                    workspace_id=workspace_id,
                    folder_id=payload.folder_id,
                    type=payload.type,
                    username=username,
                    url=url,
                    title=title,
                    access_status="pending_check",
                    source_metadata={"channel_map": channel_map_metadata} if channel_map_metadata else {},
                )
                session.add(source)
                await session.flush()
                status = "created"
        elif payload.folder_id and source.folder_id != payload.folder_id:
            source.folder_id = payload.folder_id
        if channel_map_metadata and not _has_channel_map_metadata(source):
            source.source_metadata = {**(source.source_metadata or {}), "channel_map": channel_map_metadata}

        seen[key] = source
        if status == "created":
            created += 1
        else:
            reused += 1
        items.append({"ref": ref, "status": status, "source": source})

    if not items:
        raise bad_request("At least one valid source ref is required")

    if created:
        await record_operational_event(
            session,
            workspace_id=workspace_id,
            severity="info",
            category="product",
            event_type="source_added",
            title="Добавлены источники",
            message=f"В кабинет добавлено источников: {created}.",
            source="sources.api",
            entity_type="telegram_source",
            payload={"created": created, "reused": reused, "source_type": payload.type},
        )

    await session.commit()
    for item in items:
        await session.refresh(item["source"])
    return {"created": created, "reused": reused, "items": items}


@router.get("/folders", response_model=list[SourceFolderResponse])
async def list_source_folders(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[SourceFolderResponse]:
    rows = await session.execute(
        select(TelegramSourceFolder, func.count(TelegramSource.id).label("sources_count"))
        .outerjoin(
            TelegramSource,
            (TelegramSource.folder_id == TelegramSourceFolder.id)
            & (TelegramSource.workspace_id == TelegramSourceFolder.workspace_id)
            & (TelegramSource.access_status != "archived"),
        )
        .where(TelegramSourceFolder.workspace_id == workspace_id)
        .group_by(TelegramSourceFolder.id)
        .order_by(TelegramSourceFolder.sort_order.asc(), TelegramSourceFolder.created_at.desc())
    )
    return [
        SourceFolderResponse.model_validate(folder).model_copy(update={"sources_count": int(count or 0)})
        for folder, count in rows.all()
    ]


@router.post("/folders", response_model=SourceFolderResponse, status_code=201)
async def create_source_folder(
    payload: SourceFolderCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> SourceFolderResponse:
    name = " ".join(payload.name.split()).strip()
    existing = await session.execute(
        select(TelegramSourceFolder).where(
            TelegramSourceFolder.workspace_id == workspace_id,
            func.lower(TelegramSourceFolder.name) == name.lower(),
        )
    )
    if existing.scalar_one_or_none():
        raise bad_request("Source folder with this name already exists")
    folder = TelegramSourceFolder(
        workspace_id=workspace_id,
        name=name,
        description=payload.description,
        color=payload.color,
        sort_order=payload.sort_order,
    )
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return SourceFolderResponse.model_validate(folder)


@router.patch("/folders/{folder_id}", response_model=SourceFolderResponse)
async def update_source_folder(
    folder_id: str,
    payload: SourceFolderPatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> SourceFolderResponse:
    folder = await _get_source_folder(session, workspace_id, folder_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        name = " ".join(str(data["name"]).split()).strip()
        if not name:
            raise bad_request("Folder name may not be empty")
        folder.name = name
    for key in ["description", "color", "sort_order"]:
        if key in data:
            setattr(folder, key, data[key])
    await session.commit()
    await session.refresh(folder)
    count = await _source_folder_count(session, workspace_id, folder.id)
    return SourceFolderResponse.model_validate(folder).model_copy(update={"sources_count": count})


@router.post("/folders/{folder_id}/sources", response_model=list[SourceResponse])
async def assign_sources_to_folder(
    folder_id: str,
    payload: SourceFolderAssignRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> list[TelegramSource]:
    folder = await _get_source_folder(session, workspace_id, folder_id)
    sources = await _get_workspace_sources(session, workspace_id, payload.source_ids)
    await session.execute(
        update(TelegramSource)
        .where(TelegramSource.workspace_id == workspace_id, TelegramSource.id.in_([source.id for source in sources]))
        .values(folder_id=folder.id)
    )
    await session.commit()
    result = await session.execute(
        select(TelegramSource).where(TelegramSource.id.in_([source.id for source in sources]))
    )
    return list(result.scalars().all())


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_source_folder(folder_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Response:
    folder = await _get_source_folder(session, workspace_id, folder_id)
    await session.execute(
        update(TelegramSource)
        .where(TelegramSource.workspace_id == workspace_id, TelegramSource.folder_id == folder.id)
        .values(folder_id=None)
    )
    await session.delete(folder)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/safety-summary", response_model=SourceSafetySummaryResponse)
async def safety_summary(session: SessionDep, workspace_id: WorkspaceIdDep) -> dict[str, int]:
    result = await session.execute(select(TelegramSource).where(TelegramSource.workspace_id == workspace_id))
    summary = {
        "total": 0,
        "safe": 0,
        "medium": 0,
        "high": 0,
        "critical": 0,
        "unknown": 0,
        "invalid": 0,
        "archived": 0,
        "blacklisted": 0,
        "comments_closed": 0,
        "private": 0,
        "join_request": 0,
        "write_forbidden": 0,
        "not_found": 0,
        "not_channel": 0,
    }
    for source in result.scalars().all():
        safety = source_safety(source)
        summary["total"] += 1
        risk_level = str(safety["source_risk_level"])
        if risk_level == "low":
            summary["safe"] += 1
        elif risk_level in {"medium", "high", "critical", "unknown"}:
            summary[risk_level] += 1
        if source.access_status == "archived":
            summary["archived"] += 1
        if source.access_status == "blacklisted":
            summary["blacklisted"] += 1
        if source.access_status.startswith("invalid_") or source_metadata(source).get("invalid") is True:
            summary["invalid"] += 1
        reason = str(source_metadata(source).get("invalid_reason") or "")
        if reason == "source_comments_not_available" or source.access_status == "invalid_comments_closed":
            summary["comments_closed"] += 1
        elif reason == "source_not_accessible" or source.access_status == "invalid_private":
            summary["private"] += 1
        elif reason == "discussion_join_approval_required" or source.access_status == "invalid_join_request":
            summary["join_request"] += 1
        elif reason == "account_cannot_write_comments" or source.access_status == "invalid_write_forbidden":
            summary["write_forbidden"] += 1
        elif reason == "source_not_found" or source.access_status == "invalid_not_found":
            summary["not_found"] += 1
        elif reason == "source_not_channel" or source.access_status == "invalid_not_channel":
            summary["not_channel"] += 1
    return summary


@router.get("/blacklist", response_model=list[SourceBlacklistEntryResponse])
async def list_source_blacklist(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    include_resolved: Annotated[bool, Query()] = False,
) -> list[dict]:
    statement = select(TelegramSourceBlacklistEntry).where(TelegramSourceBlacklistEntry.workspace_id == workspace_id)
    if not include_resolved:
        statement = statement.where(TelegramSourceBlacklistEntry.status == "active")
    result = await session.execute(statement.order_by(TelegramSourceBlacklistEntry.last_seen_at.desc()))
    entries = list(result.scalars().all())
    if not entries:
        return []
    sources_result = await session.execute(
        select(TelegramSource).where(TelegramSource.id.in_([entry.source_id for entry in entries]))
    )
    sources_by_id = {source.id: source for source in sources_result.scalars().all()}
    return [{**entry.__dict__, "source": sources_by_id.get(entry.source_id)} for entry in entries]


@router.post("/{source_id}/blacklist", response_model=SourceBlacklistEntryResponse)
async def block_source(
    source_id: str,
    payload: SourceBlacklistRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")
    entry = await blacklist_source(
        session,
        workspace_id=workspace_id,
        source=source,
        reason=payload.reason,
        error_code=payload.error_code,
        error_message=payload.error_message,
        blocked_by="user",
        details=payload.details,
    )
    await _remove_source_from_active_campaigns(session, workspace_id, source)
    await session.commit()
    await session.refresh(entry)
    await session.refresh(source)
    return {**entry.__dict__, "source": source}


@router.delete("/{source_id}/blacklist", response_model=SourceBlacklistEntryResponse | None)
async def unblock_source(
    source_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> dict | None:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")
    entry = await resolve_source_blacklist(session, workspace_id=workspace_id, source=source)
    await session.commit()
    if entry:
        await session.refresh(entry)
        await session.refresh(source)
        return {**entry.__dict__, "source": source}
    return None


@router.post("/bulk-archive-invalid", response_model=SourceBulkArchiveInvalidResponse)
async def bulk_archive_invalid(
    payload: SourceBulkArchiveInvalidRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> SourceBulkArchiveInvalidResponse:
    statement = select(TelegramSource).where(TelegramSource.workspace_id == workspace_id)
    if payload.source_ids is not None:
        source_ids = list(dict.fromkeys(payload.source_ids))
        if not source_ids:
            raise bad_request("source_ids must not be empty")
        statement = statement.where(TelegramSource.id.in_(source_ids))
    result = await session.execute(statement)
    archived_ids: list[str] = []
    for source in result.scalars().all():
        safety = source_safety(source)
        if safety["source_risk_level"] != "critical":
            continue
        if source.access_status == "archived":
            continue
        source.access_status = "archived"
        metadata = dict(source.source_metadata or {})
        metadata["archived_by_safety"] = True
        metadata["archived_reason"] = safety["reasons"][0] if safety["reasons"] else "source_risk_critical"
        source.source_metadata = metadata
        archived_ids.append(source.id)
    await session.commit()
    return SourceBulkArchiveInvalidResponse(status="accepted", archived=len(archived_ids), source_ids=archived_ids)


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: str,
    payload: SourcePatch,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramSource:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")

    data = payload.model_dump(exclude_unset=True)
    if "access_status" in data and data["access_status"] is not None:
        status_value = str(data["access_status"])
        if status_value not in SOURCE_MANUAL_ACCESS_STATUSES:
            raise bad_request(f"Unsupported source status: {status_value}")
        source.access_status = status_value

    if "comments_enabled" in data and data["comments_enabled"] is not None:
        source.comments_enabled = bool(data["comments_enabled"])

    metadata = dict(source.source_metadata or {})
    if source.access_status == "available":
        metadata.update(
            {
                "invalid": False,
                "invalid_reason": None,
                "last_error": None,
                "manual_status_override": True,
                "manual_status_override_at": datetime.now(UTC).isoformat(),
            }
        )
        metadata.pop("blacklisted", None)
        metadata.pop("blacklist_reason", None)
    elif source.access_status.startswith("invalid_"):
        metadata.update(
            {
                "invalid": True,
                "invalid_reason": _manual_invalid_reason_for_status(source.access_status),
                "manual_status_override": True,
                "manual_status_override_at": datetime.now(UTC).isoformat(),
            }
        )
    elif source.access_status in {"pending_check", "failed"}:
        metadata["manual_status_override"] = True
        metadata["manual_status_override_at"] = datetime.now(UTC).isoformat()

    source.source_metadata = metadata
    await session.commit()
    await session.refresh(source)
    return source


@router.post("/{source_id}/check-comments", response_model=SourceResponse)
async def check_comments(source_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> TelegramSource:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")

    if source.access_status == "archived":
        raise bad_request(
            "Source is archived after a critical Telegram access error and is hidden from campaign selection"
        )

    if real_telegram_enabled():
        account = await _first_telegram_account(session, workspace_id)
        try:
            info = await inspect_source_with_account(
                session=session,
                account=account,
                source_ref=source_ref_from_fields(source.username, source.url, source.tg_id),
            )
        except TelegramGatewayError as exc:
            invalid_reason = _source_invalid_reason_from_error(exc)
            if invalid_reason:
                _mark_source_invalid(
                    source,
                    reason=invalid_reason,
                    error_code=exc.code,
                    error_message=str(exc),
                )
                await session.commit()
                await session.refresh(source)
                return source

            source.access_status = "failed"
            source.source_metadata = {
                **(source.source_metadata or {}),
                "last_error": {"code": exc.code, "message": str(exc)},
            }
            await session.commit()
            raise bad_request(f"{exc.code}: {exc}") from exc

        source.tg_id = str(info.tg_id) if info.tg_id else source.tg_id
        source.type = info.source_type or source.type
        source.username = info.username or source.username
        source.title = info.title or source.title
        source.comments_enabled = info.comments_enabled
        source.linked_discussion_tg_id = str(info.linked_discussion_tg_id) if info.linked_discussion_tg_id else None
        source.linked_discussion_username = info.linked_discussion_username
        source.source_metadata = {**(source.source_metadata or {}), **info.metadata}
        if source.type != "channel":
            _mark_source_invalid(
                source,
                reason="source_not_channel",
                error_code="SOURCE_NOT_CHANNEL",
                error_message="Источник не является каналом Telegram.",
            )
        elif info.comments_enabled and info.metadata.get("linked_discussion_resolved") is False:
            _mark_source_invalid(
                source,
                reason="discussion_join_approval_required",
                error_code="DISCUSSION_NOT_ACCESSIBLE",
                error_message="У канала есть discussion, но аккаунт не может открыть его без вступления или заявки.",
            )
        elif not source.comments_enabled:
            _mark_source_invalid(
                source,
                reason="source_comments_not_available",
                error_code="COMMENTS_CLOSED",
                error_message="У канала нет доступного обсуждения для комментариев.",
            )
        else:
            source.access_status = "available"
            source.source_metadata = {
                **(source.source_metadata or {}),
                "invalid": False,
                "invalid_reason": None,
                "last_error": None,
            }
    else:
        source.comments_enabled = True
        source.access_status = "available"
    await session.commit()
    await session.refresh(source)
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Response:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")

    campaigns_result = await session.execute(
        select(Campaign).where(Campaign.workspace_id == workspace_id, Campaign.status != "deleted")
    )
    for campaign in campaigns_result.scalars().all():
        config = dict(campaign.config or {})
        source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
        if source_id in source_ids:
            config["source_ids"] = [item for item in source_ids if item != source_id]
            campaign.config = config

    message_ids_result = await session.execute(
        select(TelegramMessage.id).where(
            TelegramMessage.workspace_id == workspace_id,
            TelegramMessage.source_id == source_id,
        )
    )
    message_ids = list(message_ids_result.scalars().all())
    await session.execute(
        delete(TelegramComment).where(
            TelegramComment.workspace_id == workspace_id,
            TelegramComment.source_id == source_id,
        )
    )
    if message_ids:
        await session.execute(delete(CampaignTarget).where(CampaignTarget.message_id.in_(message_ids)))
    await session.execute(delete(CampaignTarget).where(CampaignTarget.source_id == source_id))
    await session.execute(
        delete(TelegramMessage).where(
            TelegramMessage.workspace_id == workspace_id,
            TelegramMessage.source_id == source_id,
        )
    )
    await session.delete(source)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{source_id}/messages", response_model=list[MessageResponse])
async def messages(source_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> list[TelegramMessage]:
    source = await session.get(TelegramSource, source_id)
    if not source or source.workspace_id != workspace_id:
        raise not_found("Source not found")
    result = await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.workspace_id == workspace_id, TelegramMessage.source_id == source_id)
        .order_by(TelegramMessage.published_at.desc(), TelegramMessage.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


async def _first_telegram_account(session: SessionDep, workspace_id: str) -> TelegramAccount:
    status_rank = case(
        (TelegramAccount.status == "active", 0),
        (TelegramAccount.status == "warming", 1),
        (TelegramAccount.status == "new", 2),
        else_=9,
    )
    result = await session.execute(
        select(TelegramAccount)
        .join(TelegramSession, TelegramSession.account_id == TelegramAccount.id)
        .where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.status.in_(["active", "warming", "new"]))
        .order_by(status_rank, TelegramAccount.health_score.desc(), TelegramAccount.created_at.asc())
        .limit(1)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise bad_request("Telegram account is required to check source comments in real mode")
    return account


def _split_source_refs(refs: str | list[str]) -> list[str]:
    if isinstance(refs, str):
        raw_refs = re.split(r"[\s,;]+", refs)
    else:
        raw_refs = []
        for ref in refs:
            raw_refs.extend(re.split(r"[\s,;]+", ref))
    return [ref.strip() for ref in raw_refs if ref and ref.strip()]


async def _find_existing_source(
    session: SessionDep,
    workspace_id: str,
    username: str | None,
    url: str | None,
) -> TelegramSource | None:
    if username:
        result = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                func.lower(TelegramSource.username) == username.lower(),
            )
        )
        source = result.scalar_one_or_none()
        if source:
            return source

    if url:
        result = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.url == url,
            )
        )
        return result.scalar_one_or_none()

    return None


def _normalize_source_ref(ref: str) -> tuple[str | None, str | None, str]:
    value = ref.strip()
    if value.startswith("@"):
        username = _clean_username(value[1:])
        return username, None, username or value

    parsed = urlparse(value if "://" in value else f"https://{value}" if value.startswith("t.me/") else value)
    host = (parsed.netloc or "").lower()
    if host in {"t.me", "telegram.me"}:
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts and not path_parts[0].startswith("+") and path_parts[0] != "joinchat":
            username = _clean_username(path_parts[0])
            return username, None, username or value
        normalized_url = f"https://{host}{parsed.path}".rstrip("/")
        return None, normalized_url, normalized_url

    if "://" in value:
        normalized_url = value.rstrip("/")
        return None, normalized_url, normalized_url

    username = _clean_username(value)
    return username, None, username or value


def _clean_username(value: str) -> str | None:
    username = value.strip().removeprefix("@").strip("/")
    if not username:
        return None
    return username.lower()


def _mark_source_invalid(
    source: TelegramSource,
    *,
    reason: str,
    error_code: str,
    error_message: str,
) -> None:
    source.comments_enabled = False
    source.access_status = SOURCE_INVALID_STATUS_BY_REASON.get(reason, "invalid")
    metadata = dict(source.source_metadata or {})
    metadata.update(
        {
            "invalid": True,
            "invalid_reason": reason,
            "last_error": {"code": error_code, "message": error_message},
        }
    )
    source.source_metadata = metadata


def _manual_invalid_reason_for_status(access_status: str) -> str:
    reasons = {
        "invalid_comments_closed": "source_comments_not_available",
        "invalid_private": "source_not_accessible",
        "invalid_join_request": "discussion_join_approval_required",
        "invalid_account_banned": "account_banned_in_source",
        "invalid_write_forbidden": "account_cannot_write_comments",
        "invalid_not_found": "source_not_found",
        "invalid_not_channel": "source_not_channel",
    }
    return reasons.get(access_status, access_status)


def _source_metadata(source: TelegramSource) -> dict:
    return source_metadata(source)


def _source_invalid_reason_from_error(exc: TelegramGatewayError) -> str | None:
    text = f"{exc.code} {exc}".lower()
    if exc.code == "TELEGRAM_CONNECTION_FAILED":
        return None
    if "inviterequestsent" in text:
        return "discussion_join_approval_required"
    if any(marker in text for marker in ["userbannedinchannel", "banned from it"]):
        return "account_banned_in_source"
    if any(
        marker in text
        for marker in [
            "chatwriteforbidden",
            "comment_send_forbidden",
            "write forbidden",
            "lack permission",
            "not return a sent comment",
        ]
    ):
        return "account_cannot_write_comments"
    if any(marker in text for marker in ["channelprivate", "private", "invitehash", "usernotparticipant"]):
        return "source_not_accessible"
    if any(marker in text for marker in ["usernameinvalid", "no user has", "could not find", "cannot find"]):
        return "source_not_found"
    return None


async def _remove_source_from_active_campaigns(
    session: SessionDep,
    workspace_id: str,
    source: TelegramSource,
) -> None:
    campaigns_result = await session.execute(
        select(Campaign).where(Campaign.workspace_id == workspace_id, Campaign.status.notin_(["archived", "deleted"]))
    )
    for campaign in campaigns_result.scalars().all():
        config = dict(campaign.config or {})
        source_ids = [str(item) for item in config.get("source_ids", []) if isinstance(item, str)]
        if source.id not in source_ids:
            continue
        config["source_ids"] = [source_id for source_id in source_ids if source_id != source.id]
        blocked_sources = dict(config.get("blocked_sources") or {})
        blocked_sources[source.id] = {
            **dict(blocked_sources.get(source.id) or {}),
            "source_id": source.id,
            "title": source.title,
            "username": source.username,
            "reason": "manual_blacklist",
        }
        config["blocked_sources"] = blocked_sources
        campaign.config = config


async def _get_source_folder(session: SessionDep, workspace_id: str, folder_id: str) -> TelegramSourceFolder:
    folder = await session.get(TelegramSourceFolder, folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise not_found("Source folder not found")
    return folder


async def _get_workspace_sources(session: SessionDep, workspace_id: str, source_ids: list[str]) -> list[TelegramSource]:
    result = await session.execute(
        select(TelegramSource).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.id.in_(source_ids),
        )
    )
    sources_by_id = {source.id: source for source in result.scalars().all()}
    if len(sources_by_id) != len(set(source_ids)):
        raise not_found("One or more sources not found")
    return [sources_by_id[source_id] for source_id in source_ids if source_id in sources_by_id]


async def _source_folder_count(session: SessionDep, workspace_id: str, folder_id: str) -> int:
    result = await session.execute(
        select(func.count(TelegramSource.id)).where(
            TelegramSource.workspace_id == workspace_id,
            TelegramSource.folder_id == folder_id,
            TelegramSource.access_status != "archived",
        )
    )
    return int(result.scalar_one() or 0)


async def _channel_map_metadata_for_refs(refs: list[str], source_type: str) -> dict[str, dict]:
    if source_type != "channel":
        return {}
    try:
        atlas = await load_atlas()
    except Exception:
        return {}
    return atlas_channel_metadata_by_refs(atlas, refs)


async def _backfill_channel_map_metadata(session: SessionDep, sources: list[TelegramSource]) -> None:
    missing_sources = [
        source
        for source in sources
        if source.type == "channel"
        and not _has_channel_map_metadata(source)
        and (source.username or source.url or source.tg_id)
    ]
    if not missing_sources:
        return
    refs = [source_ref_from_fields(source.username, source.url, source.tg_id) for source in missing_sources]
    metadata_by_ref = await _channel_map_metadata_for_refs(refs, "channel")
    changed = False
    for source, ref in zip(missing_sources, refs, strict=False):
        channel_map_metadata = (
            metadata_by_ref.get(_comparable_source_ref(ref))
            or metadata_by_ref.get(_comparable_source_ref(source.username or ""))
            or metadata_by_ref.get(_comparable_source_ref(source.url or ""))
            or metadata_by_ref.get(_comparable_source_ref(source.tg_id or ""))
        )
        if channel_map_metadata:
            source.source_metadata = {**(source.source_metadata or {}), "channel_map": channel_map_metadata}
            changed = True
    if changed:
        await session.commit()


def _has_channel_map_metadata(source: TelegramSource) -> bool:
    metadata = source.source_metadata if isinstance(source.source_metadata, dict) else {}
    channel_map = metadata.get("channel_map")
    return isinstance(channel_map, dict) and bool(
        channel_map.get("sub_cluster_label") or channel_map.get("cluster_label")
    )


def _comparable_source_ref(value: str) -> str:
    ref = value.strip().lower()
    if ref.startswith("https://t.me/"):
        ref = ref.removeprefix("https://t.me/")
    elif ref.startswith("http://t.me/"):
        ref = ref.removeprefix("http://t.me/")
    elif ref.startswith("t.me/"):
        ref = ref.removeprefix("t.me/")
    return ref.strip("/").lstrip("@")
