import copy
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Formatter

from sqlalchemy import and_, or_, select

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.errors import bad_request, not_found
from app.core.text import utf16_length
from app.modules.campaigns.models import Campaign, CampaignAction
from app.modules.contact_broadcasts.models import ContactBroadcastMedia, ContactBroadcastSnapshot
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility


def suggested_name(first_name: str, last_name: str = "") -> str:
    # Telegram first_name may itself contain surname, client number or emoji.
    # Never append last_name or merge tokens across digits/punctuation.
    match = re.search(r"[^\W\d_]+(?:[-‐‑][^\W\d_]+)*", first_name)
    return match.group() if match else ""


def render_caption(template: str, name: str = "", *, fallback: bool = False) -> str:
    name = suggested_name(name)
    if not template.strip():
        raise ValueError("Заполните текст сообщения без имени." if fallback else "Заполните текст сообщения.")
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None:
                if field != "name" or spec or conversion or fallback:
                    raise ValueError("Поддерживается только {name}; в тексте без имени подстановки запрещены.")
                if not name:
                    raise ValueError("Укажите имя или выберите текст без имени.")
        return re.sub(r",[^\S\r\n]*(?=\S)", ", ", template.format(name=name))
    except (KeyError, IndexError, AttributeError) as exc:
        raise ValueError("Некорректная подстановка в тексте.") from exc


def unpack(snapshot: ContactBroadcastSnapshot) -> dict:
    return json.loads(decrypt_text(snapshot.encrypted_payload) or "{}")


async def account_for_workspace(session, workspace_id: str, account_id: str):
    account = await session.get(TelegramAccount, account_id)
    if not account or account.workspace_id != workspace_id:
        raise not_found("Аккаунт не найден в этом воркспейсе.")
    return account


async def snapshot_for_workspace(session, workspace_id: str, snapshot_id: str, kind: str):
    snapshot = await session.get(ContactBroadcastSnapshot, snapshot_id)
    if not snapshot or snapshot.workspace_id != workspace_id or snapshot.kind != kind:
        raise not_found("Снимок контактов или предпросмотр не найден.")
    return snapshot


def ensure_fresh(snapshot, hours: int):
    created = snapshot.created_at.replace(tzinfo=UTC) if snapshot.created_at.tzinfo is None else snapshot.created_at
    if datetime.now(UTC) - created > timedelta(hours=hours):
        raise bad_request("Обновите контакты и предпросмотр: сохранённые данные устарели.")


async def media_for_workspace(session, workspace_id: str, media_id: str):
    media = await session.get(ContactBroadcastMedia, media_id)
    if not media or media.workspace_id != workspace_id:
        raise not_found("Изображение не найдено.")
    path = Path(get_settings().contact_broadcast_media_dir) / media.filename
    if not path.is_file():
        raise bad_request("Изображение недоступно в хранилище.")
    return media, path


def bulk_allowed(account) -> bool:
    settings = get_settings()
    return settings.contact_broadcast_bulk_enabled and (
        not settings.contact_broadcast_bulk_workspace_ids
        or account.workspace_id in settings.contact_broadcast_bulk_workspace_ids
    )


def pilot_allowed(account, payload: dict) -> bool:
    settings = get_settings()
    if bulk_allowed(account):
        return True
    recipients = payload.get("recipients", [])
    return bool(
        settings.contact_broadcast_pilot_account_id
        and account.id == settings.contact_broadcast_pilot_account_id
        and bool(settings.contact_broadcast_pilot_sender_tg_id)
        and payload.get("sender_id") == settings.contact_broadcast_pilot_sender_tg_id
        and payload.get("pilot_recipient_id") == settings.contact_broadcast_pilot_recipient_tg_id
        and bool(settings.contact_broadcast_pilot_recipient_tg_id)
        and payload.get("pilot_recipient_id")
        and len(recipients) == 1
        and recipients[0]["user_id"] == payload["pilot_recipient_id"]
    )


def exclusion_keys(raw: str) -> set[str]:
    keys = set()
    for token in re.split(r"[\s,;]+", raw):
        token = re.sub(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", "", token, flags=re.I)
        token = token.partition("?")[0].strip("/@").lower()
        if token.startswith("id:"):
            token = token[3:]
        if token:
            keys.add(token)
    return keys


async def reserved_recipient_ids(session, workspace_id, campaign_id=None, include_sent=True):
    query = (
        select(CampaignAction.target_id)
        .join(Campaign, Campaign.id == CampaignAction.campaign_id)
        .where(
            Campaign.workspace_id == workspace_id,
            CampaignAction.target_type == "saved_contact",
            or_(
                CampaignAction.status.in_(
                    ("sending", "unknown", "succeeded", "sent_unverified") if include_sent else ("sending", "unknown")
                ),
                and_(CampaignAction.status == "queued", Campaign.status.in_(("pending", "running", "paused"))),
            ),
        )
    )
    if campaign_id:
        query = query.where(Campaign.id != campaign_id)
    return set((await session.execute(query)).scalars().all())


async def create_preview(session, workspace_id: str, request) -> dict:
    account = await account_for_workspace(session, workspace_id, request.account_id)
    contacts_snapshot = await snapshot_for_workspace(session, workspace_id, request.contacts_snapshot_id, "contacts")
    if contacts_snapshot.account_id != account.id:
        raise bad_request("Контакты получены для другого аккаунта.")
    ensure_fresh(contacts_snapshot, 1)
    await media_for_workspace(session, workspace_id, request.media_id)
    contacts_payload = unpack(contacts_snapshot)
    by_id = {contact["user_id"]: contact for contact in contacts_payload["contacts"]}
    recipients, seen = [], set()
    exclusions = exclusion_keys(request.exclusions)
    previous = await reserved_recipient_ids(session, workspace_id, include_sent=request.exclude_previous)
    excluded_count = 0
    # Validate both templates even when one variant is currently unused.
    try:
        render_caption(request.message_text, "Александр")
        if request.fallback_text:
            render_caption(request.fallback_text, fallback=True)
        for selected in request.recipients:
            if selected.user_id in seen:
                raise ValueError("Получатель выбран повторно.")
            seen.add(selected.user_id)
            contact = by_id.get(selected.user_id)
            if not contact or not contact["eligible"]:
                raise ValueError("Выбранный получатель недоступен в сохранённых контактах.")
            if (
                selected.user_id in exclusions
                or (contact["username"] or "").lower() in exclusions
                or selected.user_id in previous
            ):
                excluded_count += 1
                continue
            if len(recipients) >= request.recipient_limit:
                continue
            name = suggested_name(selected.name)
            caption = (
                render_caption(request.fallback_text, fallback=True)
                if selected.use_fallback
                else render_caption(request.message_text, name)
            )
            if utf16_length(caption) > contacts_payload["caption_limit"]:
                raise ValueError(
                    f"Подпись для {contact['username'] or selected.user_id} слишком длинная. "
                    f"Лимит: {contacts_payload['caption_limit']}."
                )
            recipients.append(
                {
                    **contact,
                    "name": name if not selected.use_fallback else "",
                    "use_fallback": selected.use_fallback,
                    "caption": caption,
                }
            )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if not recipients:
        raise bad_request("После исключений не осталось получателей. Проверьте список и предыдущие кампании.")
    payload = {
        **contacts_payload,
        "contacts": [],
        "recipients": recipients,
        "media_id": request.media_id,
        "message_text": request.message_text,
        "fallback_text": request.fallback_text,
        "exclude_previous": request.exclude_previous,
        "exclusions": request.exclusions,
        "recipient_limit": request.recipient_limit,
        "timing": request.timing.model_dump(),
    }
    snapshot = ContactBroadcastSnapshot(
        workspace_id=workspace_id,
        account_id=account.id,
        kind="preview",
        encrypted_payload=encrypt_text(json.dumps(payload, ensure_ascii=False)),
    )
    session.add(snapshot)
    await session.commit()
    return {
        "preview_id": snapshot.id,
        "excluded_count": excluded_count,
        "caption_limit": payload["caption_limit"],
        "can_start": pilot_allowed(account, payload),
        "recipients": [
            {key: row[key] for key in ("user_id", "username", "name", "caption", "use_fallback")} for row in recipients
        ],
        "pilot_only": not bulk_allowed(account),
    }


async def create_draft(session, workspace_id: str, user_id: str, request):
    snapshot = (
        await session.execute(
            select(ContactBroadcastSnapshot)
            .where(
                ContactBroadcastSnapshot.id == request.preview_id,
                ContactBroadcastSnapshot.workspace_id == workspace_id,
                ContactBroadcastSnapshot.kind == "preview",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise not_found("Предпросмотр не найден.")
    if snapshot.campaign_id:
        return await session.get(Campaign, snapshot.campaign_id)
    ensure_fresh(snapshot, 24)
    payload = unpack(snapshot)
    campaign = Campaign(
        workspace_id=workspace_id,
        created_by=user_id,
        type="message_broadcast",
        name=request.name,
        status="draft",
        config={
            "recipient_mode": "saved_contacts",
            "account_ids": [snapshot.account_id],
            "preview_id": snapshot.id,
            "media_id": payload["media_id"],
            "message_text": payload["message_text"],
            "fallback_text": payload["fallback_text"],
            "recipient_count": len(payload["recipients"]),
            "exclude_previous": payload.get("exclude_previous", True),
            "exclusions": payload.get("exclusions", ""),
            "timing": payload.get("timing", {}),
            "source_ids": [],
            "continuous": True,
            "stop_conditions": {"manual_stop": True},
        },
        policy={
            "daily_limit_per_account": payload.get("timing", {}).get("daily_limit", 1000),
            "delay_seconds_min": payload.get("timing", {}).get("delay_min", 4),
            "delay_seconds_max": payload.get("timing", {}).get("delay_max", 7),
        },
    )
    session.add(campaign)
    await session.flush()
    snapshot.campaign_id = campaign.id
    for recipient in payload["recipients"]:
        session.add(
            CampaignAction(
                campaign_id=campaign.id,
                telegram_account_id=snapshot.account_id,
                target_type="saved_contact",
                target_id=recipient["user_id"],
                action_type="message_broadcast",
                status="prepared",
                result={
                    "peer_ref": recipient["user_id"],
                    "username": recipient["username"],
                    "chat_title": recipient["username"] or recipient["first_name"],
                    "message_text": recipient["caption"],
                    "reply_text": recipient["caption"],
                    "sent_message_id": None,
                },
            )
        )
    await session.commit()
    return campaign


async def load_campaign_payload(session, campaign):
    snapshot = (
        await session.execute(
            select(ContactBroadcastSnapshot).where(
                ContactBroadcastSnapshot.campaign_id == campaign.id,
                ContactBroadcastSnapshot.workspace_id == campaign.workspace_id,
                ContactBroadcastSnapshot.kind == "preview",
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise bad_request("Для кампании отсутствует сохранённый предпросмотр.")
    account = await account_for_workspace(session, campaign.workspace_id, snapshot.account_id)
    return account, unpack(snapshot)


async def ensure_can_start(session, campaign):
    account, payload = await load_campaign_payload(session, campaign)
    pending_ids = set(
        (
            await session.execute(
                select(CampaignAction.target_id).where(
                    CampaignAction.campaign_id == campaign.id, CampaignAction.status.in_(("prepared", "queued"))
                )
            )
        )
        .scalars()
        .all()
    )
    if any(
        row["user_id"] in pending_ids
        and not row.get("use_fallback")
        and row.get("name", "") != suggested_name(row.get("name", ""))
        for row in payload["recipients"]
    ):
        raise bad_request(
            "В сохранённом предпросмотре есть фамилии или пометки. Создайте новый предпросмотр с первым именем."
        )
    if not pilot_allowed(account, payload):
        raise bad_request("Запуск разрешён только для настроенных тестового аккаунта и получателя.")
    policy_account = account
    if not get_settings().contact_broadcast_use_assigned_proxy:
        # Only this explicit direct-connection feature bypasses proxy requirements.
        # Telegram restrictions, incubator state and other account blocks still apply.
        policy_account = copy.copy(account)
        policy_account.__dict__ = {**account.__dict__, "proxy_required": False, "proxy_id": None}
    eligibility = get_module_eligibility(policy_account, "message_broadcast")
    if eligibility.status == "blocked":
        raise bad_request(eligibility.message or eligibility.reason or "Аккаунт недоступен для отправки.")
    await media_for_workspace(session, campaign.workspace_id, payload["media_id"])
    return account, payload
