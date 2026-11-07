from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.campaigns import api as campaigns_api
from app.modules.campaigns import engine as campaigns_engine
from app.modules.campaigns import service as campaigns_service
from app.modules.campaigns.models import CampaignAction, CampaignEvent
from app.modules.campaigns.schemas import NeuroChatCampaignCreate, NeuroDialogCampaignCreate
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramChatMessageInfo
from app.modules.workspaces.models import Workspace


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _workspace(session: AsyncSession) -> tuple[User, Workspace]:
    user = User(email="owner@example.com", password_hash="test")
    session.add(user)
    await session.flush()
    workspace = Workspace(owner_user_id=user.id, name="Test", slug=f"test-{user.id[:8]}")
    session.add(workspace)
    await session.flush()
    return user, workspace


@pytest.mark.asyncio
async def test_live_neuro_dialog_queues_reply_until_delay(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    generated_calls = []

    async def fake_generate_chat_reply_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        generated_calls.append(kwargs)
        return "Привет, нормально. Как у тебя?", {"mode": "test_ai"}

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_service, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="target_user",
        title="Target user",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_dialog_campaign(
        NeuroDialogCampaignCreate(
            name="Dialog",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай коротко и по контексту",
            delivery_mode="generate_and_send",
            delay_seconds_min=60,
            delay_seconds_max=60,
        ),
        session,
        workspace.id,
        user,
    )
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    now = datetime.now(UTC)
    run = await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "target_user",
        "Target user",
        TelegramChatMessageInfo(
            tg_message_id="42",
            text="Привет, как ты?",
            published_at=now,
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert run.status == "running"
    assert len(actions) == 1
    assert actions[0].status == "queued"
    assert actions[0].planned_at is not None
    assert actions[0].planned_at.replace(tzinfo=UTC) >= now + timedelta(seconds=59)
    assert actions[0].executed_at is None
    assert actions[0].result["incoming_text"] == "Привет, как ты?"
    assert actions[0].result["reply_text"] == ""
    assert actions[0].result["send"]["mode"] == "queued"
    assert generated_calls == []

    actions[0].planned_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert run.progress["replies_sent"] == 1
    assert actions[0].status == "succeeded"
    assert actions[0].executed_at is not None
    assert actions[0].result["reply_text"] == "Привет, нормально. Как у тебя?"
    assert actions[0].result["send"]["mode"] == "mock_send"
    assert generated_calls


@pytest.mark.asyncio
async def test_live_neuro_chat_skips_message_without_trigger_keyword(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="group",
        username="test_group",
        title="Test group",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_chat_campaign(
        NeuroChatCampaignCreate(
            name="Chat",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай только по темам лидогенерации и автоматизации Telegram.",
            delivery_mode="generate_and_send",
            trigger_keywords=["лидогенерация"],
        ),
        session,
        workspace.id,
        user,
    )
    campaign.policy = {**campaign.policy, "delay_seconds_min": 60, "delay_seconds_max": 60}
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    run = await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "test_group",
        "Test group",
        TelegramChatMessageInfo(
            tg_message_id="42",
            text="Всем привет, как дела?",
            published_at=datetime.now(UTC),
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    events = list((await session.execute(select(CampaignEvent))).scalars().all())
    assert run.status == "running"
    assert actions == []
    assert events[-1].event_type == "live_neuro_message_skipped"
    assert events[-1].payload["reason"] == "keyword_mismatch"
    assert events[-1].payload["keywords_count"] == 1


@pytest.mark.asyncio
async def test_neuro_chat_numeric_group_source_uses_group_peer_ref(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    fetched_refs: list[str] = []

    async def fake_fetch_messages(
        _session: AsyncSession,
        _account: TelegramAccount,
        peer_ref: str,
        _limit: int,
    ) -> list[TelegramChatMessageInfo]:
        fetched_refs.append(peer_ref)
        return []

    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="group",
        tg_id="5166123290",
        title="Test group",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_chat_campaign(
        NeuroChatCampaignCreate(
            name="Chat",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай только по темам лидогенерации и автоматизации Telegram.",
            delivery_mode="generate_and_send",
            trigger_keywords=["лидогенерация"],
        ),
        session,
        workspace.id,
        user,
    )

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_engine.tick_generic_campaign_run(session, workspace.id, run)

    assert fetched_refs == ["-5166123290"]


@pytest.mark.asyncio
async def test_live_neuro_chat_queues_reply_on_trigger_keyword(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="group",
        username="test_group",
        title="Test group",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_chat_campaign(
        NeuroChatCampaignCreate(
            name="Chat",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай только по темам лидогенерации и автоматизации Telegram.",
            delivery_mode="generate_and_send",
            trigger_keywords=["лидогенерация", "автоматизация Telegram"],
        ),
        session,
        workspace.id,
        user,
    )
    campaign.policy = {**campaign.policy, "delay_seconds_min": 60, "delay_seconds_max": 60}
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    now = datetime.now(UTC)

    run = await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "test_group",
        "Test group",
        TelegramChatMessageInfo(
            tg_message_id="43",
            text="Кто нормально делал лидогенерация из Telegram?",
            published_at=now,
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert run.status == "running"
    assert len(actions) == 1
    assert actions[0].status == "queued"
    assert actions[0].planned_at is not None
    assert actions[0].planned_at.replace(tzinfo=UTC) >= now + timedelta(seconds=59)
    assert actions[0].result["matched_keyword"] == "лидогенерация"
    assert actions[0].result["match_type"] == "keyword"
    assert actions[0].result["match_score"] == 100
    assert actions[0].result["reply_text"] == ""

    await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "test_group",
        "Test group",
        TelegramChatMessageInfo(
            tg_message_id="44",
            text="А автоматизация Telegram тоже нужна.",
            published_at=now,
            sender_tg_id="888",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list(
        (await session.execute(select(CampaignAction).order_by(CampaignAction.target_id.asc()))).scalars().all()
    )
    assert [action.target_id for action in actions] == [
        "test_group:43",
        "test_group:44",
    ]
    assert actions[1].result["matched_keyword"] == "автоматизация Telegram"


@pytest.mark.asyncio
async def test_live_neuro_chat_sanitizes_follow_up_questions(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    async def fake_generate_chat_reply_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            "Для таких задач можно посмотреть demo-platform: там лиды, источники "
            "и AI-ответы собраны в один контур. Что именно нужно?",
            {"mode": "test_ai"},
        )

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_service, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="group",
        username="test_group",
        title="Test group",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_chat_campaign(
        NeuroChatCampaignCreate(
            name="Chat",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Дай мягкую нативную рекламу demo-platform без вопросов.",
            delivery_mode="generate_and_send",
            trigger_keywords=["лидогенерация"],
        ),
        session,
        workspace.id,
        user,
    )
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "test_group",
        "Test group",
        TelegramChatMessageInfo(
            tg_message_id="45",
            text="Интересна лидогенерация из Telegram",
            published_at=datetime.now(UTC),
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    action = (await session.execute(select(CampaignAction))).scalar_one()
    assert action.status == "succeeded"
    assert "?" not in action.result["reply_text"]
    assert "Что именно нужно" not in action.result["reply_text"]
    assert action.result["usage"]["neuro_chat_question_sanitized"] is True


@pytest.mark.asyncio
async def test_continuous_live_neuro_dialog_ignores_max_comments_terminal_stop(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="target_user",
        title="Target user",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_dialog_campaign(
        NeuroDialogCampaignCreate(
            name="Dialog",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай коротко и по контексту",
            delivery_mode="generate_and_send",
            delay_seconds_min=60,
            delay_seconds_max=60,
        ),
        session,
        workspace.id,
        user,
    )
    campaign.config = {
        **campaign.config,
        "continuous": True,
        "stop_conditions": {"max_comments": 1, "manual_stop": True},
    }
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    run = await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "target_user",
        "Target user",
        TelegramChatMessageInfo(
            tg_message_id="42",
            text="Привет, как ты?",
            published_at=datetime.now(UTC),
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    assert run.status == "running"
    assert campaign.status == "running"
    assert run.finished_at is None
    assert run.progress["actions_created"] == 1


@pytest.mark.asyncio
async def test_live_neuro_dialog_debounces_pending_reply_and_uses_account_identity(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    generated_calls = []

    async def fake_generate_chat_reply_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        generated_calls.append(kwargs)
        return "Я Настя. Написала тебе из личного аккаунта.", {"mode": "test_ai"}

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_service, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="target_user",
        title="Target user",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="nastya",
        display_name="Настя",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()},
            "profile": {"first_name": "Настя", "gender": "female"},
        },
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_dialog_campaign(
        NeuroDialogCampaignCreate(
            name="Dialog",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай коротко и по контексту",
            delivery_mode="generate_and_send",
            delay_seconds_min=60,
            delay_seconds_max=60,
        ),
        session,
        workspace.id,
        user,
    )
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    first_at = datetime.now(UTC)
    run = await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "target_user",
        "Target user",
        TelegramChatMessageInfo(
            tg_message_id="42",
            text="Ты кто?",
            published_at=first_at,
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )
    second_at = datetime.now(UTC)
    await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "target_user",
        "Target user",
        TelegramChatMessageInfo(
            tg_message_id="43",
            text="А я кто?",
            published_at=second_at,
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert len(actions) == 1
    assert actions[0].status == "queued"
    assert actions[0].target_id == "target_user:43"
    assert actions[0].result["incoming_text"] == "А я кто?"
    assert actions[0].result["reply_text"] == ""
    assert actions[0].result["account_identity"]["display_name"] == "Настя"
    assert actions[0].result["account_identity"]["author_gender"] == "feminine"

    actions[0].planned_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert len(actions) == 1
    assert actions[0].status == "succeeded"
    assert actions[0].result["reply_text"] == "Я Настя. Написала тебе из личного аккаунта."
    assert generated_calls[0]["author_gender"] == "feminine"
    assert "Настя" in str(generated_calls[0]["context_text"])
    assert "Если собеседник спрашивает кто ты" in str(generated_calls[0]["prompt_text"])
    assert "Не упоминай tg.demo-platform" in str(generated_calls[0]["prompt_text"])


@pytest.mark.asyncio
async def test_live_neuro_dialog_sanitizes_legacy_brand_in_account_identity(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    generated_calls = []

    async def fake_generate_chat_reply_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        generated_calls.append(kwargs)
        return "Я Алина, менеджер demo-platform.", {"mode": "test_ai"}

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_service, "generate_chat_reply_text", fake_generate_chat_reply_text)
    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="target_user",
        title="Target user",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="manager_demo-platform",
        display_name="Алина менеджер tg.demo-platform",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()},
            "profile": {"first_name": "Алина", "last_name": "менеджер tg.demo-platform"},
        },
    )
    session.add_all([source, account])
    await session.flush()

    campaign = await campaigns_api.create_neuro_dialog_campaign(
        NeuroDialogCampaignCreate(
            name="Dialog",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай коротко и по контексту",
            delivery_mode="generate_and_send",
        ),
        session,
        workspace.id,
        user,
    )
    campaign.policy = {**campaign.policy, "delay_seconds_min": 0, "delay_seconds_max": 0}
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    await campaigns_engine.process_live_neuro_message(
        session,
        workspace.id,
        campaign,
        run,
        account.id,
        "target_user",
        "Target user",
        TelegramChatMessageInfo(
            tg_message_id="42",
            text="Кто ты?",
            published_at=datetime.now(UTC),
            sender_tg_id="777",
            outgoing=False,
            raw={"mode": "telethon_live"},
        ),
    )

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    identity = actions[0].result["account_identity"]
    context_text = str(generated_calls[0]["context_text"])
    assert identity["display_name"] == "Алина менеджер demo-platform"
    assert "tg.demo-platform" not in context_text
    assert "Алина менеджер demo-platform" in context_text


@pytest.mark.asyncio
async def test_due_neuro_dialog_skips_duplicate_queued_reply(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)

    async def fake_fetch_messages(*args: object, **kwargs: object) -> list[TelegramChatMessageInfo]:
        return []

    monkeypatch.setattr(campaigns_engine, "fetch_chat_messages_with_account", fake_fetch_messages)
    monkeypatch.setattr(campaigns_service, "fetch_chat_messages_with_account", fake_fetch_messages)
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="target_user",
        title="Target user",
        access_status="available",
    )
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=10,
    )
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="operator",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add_all([source, account])
    await session.flush()
    campaign = await campaigns_api.create_neuro_dialog_campaign(
        NeuroDialogCampaignCreate(
            name="Dialog",
            source_ids=[source.id],
            account_ids=[account.id],
            prompt_text="Отвечай коротко и по контексту",
            delivery_mode="generate_and_send",
            delay_seconds_min=60,
            delay_seconds_max=60,
        ),
        session,
        workspace.id,
        user,
    )
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    now = datetime.now(UTC)
    session.add(
        CampaignAction(
            campaign_id=campaign.id,
            telegram_account_id=account.id,
            target_type="dialog_message",
            target_id="target_user:42",
            action_type="neuro_dialog_reply",
            status="succeeded",
            planned_at=now - timedelta(seconds=5),
            executed_at=now - timedelta(seconds=5),
            result={"peer_ref": "target_user", "reply_text": "Уже ответили", "send": {"mode": "mock_send"}},
        )
    )
    duplicate = CampaignAction(
        campaign_id=campaign.id,
        telegram_account_id=account.id,
        target_type="dialog_message",
        target_id="target_user:42",
        action_type="neuro_dialog_reply",
        status="queued",
        planned_at=now - timedelta(seconds=1),
        result={
            "peer_ref": "target_user",
            "reply_text": "",
            "send": {"mode": "queued", "delay_seconds": 60},
            "pending_generation": {"prompt_text": "Ответь", "max_length": 100},
        },
    )
    session.add(duplicate)
    await session.commit()

    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = list(
        (
            await session.execute(
                select(CampaignAction)
                .where(CampaignAction.campaign_id == campaign.id)
                .order_by(CampaignAction.created_at)
            )
        )
        .scalars()
        .all()
    )

    assert [action.status for action in actions] == ["succeeded", "skipped"]
    assert actions[1].error_code == "DUPLICATE_REPLY"
