import asyncio
import io
import json
import warnings
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from app.modules.telegram_gateway.contacts import fetch_saved_contacts
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.config import get_settings
from app.core.crypto import encrypt_text
from app.core.errors import bad_request, not_found
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignRun
from app.modules.campaigns.schemas import CampaignResponse, CampaignRunResponse
from app.modules.contact_broadcasts.models import ContactBroadcastMedia, ContactBroadcastSnapshot
from app.modules.contact_broadcasts.scheduling import queue_next_tick
from app.modules.contact_broadcasts.schemas import (
    BroadcastSettingsRequest,
    ContactBroadcastDraftRequest,
    ContactBroadcastPreviewRequest,
)
from app.modules.contact_broadcasts.service import (
    account_for_workspace,
    bulk_allowed,
    create_draft,
    create_preview,
    ensure_can_start,
    media_for_workspace,
    suggested_name,
)
from app.modules.telegram_gateway.contracts import TelegramGatewayError

router = APIRouter()
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@router.post("/accounts/{account_id}/contacts")
async def contacts(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    account = await account_for_workspace(session, workspace_id, account_id)
    try:
        payload = await asyncio.wait_for(fetch_saved_contacts(session, account), timeout=60)
    except TelegramGatewayError as exc:
        raise bad_request(f"Не удалось получить контакты: {exc.code}. {exc}") from exc
    except TimeoutError as exc:
        raise bad_request("Подключение к Telegram не ответило. Проверьте сессию и назначенный прокси.") from exc
    payload["pilot_recipient_id"] = next(
        (
            row["user_id"]
            for row in payload["contacts"]
            if row["user_id"] == get_settings().contact_broadcast_pilot_recipient_tg_id and row["eligible"]
        ),
        None,
    )
    snapshot = ContactBroadcastSnapshot(
        workspace_id=workspace_id,
        account_id=account_id,
        kind="contacts",
        encrypted_payload=encrypt_text(json.dumps(payload, ensure_ascii=False)),
    )
    session.add(snapshot)
    await session.commit()
    return {
        "contacts_snapshot_id": snapshot.id,
        "sender_id": payload["sender_id"],
        "sender_username": payload["sender_username"],
        "caption_limit": payload["caption_limit"],
        "pilot_only": not bulk_allowed(account),
        "pilot_contact_found": bool(payload["pilot_recipient_id"]),
        "contacts": [
            {key: row[key] for key in ("user_id", "username", "first_name", "last_name", "eligible", "reason")}
            | {"suggested_name": suggested_name(row["first_name"], row["last_name"])}
            for row in payload["contacts"]
        ],
    }


def validate_image(data: bytes) -> tuple[str, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise bad_request("Загрузите изображение JPEG или PNG.")
                if image.width * image.height > 40_000_000:
                    raise bad_request("Изображение слишком большое: максимум 40 мегапикселей.")
                extension = "jpg" if image.format == "JPEG" else "png"
                content_type = "image/jpeg" if extension == "jpg" else "image/png"
                image.verify()
        return extension, content_type
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise bad_request("Не удалось прочитать изображение JPEG/PNG.") from exc


@router.post("/media", status_code=201)
async def upload_media(session: SessionDep, workspace_id: WorkspaceIdDep, file: Annotated[UploadFile, File()]):
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise bad_request("Размер изображения должен быть от 1 байта до 10 МБ.")
    extension, content_type = validate_image(data)
    filename = f"{uuid4()}.{extension}"
    path = Path(get_settings().contact_broadcast_media_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    media = ContactBroadcastMedia(workspace_id=workspace_id, filename=filename, content_type=content_type)
    session.add(media)
    try:
        await session.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"media_id": media.id}


@router.get("/media/{media_id}")
async def image(media_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    media, path = await media_for_workspace(session, workspace_id, media_id)
    return FileResponse(path, media_type=media.content_type, headers={"Cache-Control": "private, no-store"})


@router.post("/preview", status_code=201)
async def preview(payload: ContactBroadcastPreviewRequest, session: SessionDep, workspace_id: WorkspaceIdDep):
    return await create_preview(session, workspace_id, payload)


@router.post("/drafts", response_model=CampaignResponse, status_code=201)
async def draft(
    payload: ContactBroadcastDraftRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
):
    return await create_draft(session, workspace_id, current_user.id, payload)


@router.post("/{campaign_id}/start", response_model=CampaignRunResponse)
async def start(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    from app.modules.contact_broadcasts.runner import start as start_run

    campaign = await session.get(Campaign, campaign_id)
    if (
        not campaign
        or campaign.workspace_id != workspace_id
        or campaign.config.get("recipient_mode") != "saved_contacts"
    ):
        raise not_found("Рассылка по сохранённым контактам не найдена.")
    await ensure_can_start(session, campaign)
    run = await start_run(session, workspace_id, campaign)
    if run.status in {"pending", "running"}:
        await queue_next_tick(session, run)
    return run


async def owned_campaign(session, workspace_id, campaign_id):
    campaign = await session.get(Campaign, campaign_id)
    if (
        not campaign
        or campaign.workspace_id != workspace_id
        or campaign.config.get("recipient_mode") != "saved_contacts"
    ):
        raise not_found("Рассылка не найдена.")
    return campaign


@router.get("/{campaign_id}/status")
async def status(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    campaign = await owned_campaign(session, workspace_id, campaign_id)
    run = (
        (
            await session.execute(
                select(CampaignRun)
                .where(CampaignRun.campaign_id == campaign_id)
                .order_by(CampaignRun.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    actions = (
        (
            await session.execute(
                select(CampaignAction)
                .where(CampaignAction.campaign_id == campaign_id)
                .order_by(CampaignAction.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "campaign_id": campaign.id,
        "status": campaign.status,
        "run_id": run.id if run else None,
        "report": run.report if run else {},
        "progress": run.progress if run else {},
        "name": campaign.name,
        "timing": campaign.config.get("timing", {}),
        "exclusions": campaign.config.get("exclusions", ""),
        "recipient_count": len(actions),
        "error": run.error_message if run else None,
        "actions": [
            {
                "recipient_id": action.target_id,
                "recipient": (action.result or {}).get("chat_title"),
                "username": (action.result or {}).get("username"),
                "caption": (action.result or {}).get("message_text"),
                "status": action.status,
                "message_id": (action.result or {}).get("sent_message_id"),
                "outgoing_verified": (action.result or {}).get("send", {}).get("outgoing_verified", False),
                "error": action.error_message,
            }
            for action in actions
        ],
    }


@router.post("/{campaign_id}/stop")
async def stop(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    from app.modules.campaigns.engine import request_stop_campaign_run

    await owned_campaign(session, workspace_id, campaign_id)
    run = (
        (
            await session.execute(
                select(CampaignRun)
                .where(CampaignRun.campaign_id == campaign_id)
                .order_by(CampaignRun.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if run:
        await request_stop_campaign_run(session, workspace_id, run)
    return {"status": "cancelled" if run else "draft"}


@router.get("/{campaign_id}/recipients")
async def recipients_export(
    campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep, scope: str = "selected"
):
    if scope not in {"selected", "sent"}:
        raise bad_request("Неизвестный список получателей.")
    campaign = await owned_campaign(session, workspace_id, campaign_id)
    from app.modules.contact_broadcasts.service import load_campaign_payload

    _, payload = await load_campaign_payload(session, campaign)
    query = select(CampaignAction).where(CampaignAction.campaign_id == campaign.id)
    if scope == "sent":
        query = query.where(CampaignAction.status.in_(("succeeded", "sent_unverified")))
    actions = (await session.execute(query.order_by(CampaignAction.created_at))).scalars().all()
    by_id = {row["user_id"]: row for row in payload["recipients"]}
    # Include Telegram ID even without username so exclusions do not silently miss contacts.
    items = [
        "@" + by_id[a.target_id]["username"] if by_id[a.target_id].get("username") else "id:" + a.target_id
        for a in actions
        if a.status != "skipped"
    ]
    return {"scope": scope, "count": len(items), "text": "\n".join(items)}


@router.post("/{campaign_id}/pause")
async def pause(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    from app.modules.contact_broadcasts.runner import event

    campaign = await owned_campaign(session, workspace_id, campaign_id)
    await session.execute(select(Campaign.id).where(Campaign.id == campaign.id).with_for_update())
    run = (
        await session.execute(
            select(CampaignRun)
            .where(CampaignRun.campaign_id == campaign.id)
            .order_by(CampaignRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not run or run.status not in {"pending", "running"}:
        raise bad_request("Приостановить можно только выполняющуюся кампанию.")
    run.status = campaign.status = "paused"
    run.progress = {**(run.progress or {}), "pause_reason": "manual", "tick_token": None, "tick_scheduled_for": None}
    event(session, campaign, run, "Кампания приостановлена пользователем. Текущая отправка может завершиться.")
    await session.commit()
    return {"status": "paused"}


@router.post("/{campaign_id}/resume")
async def resume(campaign_id: str, session: SessionDep, workspace_id: WorkspaceIdDep):
    from app.modules.contact_broadcasts.service import ensure_can_start

    campaign = await owned_campaign(session, workspace_id, campaign_id)
    await session.execute(select(Campaign.id).where(Campaign.id == campaign.id).with_for_update())
    run = (
        await session.execute(
            select(CampaignRun)
            .where(CampaignRun.campaign_id == campaign.id)
            .order_by(CampaignRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not run or run.status != "paused":
        raise bad_request("Продолжить можно только приостановленную кампанию.")
    uncertain = (
        await session.execute(
            select(CampaignAction.id).where(
                CampaignAction.campaign_id == campaign.id, CampaignAction.status.in_(("sending", "unknown"))
            )
        )
    ).first()
    if uncertain:
        raise bad_request(
            "Есть текущая или неопределённая отправка. "
            "Сначала проверьте исходящий диалог; автоматический повтор запрещён."
        )
    await ensure_can_start(session, campaign)
    run.status = campaign.status = "running"
    run.progress = {**(run.progress or {}), "pause_reason": None}
    await session.commit()
    await queue_next_tick(session, run)
    return {"status": "running"}


@router.patch("/{campaign_id}/settings")
async def settings_update(
    campaign_id: str, payload: BroadcastSettingsRequest, session: SessionDep, workspace_id: WorkspaceIdDep
):
    campaign = await owned_campaign(session, workspace_id, campaign_id)
    await session.execute(select(Campaign.id).where(Campaign.id == campaign.id).with_for_update())
    if campaign.status not in {"draft", "paused"}:
        raise bad_request("Для изменения задержек сначала приостановите кампанию.")
    campaign.config = {**campaign.config, "timing": payload.timing.model_dump()}
    if payload.exclusions is not None:
        from app.modules.contact_broadcasts.service import exclusion_keys, load_campaign_payload

        keys = exclusion_keys(payload.exclusions)
        _, frozen = await load_campaign_payload(session, campaign)
        by_id = {row["user_id"]: row for row in frozen["recipients"]}
        actions = (
            (
                await session.execute(
                    select(CampaignAction).where(
                        CampaignAction.campaign_id == campaign.id, CampaignAction.status.in_(("prepared", "queued"))
                    )
                )
            )
            .scalars()
            .all()
        )
        for action in actions:
            if action.target_id in keys or (by_id[action.target_id].get("username") or "").lower() in keys:
                action.status = "skipped"
                action.error_message = "Исключён пользователем до отправки."
        campaign.config = {**campaign.config, "exclusions": payload.exclusions}
    campaign.policy = {
        **campaign.policy,
        "daily_limit_per_account": payload.timing.daily_limit,
        "delay_seconds_min": payload.timing.delay_min,
        "delay_seconds_max": payload.timing.delay_max,
    }
    await session.commit()
    return {"timing": payload.timing.model_dump()}
