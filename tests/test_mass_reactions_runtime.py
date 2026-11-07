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
from app.modules.campaigns.models import Campaign, CampaignAction
from app.modules.campaigns.schemas import CommentPostsCampaignUpdate
from app.modules.comments.models import TelegramComment
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility
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


async def _workspace(session: AsyncSession) -> Workspace:
    user = User(email="owner@example.com", password_hash="test")
    session.add(user)
    await session.flush()
    workspace = Workspace(owner_user_id=user.id, name="Test", slug=f"test-{user.id[:8]}")
    session.add(workspace)
    await session.flush()
    return workspace


async def _reaction_parts(session: AsyncSession, workspace: Workspace) -> tuple[TelegramSource, TelegramAccount]:
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="target_channel",
        title="Target channel",
        access_status="available",
        comments_enabled=True,
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
        username="reactor",
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
    return source, account


async def _messages(
    session: AsyncSession,
    workspace: Workspace,
    source: TelegramSource,
    count: int = 2,
) -> list[TelegramMessage]:
    now = datetime.now(UTC)
    messages = [
        TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id=str(1000 + index),
            text=f"Post {index}",
            published_at=now - timedelta(minutes=index),
            raw={},
        )
        for index in range(count)
    ]
    session.add_all(messages)
    await session.flush()
    return messages


def _campaign(
    workspace: Workspace,
    source: TelegramSource,
    account: TelegramAccount,
    *,
    daily_limit: int = 50,
) -> Campaign:
    return Campaign(
        workspace_id=workspace.id,
        type="mass_reactions",
        name="AI реакции",
        status="draft",
        config={
            "source_ids": [source.id],
            "account_ids": [account.id],
            "reactions": ["👍"],
            "target_scope": "posts",
            "post_selection_mode": "historical_posts",
            "latest_posts_per_source": 10,
            "reaction_probability": 100,
            "continuous": False,
            "stop_conditions": {"max_reactions": 10, "max_comments": 10, "manual_stop": True},
        },
        policy={
            "daily_limit_per_account": daily_limit,
            "delay_seconds_min": 0,
            "delay_seconds_max": 0,
            "reaction_rotation": "round_robin",
        },
    )


def _new_posts_campaign(workspace: Workspace, source: TelegramSource, account: TelegramAccount) -> Campaign:
    campaign = _campaign(workspace, source, account)
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "latest_posts_per_source": 0,
        "continuous": True,
        "stop_conditions": {"max_reactions": 10, "max_comments": 10, "manual_stop": True},
    }
    return campaign


async def _reaction_actions(session: AsyncSession, campaign: Campaign) -> list[CampaignAction]:
    result = await session.execute(
        select(CampaignAction)
        .where(CampaignAction.campaign_id == campaign.id, CampaignAction.action_type == "set_reaction")
        .order_by(CampaignAction.created_at.asc())
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_mass_reactions_tick_creates_exact_message_actions_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    messages = await _messages(session, workspace, source, count=2)
    campaign = _campaign(workspace, source, account)
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 2
    assert run.progress["reactions_sent"] == 0
    assert [action.result["tg_message_id"] for action in actions] == [message.tg_message_id for message in messages]
    assert all(action.status == "queued" for action in actions)
    assert all(action.result["send"]["mode"] == "queued" for action in actions)

    second_run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(second_run.id, session, workspace.id)

    actions_after_retry = await _reaction_actions(session, campaign)
    assert len(actions_after_retry) == 2


@pytest.mark.asyncio
async def test_mass_reactions_daily_account_limit_stops_extra_targets(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=3)
    campaign = _campaign(workspace, source, account, daily_limit=1)
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert len(actions) == 1
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert run.progress["reactions_skipped_daily_limit"] == 1
    assert run.progress["stop_reason"] == "daily_limit"


@pytest.mark.asyncio
async def test_mass_reactions_parallel_pool_reacts_from_each_account(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    second_account = TelegramAccount(
        workspace_id=workspace.id,
        username="reactor_two",
        status="active",
        import_type="session",
        proxy_id=account.proxy_id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )
    session.add(second_account)
    messages = await _messages(session, workspace, source, count=2)
    campaign = _campaign(workspace, source, account)
    campaign.config = {
        **campaign.config,
        "account_ids": [account.id, second_account.id],
        "source_assignment_mode": "parallel_pool",
        "source_assignment": {
            account.id: [source.id],
            second_account.id: [source.id],
        },
    }
    campaign.policy = {key: value for key, value in campaign.policy.items() if key != "daily_limit_per_account"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 4
    assert len(actions) == 4
    assert sorted(action.telegram_account_id for action in actions) == sorted(
        [account.id, account.id, second_account.id, second_account.id]
    )
    assert sorted(action.result["tg_message_id"] for action in actions) == sorted(
        [message.tg_message_id for message in messages for _ in range(2)]
    )

    second_run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(second_run.id, session, workspace.id)
    assert len(await _reaction_actions(session, campaign)) == 4


@pytest.mark.asyncio
async def test_mass_reactions_new_posts_initializes_cursor_before_processing(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=1)
    campaign = _new_posts_campaign(workspace, source, account)
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    assert run.progress["reaction_cursors_initialized"] == 1
    assert await _reaction_actions(session, campaign) == []

    session.add(
        TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id="2000",
            text="Fresh post",
            published_at=datetime.now(UTC) + timedelta(minutes=1),
            raw={},
        )
    )
    await session.commit()

    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert [action.result["tg_message_id"] for action in actions] == ["2000"]


@pytest.mark.asyncio
async def test_mass_reactions_new_posts_with_initial_backfill_processes_history_then_fresh_posts(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    messages = await _messages(session, workspace, source, count=3)
    campaign = _new_posts_campaign(workspace, source, account)
    campaign.config = {**campaign.config, "latest_posts_per_source": 2}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reaction_cursors_initialized"] == 1
    assert run.progress["reactions_planned"] == 2
    assert [action.result["tg_message_id"] for action in actions] == [
        messages[0].tg_message_id,
        messages[1].tg_message_id,
    ]

    session.add(
        TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id="3000",
            text="Fresh post after backfill",
            published_at=datetime.now(UTC) + timedelta(minutes=1),
            raw={},
        )
    )
    await session.commit()

    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 3
    assert [action.result["tg_message_id"] for action in actions] == [
        messages[0].tg_message_id,
        messages[1].tg_message_id,
        "3000",
    ]


@pytest.mark.asyncio
async def test_mass_reactions_polling_campaign_executes_due_backfill_queue(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    account.daily_limits = {
        "restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()},
    }
    await _messages(session, workspace, source, count=2)
    campaign = _new_posts_campaign(workspace, source, account)
    campaign.config = {**campaign.config, "latest_posts_per_source": 2}
    campaign.policy = {**campaign.policy, "delay_seconds_min": 60, "delay_seconds_max": 60}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert run.status == "running"
    assert campaigns_engine._campaign_uses_live_listener(campaign) is False
    assert [action.status for action in actions] == ["queued", "queued"]

    for action in actions:
        action.planned_at = datetime.now(UTC) - timedelta(seconds=1)
    run.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    await session.commit()

    ticked = await campaigns_engine.tick_active_campaign_runs(session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert [item.id for item in ticked] == [run.id]
    assert [action.status for action in actions] == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_mass_reactions_initial_backfill_fetches_posts_when_cache_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: True)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    campaign = _new_posts_campaign(workspace, source, account)
    campaign.config = {**campaign.config, "latest_posts_per_source": 2}
    campaign.policy = {**campaign.policy, "delay_seconds_min": 60, "delay_seconds_max": 60}
    session.add(campaign)
    await session.commit()

    async def fake_fetch_real_posts(
        session: AsyncSession,
        workspace_id: str,
        source: TelegramSource,
        account: TelegramAccount,
        limit: int,
    ) -> list[TelegramMessage]:
        return [
            TelegramMessage(
                workspace_id=workspace_id,
                source_id=source.id,
                tg_message_id=str(4000 + index),
                text=f"Fetched post {index}",
                published_at=datetime.now(UTC) - timedelta(minutes=index),
                raw={"mode": "test_fetch"},
            )
            for index in range(limit)
        ]

    monkeypatch.setattr(campaigns_engine, "fetch_real_posts", fake_fetch_real_posts)

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["messages_seen"] == 2
    assert run.progress["reaction_cursors_initialized"] == 1
    assert run.progress["reactions_planned"] == 2
    assert [action.result["tg_message_id"] for action in actions] == ["4000", "4001"]


@pytest.mark.asyncio
async def test_mass_reactions_report_uses_campaign_actions(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=1)
    campaign = _campaign(workspace, source, account)
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    report = await campaigns_api.campaign_reaction_report(campaign.id, session, workspace.id, None, 1000)
    assert report.total_count == 1
    assert report.sent_count == 0
    assert report.rows[0].reaction == "👍"
    assert report.rows[0].tg_message_id == "1000"
    csv_report = campaigns_service.campaign_reaction_report_csv(report)
    assert "campaign_name,source_title,source_ref,target_kind,target_tg_message_id" in csv_report


@pytest.mark.asyncio
async def test_mass_reactions_fixed_mode_uses_first_reaction(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=2)
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "reactions": ["🔥", "👍"]}
    campaign.policy = {**campaign.policy, "reaction_rotation": "fixed"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert [action.result["reaction"] for action in actions] == ["🔥", "🔥"]


@pytest.mark.asyncio
async def test_mass_reactions_round_robin_uses_selected_reactions(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=3)
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "reactions": ["🔥", "❤️", "👀"]}
    campaign.policy = {**campaign.policy, "reaction_rotation": "round_robin"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert [action.result["reaction"] for action in actions] == ["🔥", "❤️", "👀"]


@pytest.mark.asyncio
async def test_mass_reactions_delay_queues_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=1)
    campaign = _campaign(workspace, source, account)
    campaign.policy = {**campaign.policy, "delay_seconds_min": 60, "delay_seconds_max": 60}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.status == "running"
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert actions[0].status == "queued"
    assert actions[0].executed_at is None
    assert actions[0].planned_at is not None
    assert actions[0].result["send"]["mode"] == "queued"

    actions[0].planned_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert run.status == "completed"
    assert run.progress["reactions_sent"] == 1
    assert actions[0].status == "succeeded"
    assert actions[0].executed_at is not None
    assert actions[0].result["send"]["mode"] == "mock_send"


@pytest.mark.asyncio
async def test_mass_reactions_delay_spaces_multiple_actions(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=2)
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "reactions": ["🔥", "👀"]}
    campaign.policy = {
        **campaign.policy,
        "reaction_rotation": "round_robin",
        "delay_seconds_min": 60,
        "delay_seconds_max": 60,
    }
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert len(actions) == 2
    assert [action.status for action in actions] == ["queued", "queued"]
    assert [action.result["reaction"] for action in actions] == ["🔥", "👀"]
    assert all(action.planned_at for action in actions)
    assert (
        actions[1].planned_at.replace(tzinfo=UTC) - actions[0].planned_at.replace(tzinfo=UTC)
    ).total_seconds() >= 600
    assert actions[1].result["send"]["delay_seconds"] > actions[0].result["send"]["delay_seconds"]
    assert actions[0].result["settings_snapshot"]["reaction_rotation"] == "round_robin"
    assert actions[0].result["settings_snapshot"]["delay_seconds_min"] == 600


@pytest.mark.asyncio
async def test_mass_reactions_update_resets_queued_actions_and_uses_new_settings(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    source.linked_discussion_username = "target_discussion"
    messages = await _messages(session, workspace, source, count=1)
    session.add(
        TelegramComment(
            workspace_id=workspace.id,
            campaign_id="source-comment-import",
            source_id=source.id,
            post_message_id=messages[0].id,
            tg_comment_message_id="777",
            text="Comment target",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    campaign = _campaign(workspace, source, account)
    campaign.policy = {**campaign.policy, "delay_seconds_min": 300, "delay_seconds_max": 300}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert [(action.status, action.result["reaction"], action.result["target_kind"]) for action in actions] == [
        ("queued", "👍", "telegram_message")
    ]

    await campaigns_api.update_campaign(
        campaign.id,
        CommentPostsCampaignUpdate(
            reactions=["🔥", "👀"],
            target_scope="comments",
            reaction_mode="round_robin",
            delay_seconds_min=90,
            delay_seconds_max=90,
        ),
        session,
        workspace.id,
    )

    assert await _reaction_actions(session, campaign) == []

    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)
    actions = await _reaction_actions(session, campaign)
    assert len(actions) == 1
    assert actions[0].status == "queued"
    assert actions[0].result["reaction"] == "🔥"
    assert actions[0].result["target_kind"] == "telegram_comment"
    assert actions[0].result["tg_message_id"] == "777"
    assert actions[0].planned_at is not None
    assert (actions[0].planned_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() > 30


@pytest.mark.asyncio
async def test_mass_reactions_comments_scope_uses_comment_targets(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    source.linked_discussion_username = "target_discussion"
    messages = await _messages(session, workspace, source, count=1)
    session.add(
        TelegramComment(
            workspace_id=workspace.id,
            campaign_id="source-comment-import",
            source_id=source.id,
            post_message_id=messages[0].id,
            tg_comment_message_id="777",
            text="Useful comment",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "target_scope": "comments", "comments_per_post": 10}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert actions[0].target_type == "telegram_comment"
    assert actions[0].result["target_kind"] == "telegram_comment"
    assert actions[0].result["tg_message_id"] == "777"
    assert actions[0].result["reaction_peer_ref"] == "target_discussion"
    assert actions[0].result["send"]["reaction_peer_ref"] == "target_discussion"


@pytest.mark.asyncio
async def test_mass_reactions_comments_scope_formats_numeric_discussion_peer(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    source.linked_discussion_tg_id = "3583775489"
    messages = await _messages(session, workspace, source, count=1)
    session.add(
        TelegramComment(
            workspace_id=workspace.id,
            campaign_id="source-comment-import",
            source_id=source.id,
            post_message_id=messages[0].id,
            tg_comment_message_id="777",
            text="Useful comment",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "target_scope": "comments", "comments_per_post": 10}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert actions[0].target_type == "telegram_comment"
    assert actions[0].result["reaction_peer_ref"] == "-1003583775489"
    assert actions[0].result["send"]["reaction_peer_ref"] == "-1003583775489"


@pytest.mark.asyncio
async def test_mass_reactions_chat_scope_uses_chat_message_targets(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    source.type = "group"
    await _messages(session, workspace, source, count=1)
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "target_scope": "chat_messages", "messages_per_source": 10}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert actions[0].target_type == "chat_message"
    assert actions[0].result["target_kind"] == "chat_message"


@pytest.mark.asyncio
async def test_mass_reactions_chat_scope_formats_numeric_group_peer(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    source.type = "group"
    source.username = None
    source.tg_id = "5166123290"
    await _messages(session, workspace, source, count=1)
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "target_scope": "chat_messages", "messages_per_source": 10}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_planned"] == 1
    assert run.progress["reactions_sent"] == 0
    assert actions[0].target_type == "chat_message"
    assert actions[0].result["source_ref"] == "-5166123290"
    assert actions[0].result["send"]["reaction_peer_ref"] == "-5166123290"


@pytest.mark.asyncio
async def test_mass_reactions_weighted_mode_uses_positive_weight(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    await _messages(session, workspace, source, count=1)
    campaign = _campaign(workspace, source, account)
    campaign.config = {
        **campaign.config,
        "reactions": ["🔥", "👍"],
        "reaction_weights": {"🔥": 0, "👍": 100},
    }
    campaign.policy = {**campaign.policy, "reaction_rotation": "weighted"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert actions[0].result["reaction"] == "👍"


@pytest.mark.asyncio
async def test_mass_reactions_ai_context_skips_sensitive_and_picks_context(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    monkeypatch.setattr(campaigns_service, "real_telegram_enabled", lambda: False)
    workspace = await _workspace(session)
    source, account = await _reaction_parts(session, workspace)
    now = datetime.now(UTC)
    session.add_all(
        [
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id="1000",
                text="Трагедия в городе, погиб человек",
                published_at=now,
                raw={},
            ),
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id="1001",
                text="Запуск нового продукта уже сегодня",
                published_at=now - timedelta(minutes=1),
                raw={},
            ),
        ]
    )
    campaign = _campaign(workspace, source, account)
    campaign.config = {**campaign.config, "reactions": ["🔥", "👍"]}
    campaign.policy = {**campaign.policy, "reaction_rotation": "ai_context"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    run = await campaigns_api.tick_campaign_run(run.id, session, workspace.id)

    actions = await _reaction_actions(session, campaign)
    assert run.progress["reactions_skipped_ai_context"] == 1
    assert len(actions) == 1
    assert actions[0].result["tg_message_id"] == "1001"
    assert actions[0].result["reaction"] == "🔥"


@pytest.mark.asyncio
async def test_mass_reactions_recent_too_many_blocks_account(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    _source, account = await _reaction_parts(session, workspace)
    account.daily_limits = {"last_reactions_too_many_at": datetime.now(UTC).isoformat()}
    await session.commit()

    eligibility = get_module_eligibility(account, "mass_reactions")

    assert eligibility.status == "blocked"
    assert eligibility.reason == "recent_reactions_too_many"
