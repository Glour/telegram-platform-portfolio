import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.core.config import get_settings
from app.core.crypto import encrypt_text
from app.db.base import Base
from app.modules.campaigns.engine import (
    request_stop_campaign_run,
    start_generic_campaign_run,
    tick_generic_campaign_run,
)
from app.modules.campaigns.models import CampaignAction
from app.modules.contact_broadcasts import runner
from app.modules.contact_broadcasts.api import contacts, status, validate_image
from app.modules.contact_broadcasts.models import ContactBroadcastMedia, ContactBroadcastSnapshot
from app.modules.contact_broadcasts.schemas import (
    ContactBroadcastDraftRequest,
    ContactBroadcastPreviewRequest,
    RecipientSelection,
)
from app.modules.contact_broadcasts.service import create_draft, create_preview, render_caption, suggested_name
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway import contacts as gateway
from app.modules.telegram_gateway.contracts import TelegramGatewayError, TelegramSendResult
from app.modules.workspaces.models import Workspace


@pytest.fixture
def pilot_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "contact_broadcast_media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "contact_broadcast_pilot_account_id", "account")
    monkeypatch.setattr(settings, "contact_broadcast_pilot_sender_tg_id", "100")
    monkeypatch.setattr(settings, "contact_broadcast_pilot_recipient_tg_id", "200")
    monkeypatch.setattr(settings, "contact_broadcast_bulk_enabled", False)
    monkeypatch.setattr(settings, "contact_broadcast_bulk_workspace_ids", [])
    monkeypatch.setattr(settings, "contact_broadcast_use_assigned_proxy", False)
    return settings


def private_contacts():
    return {
        "sender_id": "100",
        "sender_username": "SampleUser",
        "caption_limit": 1000,
        "pilot_recipient_id": "200",
        "contacts": [
            {
                "user_id": "200",
                "access_hash": "987654321",
                "username": "example_recipient",
                "first_name": "Мой",
                "last_name": "Номер",
                "eligible": True,
                "reason": None,
            },
            {
                "user_id": "201",
                "access_hash": "456",
                "username": None,
                "first_name": "Александр",
                "last_name": "",
                "eligible": True,
                "reason": None,
            },
            {
                "user_id": "202",
                "access_hash": "456",
                "username": "bot",
                "first_name": "Бот",
                "last_name": "",
                "eligible": False,
                "reason": "Бот",
            },
        ],
    }


@pytest_asyncio.fixture
async def db(pilot_settings):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(Workspace(id="workspace", owner_user_id="user", name="test", slug="test"))
        session.add(
            TelegramAccount(
                id="account",
                workspace_id="workspace",
                username="SampleUser",
                status="failed",
                import_type="session",
                proxy_required=True,
                daily_limits={"restriction_status": {"status": "clear"}},
            )
        )
        media = ContactBroadcastMedia(
            id="media", workspace_id="workspace", filename="example.png", content_type="image/png"
        )
        session.add(media)
        Image.new("RGB", (10, 10)).save(Path(pilot_settings.contact_broadcast_media_dir) / media.filename)
        session.add(
            ContactBroadcastSnapshot(
                id="contacts",
                workspace_id="workspace",
                account_id="account",
                kind="contacts",
                encrypted_payload=encrypt_text(json.dumps(private_contacts())),
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


def preview_request(**changes):
    values = {
        "account_id": "account",
        "contacts_snapshot_id": "contacts",
        "media_id": "media",
        "message_text": "{name},доброе утро 🪷\nТекст💜",
        "fallback_text": "Доброе утро 🪷\nТекст💜",
        "recipients": [RecipientSelection(user_id="200", use_fallback=True)],
    }
    return ContactBroadcastPreviewRequest(**(values | changes))


async def draft(db, **changes):
    result = await create_preview(db, "workspace", preview_request(**changes))
    return await create_draft(db, "workspace", "user", ContactBroadcastDraftRequest(preview_id=result["preview_id"]))


def test_templates_and_names():
    assert render_caption("Здравствуйте, {name}! 💜", "Александр") == "Здравствуйте, Александр! 💜"
    assert render_caption("Доброе утро 🪷", fallback=True) == "Доброе утро 🪷"
    assert render_caption("{{name}} {name}", "Анна") == "{name} Анна"
    assert suggested_name("Александр") == "Александр"
    assert suggested_name("Alex") == "Alex"
    assert suggested_name("Саша работа") == "Саша"
    assert suggested_name("Мой", "Номер") == "Мой"


@pytest.mark.parametrize("template", ["{unknown}", "{name.__class__}", "{name[0]}", "{name!r}", "{name:>4}", "{", "{}"])
def test_no_expression_execution(template):
    with pytest.raises(ValueError):
        render_caption(template, "Анна")


def test_image_validation():
    for fmt, expected in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
        data = io.BytesIO()
        Image.new("RGB", (10, 10)).save(data, format=fmt)
        assert validate_image(data.getvalue())[1] == expected
    with pytest.raises(HTTPException):
        validate_image(b"not an image")


@pytest.mark.asyncio
async def test_private_snapshot_preview_and_draft_idempotence(db):
    result = await create_preview(db, "workspace", preview_request())
    assert result["can_start"]
    assert result["recipients"][0]["caption"] == "Доброе утро 🪷\nТекст💜"
    assert "987654321" not in json.dumps(result)
    request = ContactBroadcastDraftRequest(preview_id=result["preview_id"])
    first = await create_draft(db, "workspace", "user", request)
    second = await create_draft(db, "workspace", "user", request)
    assert first.id == second.id
    assert "access_hash" not in json.dumps(first.config)
    actions = (await db.execute(select(CampaignAction))).scalars().all()
    assert len(actions) == 1 and actions[0].status == "prepared"


@pytest.mark.asyncio
async def test_no_username_manual_name_and_nonpilot_draft(db):
    result = await create_preview(
        db, "workspace", preview_request(recipients=[RecipientSelection(user_id="201", name="Александр")])
    )
    assert result["recipients"][0]["caption"].startswith("Александр,")
    assert not result["can_start"]
    campaign = await draft(db, recipients=[RecipientSelection(user_id="201", name="Александр")])
    with pytest.raises(HTTPException):
        await start_generic_campaign_run(db, "workspace", campaign)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"recipients": [RecipientSelection(user_id="999", name="Анна")]},
        {"recipients": [RecipientSelection(user_id="202", name="Анна")]},
        {"recipients": [RecipientSelection(user_id="200")]},
        {"message_text": "💜" * 501, "recipients": [RecipientSelection(user_id="201", name="Александр")]},
        {"fallback_text": "💜" * 501},
        {"recipients": [RecipientSelection(user_id="200", use_fallback=True)] * 2},
        {"fallback_text": "{name}"},
    ],
)
async def test_invalid_previews(db, changes):
    with pytest.raises(HTTPException):
        await create_preview(db, "workspace", preview_request(**changes))


@pytest.mark.asyncio
async def test_cross_workspace_and_stale_contacts(db):
    with pytest.raises(HTTPException) as error:
        await create_preview(db, "other", preview_request())
    assert error.value.status_code == 404
    snapshot = await db.get(ContactBroadcastSnapshot, "contacts")
    snapshot.created_at = datetime.now(UTC) - timedelta(hours=2)
    await db.commit()
    with pytest.raises(HTTPException):
        await create_preview(db, "workspace", preview_request())


@pytest.mark.asyncio
async def test_contacts_api_never_exposes_peer_secrets(db, monkeypatch):
    async def fetch(*args):
        return private_contacts()

    monkeypatch.setattr("app.modules.contact_broadcasts.api.fetch_saved_contacts", fetch)
    result = await contacts("account", db, "workspace")
    assert len(result["contacts"]) == 3 and result["pilot_contact_found"]
    assert "access_hash" not in json.dumps(result)
    assert "phone" not in json.dumps(result)


@pytest.mark.asyncio
async def test_single_send_restart_and_status(db, monkeypatch):
    calls = []

    async def send(*args):
        calls.append(args)
        return TelegramSendResult(tg_message_id="77", raw={"outgoing_verified": True})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    duplicate = await start_generic_campaign_run(db, "workspace", campaign)
    assert duplicate.id == run.id
    await tick_generic_campaign_run(db, "workspace", run)
    await tick_generic_campaign_run(db, "workspace", run)
    assert len(calls) == 1
    result = await status(campaign.id, db, "workspace")
    assert result["status"] == "completed"
    assert result["report"]["sent"] == result["report"]["outgoing_verified"] == 1
    assert result["actions"][0]["message_id"] == "77"
    assert "access_hash" not in json.dumps(result)
    assert (await db.get(TelegramAccount, "account")).proxy_required  # feature did not mutate account routing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, expected_status",
    [
        ("FLOOD_WAIT", "queued"),
        ("TELEGRAM_CONNECTION_FAILED", "unknown"),
        ("TELEGRAM_OPERATION_VALUE_ERROR", "unknown"),
        ("PeerFloodError", "failed"),
        ("UserIsBlockedError", "failed"),
    ],
)
async def test_delivery_failure_policy(db, monkeypatch, code, expected_status):
    calls = []

    async def send(*args):
        calls.append(args)
        raise TelegramGatewayError(code, "test", {"seconds": 3600})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    await tick_generic_campaign_run(db, "workspace", run)
    action = (await db.execute(select(CampaignAction))).scalars().one()
    assert action.status == expected_status
    await tick_generic_campaign_run(db, "workspace", run)
    assert len(calls) == 1
    if code == "FLOOD_WAIT":
        assert run.status == "running" and runner.aware(action.planned_at) > datetime.now(UTC)
    elif code == "TELEGRAM_CONNECTION_FAILED":
        assert run.status == "paused"


@pytest.mark.asyncio
async def test_killed_worker_never_repeats_send(db, monkeypatch):
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    action = (await db.execute(select(CampaignAction))).scalars().one()
    action.status = "sending"
    action.result = {**action.result, "sending_started_at": (datetime.now(UTC) - timedelta(minutes=11)).isoformat()}
    await db.commit()
    await tick_generic_campaign_run(db, "workspace", run)
    assert action.status == "unknown" and run.status == "paused"


@pytest.mark.asyncio
async def test_manual_stop_before_tick(db, monkeypatch):
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    await request_stop_campaign_run(db, "workspace", run)
    await tick_generic_campaign_run(db, "workspace", run)
    assert run.status == "cancelled"
    assert not run.report.get("sent", 0)


@pytest.mark.asyncio
async def test_unique_recipient_delivery(db):
    campaign = await draft(db)
    db.add(
        CampaignAction(
            campaign_id=campaign.id,
            telegram_account_id="account",
            target_type="saved_contact",
            target_id="200",
            action_type="message_broadcast",
            status="prepared",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_gateway_contact_membership_requires_proxy(monkeypatch, pilot_settings):
    calls = []
    users = [
        SimpleNamespace(
            id=value,
            username="example_recipient",
            first_name="Мой",
            last_name="Номер",
            deleted=False,
            bot=False,
            access_hash=400,
        )
        for value in (200, 999)
    ]

    class Client:
        async def get_me(self):
            return SimpleNamespace(id=100, username="SampleUser", premium=False)

        async def __call__(self, request):
            if isinstance(request, gateway.GetContactsRequest):
                return SimpleNamespace(contacts=[SimpleNamespace(user_id=200)], users=users)
            return SimpleNamespace(caption_length_max=1024)

    @asynccontextmanager
    async def context(*args, **kwargs):
        calls.append(kwargs)
        yield Client()

    monkeypatch.setattr(gateway, "telegram_client_for_account", context)
    result = await gateway.fetch_saved_contacts(None, SimpleNamespace(username="SampleUser", proxy_id="proxy"))
    assert [c["user_id"] for c in result["contacts"]] == ["200"]
    assert calls[0] == {"use_proxy": True, "strict_requests": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Мой Номер", "Саша работа", "Alex 💜", "Виктория Иванова"])
async def test_saved_contact_names_trimmed_to_first_name(db, name):
    result = await create_preview(
        db, "workspace", preview_request(recipients=[RecipientSelection(user_id="200", name=name)])
    )
    assert result["recipients"][0]["caption"].startswith(suggested_name(name) + ", ")
    assert result["recipients"][0]["name"] == suggested_name(name)
    assert not result["recipients"][0]["use_fallback"]


@pytest.mark.asyncio
async def test_bulk_enablement_scoped_to_workspace(db, pilot_settings):
    pilot_settings.contact_broadcast_bulk_enabled = True
    pilot_settings.contact_broadcast_bulk_workspace_ids = ["workspace"]
    result = await create_preview(
        db, "workspace", preview_request(recipients=[RecipientSelection(user_id="201", name="Александр")])
    )
    assert result["can_start"] and not result["pilot_only"]
    pilot_settings.contact_broadcast_bulk_workspace_ids = ["other"]
    result = await create_preview(
        db, "workspace", preview_request(recipients=[RecipientSelection(user_id="201", name="Александр")])
    )
    assert not result["can_start"] and result["pilot_only"]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{name},доброе утро 🪷", "Александр, доброе утро 🪷"),
        ("{name},   доброе утро", "Александр, доброе утро"),
        (
            "С октября цены на массаж,если нужен абонемент,то сегодня💜",
            "С октября цены на массаж, если нужен абонемент, то сегодня💜",
        ),
        ("{name},\nдоброе утро", "Александр,\nдоброе утро"),
        ("{name}, доброе утро", "Александр, доброе утро"),
    ],
)
def test_comma_spacing_in_rendered_caption(template, expected):
    assert render_caption(template, "Александр") == expected


@pytest.mark.asyncio
async def test_caption_limit_applies_after_comma_spacing(db):
    # 1,000 units before normalization; adding the missing space exceeds the limit.
    with pytest.raises(HTTPException):
        await create_preview(
            db,
            "workspace",
            preview_request(
                message_text="а," + "а" * 998,
                recipients=[RecipientSelection(user_id="200", name="Мой Номер")],
            ),
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Светлана Морошкина6438", "Светлана"),
        ("Светлана6438", "Светлана"),
        ("Эвелина💗", "Эвелина"),
        ("  Виктория Шмелькова 8658 🌺", "Виктория"),
        ("Марина 8611", "Марина"),
        ("Анна-Мария Сидорова", "Анна-Мария"),
        ("💜 Анна 123", "Анна"),
        ("1234 💜", ""),
        ("", ""),
    ],
)
def test_first_name_extraction(raw, expected):
    assert suggested_name(raw, "Фамилия") == expected
    if expected:
        assert render_caption("{name},доброе утро", raw) == expected + ", доброе утро"
    else:
        with pytest.raises(ValueError):
            render_caption("{name},доброе утро", raw)


@pytest.mark.asyncio
async def test_legacy_snapshot_with_dirty_name_cannot_start(db):
    from app.modules.contact_broadcasts.service import unpack

    campaign = await draft(db, recipients=[RecipientSelection(user_id="200", name="Светлана")])
    snapshot = (
        await db.execute(select(ContactBroadcastSnapshot).where(ContactBroadcastSnapshot.campaign_id == campaign.id))
    ).scalar_one()
    payload = unpack(snapshot)
    payload["recipients"][0]["name"] = "Светлана Морошкина6438"
    payload["recipients"][0]["caption"] = "Светлана Морошкина6438, доброе утро"
    snapshot.encrypted_payload = encrypt_text(json.dumps(payload))
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await start_generic_campaign_run(db, "workspace", campaign)
    assert "новый предпросмотр" in error.value.detail


@pytest.mark.asyncio
async def test_limit_and_manual_exclusions(db):
    result = await create_preview(
        db,
        "workspace",
        preview_request(
            recipient_limit=1,
            recipients=[
                RecipientSelection(user_id="200", name="Светлана6438"),
                RecipientSelection(user_id="201", name="Александр"),
            ],
        ),
    )
    assert [row["user_id"] for row in result["recipients"]] == ["200"]
    assert result["recipients"][0]["name"] == "Светлана"
    result = await create_preview(
        db,
        "workspace",
        preview_request(
            exclusions="https://t.me/example_recipient; @OTHER",
            recipients=[
                RecipientSelection(user_id="200", name="Светлана"),
                RecipientSelection(user_id="201", name="Александр"),
            ],
        ),
    )
    assert result["excluded_count"] == 1 and [row["user_id"] for row in result["recipients"]] == ["201"]
    with pytest.raises(HTTPException):
        await create_preview(db, "workspace", preview_request(exclusions="id:200"))


@pytest.mark.asyncio
async def test_cross_campaign_reservation_and_exports(db, monkeypatch):
    from app.modules.contact_broadcasts.api import recipients_export
    from app.modules.contact_broadcasts.service import reserved_recipient_ids

    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    first = await draft(db)
    second = await draft(db)
    await start_generic_campaign_run(db, "workspace", first)
    second_run = await start_generic_campaign_run(db, "workspace", second)
    action = (await db.execute(select(CampaignAction).where(CampaignAction.campaign_id == second.id))).scalar_one()
    assert action.status == "skipped"
    await tick_generic_campaign_run(db, "workspace", second_run)
    assert second_run.status == "completed"
    assert await reserved_recipient_ids(db, "other") == set()
    assert (await recipients_export(first.id, db, "workspace"))["text"] == "@example_recipient"
    assert (await recipients_export(first.id, db, "workspace", "sent"))["text"] == ""
    with pytest.raises(HTTPException):
        await recipients_export(first.id, db, "other")
    # A new campaign cannot blindly retry an uncertain recipient even if history exclusion is disabled.
    action = (await db.execute(select(CampaignAction).where(CampaignAction.campaign_id == first.id))).scalar_one()
    action.status = "unknown"
    await db.commit()
    with pytest.raises(HTTPException):
        await create_preview(db, "workspace", preview_request(exclude_previous=False))


@pytest.mark.asyncio
async def test_pause_resume_remaining_only_and_username_free_export(db, monkeypatch, pilot_settings):
    from app.modules.contact_broadcasts import scheduling
    from app.modules.contact_broadcasts.api import pause, recipients_export, resume, settings_update
    from app.modules.contact_broadcasts.schemas import BroadcastSettingsRequest, BroadcastTiming

    pilot_settings.contact_broadcast_bulk_enabled = True
    calls = []
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(scheduling.celery_app, "send_task", lambda *args, **kwargs: None)

    async def send(*args):
        calls.append(args[3]["user_id"])
        return TelegramSendResult(tg_message_id=str(len(calls)), raw={"outgoing_verified": True})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    campaign = await draft(
        db,
        recipients=[
            RecipientSelection(user_id="200", name="Эвелина💗"),
            RecipientSelection(user_id="201", name="Александр"),
        ],
    )
    run = await start_generic_campaign_run(db, "workspace", campaign)
    await tick_generic_campaign_run(db, "workspace", run)
    assert len(calls) == 1
    await pause(campaign.id, db, "workspace")
    await tick_generic_campaign_run(db, "workspace", run)
    assert len(calls) == 1 and run.status == "paused"
    await settings_update(
        campaign.id, BroadcastSettingsRequest(timing=BroadcastTiming(delay_min=8, delay_max=10)), db, "workspace"
    )
    await resume(campaign.id, db, "workspace")
    await tick_generic_campaign_run(db, "workspace", run)
    assert len(calls) == 1  # Resume must preserve the next-send time.
    account = await db.get(TelegramAccount, "account")
    account.daily_limits = {
        **account.daily_limits,
        "contact_broadcast_wait_until": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
    }
    run.progress = {**run.progress, "next_send_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat()}
    action = (
        await db.execute(
            select(CampaignAction).where(
                CampaignAction.campaign_id == campaign.id, CampaignAction.status == "succeeded"
            )
        )
    ).scalar_one()
    action.executed_at = datetime.now(UTC) - timedelta(seconds=60)
    await db.commit()
    await tick_generic_campaign_run(db, "workspace", run)
    assert sorted(calls) == ["200", "201"] and run.status == "completed"
    assert (await recipients_export(campaign.id, db, "workspace", "sent"))["text"] == "@example_recipient\nid:201"


@pytest.mark.asyncio
async def test_pause_arriving_during_network_send_is_preserved(db, monkeypatch, pilot_settings):
    from app.modules.contact_broadcasts.api import pause

    pilot_settings.contact_broadcast_bulk_enabled = True
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    campaign = await draft(
        db,
        recipients=[
            RecipientSelection(user_id="200", name="Анна"),
            RecipientSelection(user_id="201", name="Александр"),
        ],
    )
    run = await start_generic_campaign_run(db, "workspace", campaign)

    async def send(*args):
        await pause(campaign.id, db, "workspace")
        return TelegramSendResult(tg_message_id="1", raw={"outgoing_verified": True})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    await tick_generic_campaign_run(db, "workspace", run)
    assert run.status == campaign.status == "paused"
    assert run.report["sent"] == 1


@pytest.mark.asyncio
async def test_random_delays_periodic_pause_and_scheduler_dedup(db, monkeypatch, pilot_settings):
    from app.modules.contact_broadcasts import scheduling

    pilot_settings.contact_broadcast_bulk_enabled = True
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(runner.random, "randint", lambda low, high: low)

    async def send(*args):
        return TelegramSendResult(tg_message_id="1", raw={"outgoing_verified": True})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    campaign = await draft(
        db,
        recipients=[
            RecipientSelection(user_id="200", name="Анна"),
            RecipientSelection(user_id="201", name="Александр"),
        ],
    )
    run = await start_generic_campaign_run(db, "workspace", campaign)
    run.progress = {"pause_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    await db.commit()
    await tick_generic_campaign_run(db, "workspace", run)
    delta = datetime.fromisoformat(run.progress["next_send_at"]) - datetime.now(UTC)
    assert 4.5 < delta.total_seconds() <= 5  # 4-second spacing plus a 1-second periodic pause.
    calls = []
    monkeypatch.setattr(scheduling.celery_app, "send_task", lambda *args, **kwargs: calls.append(kwargs))
    await scheduling.queue_next_tick(db, run)
    await scheduling.queue_next_tick(db, run)
    assert len(calls) == 1 and calls[0]["kwargs"]["token"] == run.progress["tick_token"]


@pytest.mark.asyncio
async def test_unknown_result_cannot_resume(db):
    from app.modules.contact_broadcasts.api import resume

    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    action = (await db.execute(select(CampaignAction))).scalar_one()
    run.status = campaign.status = "paused"
    action.status = "unknown"
    await db.commit()
    with pytest.raises(HTTPException):
        await resume(campaign.id, db, "workspace")


@pytest.mark.asyncio
async def test_floodwait_blocks_other_campaign_on_same_account(db, monkeypatch, pilot_settings):
    pilot_settings.contact_broadcast_bulk_enabled = True
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)
    calls = []

    async def send(*args):
        calls.append(args)
        raise TelegramGatewayError("FLOOD_WAIT", "wait", {"seconds": 3600})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    first = await draft(db)
    second = await draft(db, recipients=[RecipientSelection(user_id="201", name="Александр")])
    first_run = await start_generic_campaign_run(db, "workspace", first)
    second_run = await start_generic_campaign_run(db, "workspace", second)
    await tick_generic_campaign_run(db, "workspace", first_run)
    await tick_generic_campaign_run(db, "workspace", second_run)
    assert len(calls) == 1
    assert second_run.progress["next_send_at"] == first_run.progress["next_send_at"]


@pytest.mark.asyncio
async def test_paused_exclusions_only_skip_unsent(db, monkeypatch, pilot_settings):
    from app.modules.contact_broadcasts.api import pause, settings_update
    from app.modules.contact_broadcasts.schemas import BroadcastSettingsRequest, BroadcastTiming

    pilot_settings.contact_broadcast_bulk_enabled = True
    monkeypatch.setattr(runner, "real_telegram_enabled", lambda: True)

    async def send(*args):
        return TelegramSendResult(tg_message_id="1", raw={"outgoing_verified": True})

    monkeypatch.setattr(runner, "send_contact_photo", send)
    campaign = await draft(
        db,
        recipients=[
            RecipientSelection(user_id="200", name="Анна"),
            RecipientSelection(user_id="201", name="Александр"),
        ],
    )
    run = await start_generic_campaign_run(db, "workspace", campaign)
    await tick_generic_campaign_run(db, "workspace", run)
    await pause(campaign.id, db, "workspace")
    await settings_update(
        campaign.id,
        BroadcastSettingsRequest(timing=BroadcastTiming(), exclusions="@example_recipient\nid:201"),
        db,
        "workspace",
    )
    actions = (
        (
            await db.execute(
                select(CampaignAction)
                .where(CampaignAction.campaign_id == campaign.id)
                .order_by(CampaignAction.target_id)
            )
        )
        .scalars()
        .all()
    )
    assert [a.status for a in actions] == ["succeeded", "skipped"]


@pytest.mark.asyncio
async def test_scheduler_ignores_stale_token(db, monkeypatch):
    from contextlib import asynccontextmanager

    from app.workers import tasks

    campaign = await draft(db)
    run = await start_generic_campaign_run(db, "workspace", campaign)
    run.progress = {"tick_token": "current"}
    await db.commit()

    @asynccontextmanager
    async def session_factory():
        yield db

    async def dispose():
        pass

    monkeypatch.setattr(tasks, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(tasks, "engine", SimpleNamespace(dispose=dispose))
    result = await tasks._tick_contact_broadcast(run.id, "old")
    assert result["status"] == "inactive_or_stale"
    assert run.progress["tick_token"] == "current"
    action = (await db.execute(select(CampaignAction))).scalar_one()
    assert action.status == "queued"
