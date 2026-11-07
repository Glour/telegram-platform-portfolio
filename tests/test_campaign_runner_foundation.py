from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.ai.models import AiProfile, CommentPromptTemplate
from app.modules.ai.service import AiProviderError
from app.modules.auth.models import User
from app.modules.campaigns import api as campaigns_api
from app.modules.campaigns import engine as campaigns_engine
from app.modules.campaigns.behavior_protection import apply_behavior_delay_range
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignEvent, CampaignRun
from app.modules.campaigns.schemas import (
    CampaignStopConditions,
    CommentPostsCampaignCreate,
    CommentPostsCampaignUpdate,
    MessageBroadcastCampaignCreate,
    NeuroChatCampaignCreate,
    NeuroDialogCampaignCreate,
)
from app.modules.campaigns.service import (
    _account_is_actionable,
    _policy_auto_join_daily_limit,
    _policy_auto_join_interval_seconds,
    _policy_auto_join_max_per_tick,
    _should_ensure_source_membership,
    build_campaign_comment_report,
    campaign_comment_report_csv,
)
from app.modules.comments.models import TelegramComment
from app.modules.operations.models import OperationalEvent
from app.modules.parsing.service import create_mock_posts
from app.modules.proxies.models import Proxy
from app.modules.sources import api as sources_api
from app.modules.sources.models import TelegramMessage, TelegramSource, TelegramSourceBlacklistEntry
from app.modules.sources.schemas import SourceBulkCreate, SourceFolderAssignRequest, SourceFolderCreate, SourcePatch
from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount, TelegramSession
from app.modules.telegram_gateway.contracts import (
    TelegramGatewayError,
    TelegramPostInfo,
    TelegramSendResult,
    TelegramSourceInfo,
)
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


async def _comment_campaign_parts(
    session: AsyncSession,
    workspace: Workspace,
    *,
    comments_enabled: bool = True,
) -> tuple[TelegramSource, TelegramAccount, CommentPromptTemplate]:
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="target_channel",
        title="Target channel",
        comments_enabled=comments_enabled,
        access_status="available",
    )
    proxy = Proxy(workspace_id=workspace.id, type="socks5", host="127.0.0.1", port=1080, status="active")
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="commenter",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {
                "status": "clear",
                "checked_at": datetime.now(UTC).isoformat(),
                "evidence_source": "operator_admission",
            },
        },
    )
    profile = AiProfile(
        workspace_id=workspace.id,
        name="Expert",
        system_prompt="Write helpful comments",
    )
    session.add_all([source, account, profile])
    await session.flush()
    prompt = CommentPromptTemplate(
        workspace_id=workspace.id,
        name="Default",
        ai_profile_id=profile.id,
        prompt_text="Write a thoughtful expert comment",
        max_length=280,
        blacklist_words=[],
    )
    session.add(prompt)
    await session.flush()
    return source, account, prompt


def _campaign(
    workspace: Workspace,
    source: TelegramSource,
    account: TelegramAccount,
    prompt: CommentPromptTemplate,
    *,
    mode: str,
    latest_posts_per_source: int = 10,
    stop_conditions: dict | None = None,
) -> Campaign:
    return Campaign(
        workspace_id=workspace.id,
        type="comment_posts",
        name="Neurocomments",
        status="draft",
        config={
            "source_ids": [source.id],
            "account_ids": [account.id],
            "comment_prompt_template_id": prompt.id,
            "latest_posts_per_source": latest_posts_per_source,
            "mode": mode,
            "stop_conditions": stop_conditions or {},
        },
        policy={},
    )


def _author_gender_schema_cases() -> dict[str, tuple[type, dict]]:
    return {
        "comment_posts": (
            CommentPostsCampaignCreate,
            {
                "name": "Comments",
                "source_ids": ["source-id"],
                "account_ids": ["account-id"],
                "comment_prompt_template_id": "prompt-id",
            },
        ),
        "neuro_chat": (
            NeuroChatCampaignCreate,
            {
                "name": "Chat",
                "source_ids": ["source-id"],
                "account_ids": ["account-id"],
                "prompt_text": "Ответь в чате живо и коротко",
            },
        ),
        "neuro_dialog": (
            NeuroDialogCampaignCreate,
            {
                "name": "Dialog",
                "account_ids": ["account-id"],
                "target_refs": ["@client"],
                "prompt_text": "Ответь клиенту живо и коротко",
            },
        ),
        "message_broadcast": (
            MessageBroadcastCampaignCreate,
            {
                "name": "Broadcast",
                "source_ids": ["source-id"],
                "account_ids": ["account-id"],
                "message_text": "Напиши первое сообщение клиенту",
            },
        ),
    }


def test_campaign_create_schemas_default_author_gender_to_neutral() -> None:
    actual = {}
    for campaign_type, (schema_cls, kwargs) in _author_gender_schema_cases().items():
        payload = schema_cls(**kwargs)
        actual[campaign_type] = {
            "attribute": getattr(payload, "author_gender", None),
            "model_dump": payload.model_dump().get("author_gender"),
        }

    assert actual == {
        "comment_posts": {"attribute": "neutral", "model_dump": "neutral"},
        "neuro_chat": {"attribute": "neutral", "model_dump": "neutral"},
        "neuro_dialog": {"attribute": "neutral", "model_dump": "neutral"},
        "message_broadcast": {"attribute": "neutral", "model_dump": "neutral"},
    }


def test_campaign_create_schemas_accept_author_gender_values() -> None:
    actual = {}
    for campaign_type, (schema_cls, kwargs) in _author_gender_schema_cases().items():
        actual[campaign_type] = {}
        for author_gender in ("masculine", "feminine", "neutral"):
            payload = schema_cls(**{**kwargs, "author_gender": author_gender})
            actual[campaign_type][author_gender] = getattr(payload, "author_gender", None)

    assert actual == {
        campaign_type: {
            "masculine": "masculine",
            "feminine": "feminine",
            "neutral": "neutral",
        }
        for campaign_type in _author_gender_schema_cases()
    }


def test_campaign_create_schemas_reject_unknown_author_gender() -> None:
    rejected = {}
    for campaign_type, (schema_cls, kwargs) in _author_gender_schema_cases().items():
        try:
            schema_cls(**{**kwargs, "author_gender": "robot"})
        except ValidationError:
            rejected[campaign_type] = True
        else:
            rejected[campaign_type] = False

    assert rejected == {campaign_type: True for campaign_type in _author_gender_schema_cases()}


def test_behavior_protection_forces_conservative_mode_for_new_accounts() -> None:
    account = TelegramAccount(workspace_id="workspace", status="new", username="fresh")

    delay_min, delay_max, snapshot = apply_behavior_delay_range(
        {"behavior_protection_mode": "fast"},
        [account],
        60,
        120,
        now=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
    )

    assert (delay_min, delay_max) == (90, 180)
    assert snapshot["mode"] == "conservative"
    assert snapshot["typing_cpm_min"] == 40
    assert snapshot["profile_view_probability"] == 90


def test_behavior_protection_fast_mode_keeps_hard_delay_floor() -> None:
    account = TelegramAccount(workspace_id="workspace", status="active", username="trusted")

    delay_min, delay_max, snapshot = apply_behavior_delay_range(
        {"behavior_protection_mode": "fast"},
        [account],
        600,
        900,
        floor_min=600,
    )

    assert delay_min == 600
    assert delay_max == 630
    assert snapshot["mode"] == "fast"


@pytest.mark.asyncio
async def test_campaign_create_persists_author_gender_in_message_generating_config(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    comment_source, account, prompt = await _comment_campaign_parts(session, workspace)
    group_source = TelegramSource(
        workspace_id=workspace.id,
        type="group",
        username="chat_target",
        title="Chat target",
        access_status="available",
    )
    dialog_source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="dialog_target",
        title="Dialog target",
        access_status="available",
    )
    broadcast_source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="broadcast_target",
        title="Broadcast target",
        access_status="available",
    )
    session.add_all([group_source, dialog_source, broadcast_source])
    await session.flush()

    campaigns_by_type = {
        "comment_posts": await campaigns_api.create_comment_posts_campaign(
            CommentPostsCampaignCreate(
                name="Comments voice",
                source_ids=[comment_source.id],
                account_ids=[account.id],
                comment_prompt_template_id=prompt.id,
                author_gender="feminine",
            ),
            session,
            workspace.id,
            user,
        ),
        "neuro_chat": await campaigns_api.create_neuro_chat_campaign(
            NeuroChatCampaignCreate(
                name="Chat voice",
                source_ids=[group_source.id],
                account_ids=[account.id],
                prompt_text="Отвечай в группе живо и по делу",
                author_gender="masculine",
            ),
            session,
            workspace.id,
            user,
        ),
        "neuro_dialog": await campaigns_api.create_neuro_dialog_campaign(
            NeuroDialogCampaignCreate(
                name="Dialog voice",
                source_ids=[dialog_source.id],
                account_ids=[account.id],
                prompt_text="Отвечай клиентам коротко и человечно",
                author_gender="neutral",
            ),
            session,
            workspace.id,
            user,
        ),
        "message_broadcast": await campaigns_api.create_message_broadcast_campaign(
            MessageBroadcastCampaignCreate(
                name="Broadcast voice",
                source_ids=[broadcast_source.id],
                account_ids=[account.id],
                message_text="Напиши первое сообщение о продукте без давления",
                author_gender="feminine",
            ),
            session,
            workspace.id,
            user,
        ),
    }

    assert {
        campaign_type: campaign.config.get("author_gender") for campaign_type, campaign in campaigns_by_type.items()
    } == {
        "comment_posts": "feminine",
        "neuro_chat": "masculine",
        "neuro_dialog": "neutral",
        "message_broadcast": "feminine",
    }


@pytest.mark.asyncio
async def test_message_broadcast_uses_behavior_protection_queue(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="dialog",
        username="broadcast_target",
        title="Broadcast target",
        access_status="available",
    )
    proxy = Proxy(workspace_id=workspace.id, type="socks5", host="127.0.0.1", port=1080, status="active")
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="broadcaster",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {
                "status": "clear",
                "checked_at": datetime.now(UTC).isoformat(),
                "evidence_source": "operator_admission",
            }
        },
    )
    session.add_all([source, account])
    await session.flush()
    campaign = await campaigns_api.create_message_broadcast_campaign(
        MessageBroadcastCampaignCreate(
            name="Broadcast",
            source_ids=[source.id],
            account_ids=[account.id],
            message_text="Привет, короткое сообщение без давления",
            delay_seconds_min=900,
            delay_seconds_max=900,
            policy={"behavior_protection_mode": "conservative"},
        ),
        session,
        workspace.id,
        user,
    )
    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    before_tick = datetime.now(UTC)
    await campaigns_engine.tick_generic_campaign_run(session, workspace.id, run)

    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert len(actions) == 1
    action = actions[0]
    assert action.status == "queued"
    assert action.executed_at is None
    assert action.planned_at is not None
    assert action.planned_at.replace(tzinfo=UTC) >= before_tick + timedelta(seconds=1349)
    assert action.result["send"]["mode"] == "queued"
    assert action.result["behavior_protection"]["mode"] == "conservative"
    assert action.result["behavior_protection"]["effective_delay_seconds_min"] >= 1350


@pytest.mark.asyncio
async def test_bulk_source_create_reuses_normalized_refs(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)

    first = await sources_api.bulk_create_sources(
        SourceBulkCreate(refs="@Target_Channel\nhttps://t.me/target_channel\nother_channel"),
        session,
        workspace.id,
    )
    second = await sources_api.bulk_create_sources(
        SourceBulkCreate(refs=["@target_channel", "t.me/other_channel"]),
        session,
        workspace.id,
    )

    assert first["created"] == 2
    assert first["reused"] == 1
    assert second["created"] == 0
    assert second["reused"] == 2


@pytest.mark.asyncio
async def test_source_comment_check_marks_closed_comments_invalid(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="closed_channel",
        title="Closed channel",
        comments_enabled=True,
        access_status="pending_check",
    )
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="checker",
        status="active",
        import_type="session",
    )
    session.add_all([source, account])
    await session.commit()

    async def fake_first_account(*args: object, **kwargs: object) -> TelegramAccount:
        return account

    async def fake_inspect_source(*args: object, **kwargs: object) -> TelegramSourceInfo:
        return TelegramSourceInfo(
            tg_id=123,
            username="closed_channel",
            title="Closed channel",
            source_type="channel",
            comments_enabled=False,
            linked_discussion_tg_id=None,
            linked_discussion_username=None,
            metadata={"source_type": "channel"},
        )

    monkeypatch.setattr(sources_api, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(sources_api, "_first_telegram_account", fake_first_account)
    monkeypatch.setattr(sources_api, "inspect_source_with_account", fake_inspect_source)

    checked = await sources_api.check_comments(source.id, session, workspace.id)

    assert checked.access_status == "invalid_comments_closed"
    assert checked.comments_enabled is False
    assert checked.source_metadata["invalid"] is True
    assert checked.source_metadata["invalid_reason"] == "source_comments_not_available"


@pytest.mark.asyncio
async def test_source_comment_check_marks_private_source_invalid_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="private_channel",
        title="Private channel",
        comments_enabled=False,
        access_status="pending_check",
    )
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="checker",
        status="active",
        import_type="session",
    )
    session.add_all([source, account])
    await session.commit()

    async def fake_first_account(*args: object, **kwargs: object) -> TelegramAccount:
        return account

    async def fake_inspect_source(*args: object, **kwargs: object) -> TelegramSourceInfo:
        raise TelegramGatewayError("ChannelPrivateError", "The channel specified is private")

    monkeypatch.setattr(sources_api, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(sources_api, "_first_telegram_account", fake_first_account)
    monkeypatch.setattr(sources_api, "inspect_source_with_account", fake_inspect_source)

    checked = await sources_api.check_comments(source.id, session, workspace.id)

    assert checked.access_status == "invalid_private"
    assert checked.comments_enabled is False
    assert checked.source_metadata["invalid_reason"] == "source_not_accessible"
    assert checked.source_metadata["last_error"]["code"] == "ChannelPrivateError"


@pytest.mark.asyncio
async def test_source_comment_check_marks_unresolved_discussion_invalid(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="join_request_discussion",
        title="Join request discussion",
        comments_enabled=False,
        access_status="pending_check",
    )
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="checker",
        status="active",
        import_type="session",
    )
    session.add_all([source, account])
    await session.commit()

    async def fake_first_account(*args: object, **kwargs: object) -> TelegramAccount:
        return account

    async def fake_inspect_source(*args: object, **kwargs: object) -> TelegramSourceInfo:
        return TelegramSourceInfo(
            tg_id=123,
            username="join_request_discussion",
            title="Join request discussion",
            source_type="channel",
            comments_enabled=True,
            linked_discussion_tg_id=456,
            linked_discussion_username=None,
            metadata={"source_type": "channel", "linked_discussion_resolved": False},
        )

    monkeypatch.setattr(sources_api, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(sources_api, "_first_telegram_account", fake_first_account)
    monkeypatch.setattr(sources_api, "inspect_source_with_account", fake_inspect_source)

    checked = await sources_api.check_comments(source.id, session, workspace.id)

    assert checked.access_status == "invalid_join_request"
    assert checked.comments_enabled is False
    assert checked.source_metadata["invalid_reason"] == "discussion_join_approval_required"


@pytest.mark.asyncio
async def test_source_safety_summary_and_bulk_archive_invalid(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    safe = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="safe_source",
        comments_enabled=True,
        access_status="available",
    )
    invalid = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="closed_source",
        comments_enabled=False,
        access_status="invalid_comments_closed",
        source_metadata={"invalid": True, "invalid_reason": "source_comments_not_available"},
    )
    unknown = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="unchecked_source",
        comments_enabled=False,
        access_status="pending_check",
    )
    session.add_all([safe, invalid, unknown])
    await session.commit()

    summary = await sources_api.safety_summary(session, workspace.id)

    assert summary["total"] == 3
    assert summary["safe"] == 1
    assert summary["critical"] == 1
    assert summary["unknown"] == 1
    assert summary["comments_closed"] == 1
    assert safe.safe_for_new_accounts is True
    assert safe.source_risk_level == "low"
    assert invalid.safe_for_new_accounts is False
    assert invalid.source_risk_level == "critical"
    assert unknown.safe_for_new_accounts is False
    assert unknown.source_risk_level == "unknown"

    result = await sources_api.bulk_archive_invalid(
        sources_api.SourceBulkArchiveInvalidRequest(),
        session,
        workspace.id,
    )

    assert result.archived == 1
    await session.refresh(invalid)
    await session.refresh(unknown)
    assert invalid.access_status == "archived"
    assert unknown.access_status == "pending_check"


@pytest.mark.asyncio
async def test_manual_source_available_status_keeps_comments_closed_but_clears_safety(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="content_only",
        comments_enabled=False,
        access_status="invalid_comments_closed",
        source_metadata={"invalid": True, "invalid_reason": "source_comments_not_available"},
    )
    session.add(source)
    await session.commit()

    updated = await sources_api.update_source(
        source.id,
        SourcePatch(access_status="available", comments_enabled=False),
        session,
        workspace.id,
    )

    assert updated.access_status == "available"
    assert updated.comments_enabled is False
    assert updated.source_metadata["invalid"] is False
    assert updated.source_metadata["manual_status_override"] is True
    assert updated.source_risk_level == "low"
    assert updated.safe_for_new_accounts is True


@pytest.mark.asyncio
async def test_source_blacklist_api_hides_and_restores_source(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="blocked_channel",
        title="Blocked channel",
        comments_enabled=True,
        access_status="available",
    )
    session.add(source)
    await session.commit()

    entry = await sources_api.block_source(
        source.id,
        sources_api.SourceBlacklistRequest(reason="manual_bad_source"),
        session,
        workspace.id,
    )
    visible_sources = await sources_api.list_sources(session, workspace.id)
    blacklist = await sources_api.list_source_blacklist(session, workspace.id)

    assert entry["reason"] == "manual_bad_source"
    assert source.access_status == "blacklisted"
    assert visible_sources == []
    assert len(blacklist) == 1
    assert blacklist[0]["source_id"] == source.id

    restored = await sources_api.unblock_source(source.id, session, workspace.id)
    visible_sources = await sources_api.list_sources(session, workspace.id)

    assert restored is not None
    assert restored["status"] == "resolved"
    assert source.access_status == "pending_check"
    assert [item.id for item in visible_sources] == [source.id]


@pytest.mark.asyncio
async def test_comment_campaign_safety_preset_preserves_explicit_limits(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.status = "new"
    account.warmup_status = "not_started"
    account.warmup_score = 0
    await session.commit()

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Unsafe requested",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            delivery_mode="generate_and_send",
            policy={
                "daily_limit_per_account": 25,
                "delay_seconds_min": 30,
                "delay_seconds_max": 90,
                "auto_join_sources": True,
                "auto_join_discussions": True,
            },
        ),
        session,
        workspace.id,
        user,
    )

    assert campaign.policy["safety_preset"] == "first_launch"
    assert campaign.policy["daily_limit_per_account"] == 25
    assert campaign.policy["delay_seconds_min"] == 30
    assert campaign.policy["delay_seconds_max"] == 90
    assert campaign.policy["auto_join_sources"] is True
    assert campaign.policy["auto_join_discussions"] is True
    assert campaign.policy["safety_adjustments"]


@pytest.mark.asyncio
async def test_comment_campaign_allows_unverified_manual_link_sources(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=False)
    source.access_status = "pending_check"
    await session.commit()

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Manual link comments",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            delivery_mode="generate_and_send",
            policy={"auto_join_sources": True, "auto_join_discussions": True},
        ),
        session,
        workspace.id,
        user,
    )

    assert campaign.config["source_ids"] == [source.id]
    assert campaign.policy["auto_join_sources"] is True
    assert campaign.policy["auto_join_discussions"] is True

    safety = await campaigns_api.campaign_safety(campaign.id, session, workspace.id)
    assert safety.can_start is True
    assert safety.status == "limited"
    assert safety.sources[0].status == "limited"
    assert safety.sources[0].reason == "source_status_pending_check"


@pytest.mark.asyncio
async def test_comment_campaign_legacy_standard_join_preset_downgrades_to_standard(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.status = "active"
    account.warmup_status = "completed"
    account.warmup_score = 100
    account.daily_limits = {"restriction_status": {"status": "clear"}}
    await session.commit()

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Controlled join campaign",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            delivery_mode="generate_and_send",
            policy={"safety_preset": "standard_with_join"},
        ),
        session,
        workspace.id,
        user,
    )

    assert campaign.policy["safety_preset"] == "standard"
    assert campaign.policy["auto_join_sources"] is False
    assert campaign.policy["auto_join_discussions"] is False
    assert "auto_join_interval_seconds" not in campaign.policy
    assert "auto_join_daily_limit" not in campaign.policy
    assert "auto_join_max_per_tick" not in campaign.policy


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["first_launch", "standard", "not_a_preset"])
async def test_comment_campaign_save_preserves_disabled_auto_joins(session: AsyncSession, preset: str) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="No automatic memberships",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            policy={"safety_preset": preset, "auto_join_sources": False, "auto_join_discussions": False},
        ),
        session,
        workspace.id,
        user,
    )
    assert campaign.policy["auto_join_sources"] is False
    assert campaign.policy["auto_join_discussions"] is False
    updated = await campaigns_api.update_campaign(
        campaign.id,
        CommentPostsCampaignUpdate(policy={**campaign.policy, "daily_limit_per_account": 5}),
        session,
        workspace.id,
    )
    await session.refresh(updated)
    assert updated.policy["auto_join_sources"] is False
    assert updated.policy["auto_join_discussions"] is False
    assert updated.policy["daily_limit_per_account"] == 5


@pytest.mark.asyncio
async def test_comment_campaign_update_preserves_limits_and_syncs_max_length(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.status = "new"
    account.warmup_status = "not_started"
    account.warmup_score = 0
    await session.commit()

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Editable",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            delivery_mode="generate_and_send",
            policy={
                "daily_limit_per_account": 3,
                "delay_seconds_min": 600,
                "delay_seconds_max": 1800,
                "max_length": 110,
                "safety_preset": "first_launch",
            },
        ),
        session,
        workspace.id,
        user,
    )

    updated = await campaigns_api.update_campaign(
        campaign.id,
        CommentPostsCampaignUpdate(
            policy={
                **campaign.policy,
                "daily_limit_per_account": 30,
                "delay_seconds_min": 150,
                "delay_seconds_max": 300,
                "max_length": 120,
                "safety_preset": "first_launch",
            },
            max_length=120,
        ),
        session,
        workspace.id,
    )

    await session.refresh(prompt)
    assert updated.policy["safety_preset"] == "first_launch"
    assert updated.policy["daily_limit_per_account"] == 30
    assert updated.policy["delay_seconds_min"] == 150
    assert updated.policy["delay_seconds_max"] == 300
    assert updated.policy["max_length"] == 120
    assert prompt.max_length == 120


def test_comment_interval_comments_first_post_per_source() -> None:
    policy = {
        "comment_post_interval_mode": "random_range",
        "comment_post_interval_min": 2,
        "comment_post_interval_max": 5,
    }

    first_source, counters, targets = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters={},
        targets={"source-a": 5},
        source_id="source-a",
    )
    other_source, counters, targets = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters=counters,
        targets={**targets, "source-b": 5},
        source_id="source-b",
    )

    assert first_source is True
    assert other_source is True
    assert counters["source-a"] == 0
    assert counters["source-b"] == 0


def test_comment_interval_isolated_per_source_after_first_comment() -> None:
    policy = {
        "comment_post_interval_mode": "fixed",
        "comment_post_interval": 3,
    }

    first_a, counters, _ = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters={},
        targets={},
        source_id="source-a",
    )
    first_b, counters, _ = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters=counters,
        targets={},
        source_id="source-b",
    )
    second_a, counters, _ = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters=counters,
        targets={},
        source_id="source-a",
    )
    third_a, counters, _ = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters=counters,
        targets={},
        source_id="source-a",
    )
    fourth_a, counters, _ = campaigns_engine._next_comment_post_decision(
        policy=policy,
        counters=counters,
        targets={},
        source_id="source-a",
    )

    assert first_a is True
    assert first_b is True
    assert second_a is False
    assert third_a is False
    assert fourth_a is True
    assert counters["source-a"] == 0
    assert counters["source-b"] == 0


def test_comment_interval_legacy_state_is_reset() -> None:
    counters, targets, reset = campaigns_engine._comment_interval_state(
        {
            "comment_post_interval_counters": {"source-a": 1},
            "comment_post_interval_targets": {"source-a": 5},
        }
    )

    assert counters == {}
    assert targets == {}
    assert reset is True


def test_live_comment_account_prefers_listener_account() -> None:
    first = TelegramAccount(
        workspace_id="workspace",
        username="first",
        status="active",
        import_type="session",
        health_score=90,
    )
    second = TelegramAccount(
        workspace_id="workspace",
        username="second",
        status="active",
        import_type="session",
        health_score=90,
    )
    first.id = "account-first"
    second.id = "account-second"

    assert campaigns_engine._select_live_comment_account([first, second], "account-second") is second
    assert campaigns_engine._select_live_comment_account([first, second], "missing") is first


def test_comment_daily_limit_uses_configured_value() -> None:
    assert campaigns_engine._comment_daily_limit({"daily_limit_per_account": 15}) == 15


def test_new_post_campaign_uses_polling() -> None:
    campaign = Campaign(
        workspace_id="workspace",
        type="comment_posts",
        name="Live",
        config={"post_selection_mode": "new_posts"},
        policy={"live_listener_enabled": False},
    )

    assert campaigns_engine._campaign_uses_live_listener(campaign) is False


def test_new_post_mass_reactions_use_polling_for_posts() -> None:
    campaign = Campaign(
        workspace_id="workspace",
        type="mass_reactions",
        name="Live reactions",
        config={"post_selection_mode": "new_posts", "target_scope": "posts"},
        policy={},
    )

    assert campaigns_engine._campaign_uses_live_listener(campaign) is False


def test_comment_sources_for_tick_rotates_small_batches() -> None:
    sources = [
        TelegramSource(workspace_id="workspace", type="channel", title=f"Source {index}", access_status="available")
        for index in range(5)
    ]

    batch, delta = campaigns_engine._comment_sources_for_tick(
        sources,
        {"source_rotation_index": 3},
        {},
        throttle_by_default=True,
    )

    assert [source.title for source in batch] == ["Source 3", "Source 4", "Source 0"]
    assert delta == 3


@pytest.mark.asyncio
async def test_tick_active_campaign_runs_skips_second_run_with_same_account(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    first_campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    second_campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    first_campaign.status = "running"
    second_campaign.status = "running"
    session.add_all([first_campaign, second_campaign])
    await session.flush()
    old_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    first_run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=first_campaign.id,
        status="running",
        started_at=old_heartbeat,
        heartbeat_at=old_heartbeat,
        stop_conditions={},
        progress={},
        report={},
    )
    second_run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=second_campaign.id,
        status="running",
        started_at=old_heartbeat,
        heartbeat_at=old_heartbeat,
        stop_conditions={},
        progress={},
        report={},
    )
    session.add_all([first_run, second_run])
    await session.flush()

    ticked_run_ids: list[str] = []

    async def fake_tick(_session: AsyncSession, _workspace_id: str, run: CampaignRun) -> CampaignRun:
        ticked_run_ids.append(run.id)
        return run

    monkeypatch.setattr(campaigns_engine, "tick_generic_campaign_run", fake_tick)

    ticked = await campaigns_engine.tick_active_campaign_runs(session, workspace.id, limit=20)

    assert [run.id for run in ticked] == [first_run.id]
    assert ticked_run_ids == [first_run.id]


@pytest.mark.asyncio
async def test_scheduled_tick_skips_recent_heartbeat(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
        stop_conditions={},
        progress={},
        report={},
    )
    session.add(run)
    await session.flush()

    async def fail_tick(*_args: object, **_kwargs: object) -> CampaignRun:
        raise AssertionError("recent run must not be ticked by scheduled scan")

    monkeypatch.setattr(campaigns_engine, "tick_generic_campaign_run", fail_tick)

    ticked = await campaigns_engine.tick_active_campaign_runs(session, workspace.id, limit=20)

    assert ticked == []


@pytest.mark.asyncio
async def test_scheduled_tick_polls_live_mass_reactions_as_fallback(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    old_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    campaign = Campaign(
        workspace_id=workspace.id,
        type="mass_reactions",
        name="Live reactions",
        status="running",
        config={
            "post_selection_mode": "new_posts",
            "target_scope": "posts",
            "source_ids": ["source-1"],
            "account_ids": ["account-1"],
            "source_cursors": {
                "source-1": {
                    "last_seen_tg_message_id": "100",
                    "last_seen_target_kind": "telegram_message",
                    "last_seen_at": old_heartbeat.isoformat(),
                }
            },
        },
        policy={},
    )
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=old_heartbeat,
        heartbeat_at=old_heartbeat,
        stop_conditions={},
        progress={},
        report={},
    )
    session.add(run)
    await session.flush()

    ticked_run_ids: list[str] = []

    async def fake_tick(_session: AsyncSession, _workspace_id: str, run: CampaignRun) -> CampaignRun:
        ticked_run_ids.append(run.id)
        return run

    monkeypatch.setattr(campaigns_engine, "tick_generic_campaign_run", fake_tick)

    ticked = await campaigns_engine.tick_active_campaign_runs(session, workspace.id, limit=20)

    assert [item.id for item in ticked] == [run.id]
    assert ticked_run_ids == [run.id]


@pytest.mark.asyncio
async def test_live_comment_tick_catches_up_new_posts(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Полезный комментарий к новому посту.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.engine.generate_comment_text", fake_generate)
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="generate_and_send",
    )
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
    }
    campaign.policy = {"min_post_chars": 10}
    session.add(campaign)
    await session.flush()
    started_at = datetime.now(UTC) - timedelta(minutes=10)
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
        stop_conditions={},
        progress={},
        report={},
    )
    session.add(run)
    session.add(
        TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id="live-batch-catch-up",
            text="Новый пост после запуска кампании, который должен подобрать recovery tick.",
            published_at=started_at + timedelta(minutes=1),
        )
    )
    await session.flush()

    result = await campaigns_engine.tick_comment_posts_campaign_run(session, workspace.id, run)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert result.status == "running"
    assert result.progress["ticks"] == 1
    assert result.progress["sources_checked"] == 1
    assert result.progress["cursors_initialized"] == 1
    assert result.progress["new_posts_found"] == 1
    assert result.progress["comments_sent"] == 1
    assert len(comments) == 1
    assert len(actions) == 1
    events = await session.execute(select(CampaignEvent).where(CampaignEvent.run_id == run.id))
    assert [event.event_type for event in events.scalars().all()] == [
        "tick_started",
        "source_monitoring_started",
        "source_posts_loaded",
        "source_posts_filtered",
        "comment_generation_started",
        "comment_generated",
        "comment_sent",
        "tick_completed",
    ]


@pytest.mark.asyncio
async def test_comment_ai_provider_failure_fails_post_without_stopping_run(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def flaky_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AiProviderError("AI_PROVIDER_REQUEST_ERROR: [Errno -3] Temporary failure in name resolution")
        return "Полезный комментарий после временного сбоя.", {"mode": "test_ai"}

    monkeypatch.setattr(campaigns_engine, "generate_comment_text", flaky_generate)
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="generate_and_send",
    )
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
    }
    campaign.policy = {"min_post_chars": 10}
    session.add(campaign)
    await session.flush()
    started_at = datetime.now(UTC) - timedelta(minutes=10)
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
        stop_conditions={},
        progress={},
        report={},
    )
    session.add(run)
    session.add_all(
        [
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id="ai-fails-on-this-post",
                text="Новый пост после запуска кампании, на котором AI временно упадет.",
                published_at=started_at + timedelta(minutes=1),
            ),
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id="ai-recovers-on-this-post",
                text="Еще один новый пост после запуска кампании, который должен отправиться.",
                published_at=started_at + timedelta(minutes=2),
            ),
        ]
    )
    await session.flush()

    result = await campaigns_engine.tick_comment_posts_campaign_run(session, workspace.id, run)
    comments = list((await session.execute(select(TelegramComment).order_by(TelegramComment.created_at))).scalars())
    actions = list((await session.execute(select(CampaignAction).order_by(CampaignAction.created_at))).scalars())
    events = list((await session.execute(select(CampaignEvent).where(CampaignEvent.run_id == run.id))).scalars())

    assert result.status == "running"
    assert result.error_code is None
    assert result.progress["actions_created"] == 2
    assert result.progress["comments_failed"] == 1
    assert result.progress["comments_sent"] == 1
    assert result.progress["comments_generated"] == 1
    assert [action.status for action in actions] == ["failed", "succeeded"]
    assert [comment.status for comment in comments] == ["failed", "sent"]
    assert comments[0].error_code == "AI_PROVIDER_REQUEST_ERROR"
    assert "comment_generation_failed" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_new_post_daily_limit_waits_without_completing_run(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
        "latest_posts_per_source": 5,
    }
    campaign.policy = {"daily_limit_per_account": 1, "min_post_chars": 10}
    session.add(campaign)
    await session.flush()
    started_at = datetime.now(UTC) - timedelta(minutes=30)
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
        stop_conditions={},
        progress={},
        report={},
    )
    old_message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="already-commented",
        text="Старый пост, по которому уже был комментарий.",
        published_at=started_at + timedelta(minutes=1),
    )
    new_message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="new-after-limit",
        text="Новый содержательный пост после запуска кампании.",
        published_at=started_at + timedelta(minutes=10),
    )
    session.add_all([run, old_message, new_message])
    await session.flush()
    session.add(
        TelegramComment(
            workspace_id=workspace.id,
            source_id=source.id,
            post_message_id=old_message.id,
            campaign_id=campaign.id,
            telegram_account_id=account.id,
            text="Уже отправленный комментарий.",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    await session.flush()

    result = await campaigns_engine.tick_comment_posts_campaign_run(session, workspace.id, run)

    assert result.status == "running"
    assert result.finished_at is None
    assert result.progress["last_wait_reason"] == "daily_limit"
    assert "stop_reason" not in result.progress
    events = await session.execute(select(CampaignEvent).where(CampaignEvent.run_id == run.id))
    assert [event.event_type for event in events.scalars().all()] == [
        "tick_started",
        "source_monitoring_started",
        "source_posts_loaded",
        "source_posts_filtered",
        "run_waiting",
    ]


@pytest.mark.asyncio
async def test_new_post_polling_tick_processes_only_posts_after_run_start(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Комментарий только к посту после запуска.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.engine.generate_comment_text", fake_generate)
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.daily_limits = {
        "restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()},
    }
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
        "latest_posts_per_source": 5,
    }
    campaign.policy = {"min_post_chars": 10}
    session.add(campaign)
    await session.flush()
    started_at = datetime.now(UTC) - timedelta(hours=1)
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
        stop_conditions={},
        progress={},
        report={},
    )
    session.add(run)
    old_message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="old-before-run",
        text="Старый пост до запуска кампании",
        published_at=started_at - timedelta(minutes=5),
    )
    new_message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="new-after-run",
        text="Новый содержательный пост после запуска кампании",
        published_at=started_at + timedelta(minutes=5),
    )
    session.add_all([old_message, new_message])
    await session.flush()

    result = await campaigns_engine.tick_comment_posts_campaign_run(session, workspace.id, run)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert result.progress["cursors_initialized"] == 1
    assert result.progress["new_posts_found"] == 1
    assert result.progress["comments_sent"] == 1
    assert len(comments) == 1
    assert comments[0].post_message_id == new_message.id
    assert len(actions) == 1
    assert actions[0].target_id == new_message.id


@pytest.mark.asyncio
async def test_live_comment_recovery_ignores_posts_before_run_start(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
    }
    session.add(campaign)
    await session.flush()

    run_started_at = datetime.now(UTC)
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=run_started_at,
        heartbeat_at=run_started_at,
        stop_conditions={},
        progress={},
        report={},
    )
    message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="before-run",
        text="Post that existed before the campaign was started",
        published_at=run_started_at - timedelta(minutes=1),
    )
    session.add_all([run, message])
    await session.flush()

    result = await campaigns_engine.process_live_comment_message(
        session,
        workspace.id,
        campaign,
        run,
        source,
        message,
    )

    assert result.progress == {}
    events = await session.execute(select(CampaignEvent).where(CampaignEvent.run_id == run.id))
    assert events.scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_source_create_attaches_channel_map_theme(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)

    async def fake_load_atlas() -> dict:
        return {
            "clusters": [{"id": 10, "label": "Маркетинг"}],
            "sub_clusters": [{"id": 101, "label": "Яндекс Директ", "parent_id": 10}],
            "channels": [[None, None, 10, "target_channel", "Target channel", 5000, "", "777", 0]],
            "meta": {},
        }

    monkeypatch.setattr(sources_api, "load_atlas", fake_load_atlas)

    result = await sources_api.bulk_create_sources(
        SourceBulkCreate(refs="@target_channel", type="channel"),
        session,
        workspace.id,
    )

    source = result["items"][0]["source"]
    assert source.source_metadata["channel_map"] == {
        "cluster_id": 10,
        "cluster_label": "Маркетинг",
        "sub_cluster_id": 101,
        "sub_cluster_label": "Яндекс Директ",
    }


@pytest.mark.asyncio
async def test_source_list_backfills_channel_map_theme(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="target_channel",
        title="Target channel",
        access_status="available",
        source_metadata={},
    )
    session.add(source)
    await session.commit()

    async def fake_load_atlas() -> dict:
        return {
            "clusters": [{"id": 10, "label": "Маркетинг"}],
            "sub_clusters": [{"id": 101, "label": "Яндекс Директ", "parent_id": 10}],
            "channels": [[None, None, 10, "target_channel", "Target channel", 5000, "", "777", 0]],
            "meta": {},
        }

    monkeypatch.setattr(sources_api, "load_atlas", fake_load_atlas)

    sources = await sources_api.list_sources(session, workspace.id)

    assert sources[0].source_metadata["channel_map"]["sub_cluster_label"] == "Яндекс Директ"


@pytest.mark.asyncio
async def test_campaign_list_marks_active_run_even_when_campaign_status_is_stale(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.status = "cancelled"
    session.add(campaign)
    await session.flush()
    session.add(
        CampaignRun(
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            status="running",
            progress={},
            stop_conditions={},
            report={},
        )
    )
    await session.commit()

    campaigns = await campaigns_api.list_campaigns(session, workspace.id)

    assert campaigns[0].status == "cancelled"
    assert campaigns[0].active_run_status == "running"


@pytest.mark.asyncio
async def test_create_campaign_accepts_new_post_mode_stop_conditions(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    stop_at = datetime.now(UTC) + timedelta(hours=1)

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Monitor new posts",
            source_ids=[source.id],
            account_ids=[account.id],
            comment_prompt_template_id=prompt.id,
            latest_posts_per_source=5,
            mode="comment_new_posts",
            stop_conditions=CampaignStopConditions(
                max_comments=3,
                run_for_minutes=20,
                stop_at=stop_at,
                active_window={"start": "09:00", "end": "18:00"},
            ),
        ),
        session,
        workspace.id,
        user,
    )

    assert campaign.config["mode"] == "comment_new_posts"
    assert campaign.config["stop_conditions"]["max_comments"] == 3
    assert campaign.config["stop_conditions"]["run_for_minutes"] == 20
    assert campaign.policy["stop_words_enabled"] is True
    assert "реклама" in campaign.policy["stop_words"]
    stored_stop_at = datetime.fromisoformat(campaign.config["stop_conditions"]["stop_at"].replace("Z", "+00:00"))
    assert stored_stop_at == stop_at


@pytest.mark.asyncio
async def test_campaign_safety_blocks_limited_account_start(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.status = "quarantined"
    account.daily_limits = {"restriction_status": {"status": "limited"}}
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    session.add(campaign)
    await session.commit()

    safety = await campaigns_api.campaign_safety(campaign.id, session, workspace.id)

    assert safety.can_start is False
    assert safety.status == "blocked"
    assert safety.accounts[0].reason == "telegram_restriction_limited"

    with pytest.raises(HTTPException) as exc:
        await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    assert exc.value.status_code == 400
    assert "safety preflight" in exc.value.detail


@pytest.mark.asyncio
async def test_campaign_safety_does_not_block_high_risk_account_start(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.health_score = 0
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    session.add(campaign)
    await session.commit()

    safety = await campaigns_api.campaign_safety(campaign.id, session, workspace.id)

    assert safety.can_start is True
    assert safety.status == "allowed"


@pytest.mark.asyncio
async def test_single_account_campaign_stops_when_account_becomes_frozen(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        progress={},
        stop_conditions={},
        report={},
    )
    session.add(run)
    await session.commit()

    account.status = "frozen"
    await session.commit()

    ticked = await campaigns_engine.tick_generic_campaign_run(session, workspace.id, run)

    assert ticked.status == "failed"
    assert ticked.error_code == "ACCOUNT_SAFETY_BLOCKED"
    assert "заморожен" in (ticked.error_message or "")
    await session.refresh(campaign)
    assert campaign.status == "failed"


@pytest.mark.asyncio
async def test_multi_account_campaign_removes_frozen_account_and_keeps_running(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    second_account = TelegramAccount(
        workspace_id=workspace.id,
        username="healthy_runner",
        status="active",
        import_type="session",
        proxy_id=account.proxy_id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": dict(account.daily_limits["restriction_status"])},
    )
    session.add(second_account)
    await session.flush()
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {**campaign.config, "account_ids": [account.id, second_account.id]}
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        progress={},
        stop_conditions={},
        report={},
    )
    session.add(run)
    await session.commit()

    await session.refresh(account)
    account.status = "frozen"
    session.add(account)
    await session.flush()

    block_reason = await campaigns_engine._campaign_runtime_account_block_reason(session, campaign, run=run)
    await session.flush()

    assert block_reason is None
    assert run.status == "running"
    await session.refresh(campaign)
    assert campaign.status == "running"
    assert campaign.config["account_ids"] == [second_account.id]
    assert campaign.config["removed_accounts"][0]["account_id"] == account.id
    events = list((await session.execute(select(CampaignEvent).where(CampaignEvent.run_id == run.id))).scalars().all())
    assert any(event.event_type == "campaign_account_removed" for event in events)


@pytest.mark.asyncio
async def test_campaign_safety_blocks_overloaded_proxy(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    second_account = TelegramAccount(
        workspace_id=workspace.id,
        username="same_proxy",
        status="active",
        import_type="session",
        proxy_id=account.proxy_id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
    )
    session.add(second_account)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    session.add(campaign)
    await session.commit()

    safety = await campaigns_api.campaign_safety(campaign.id, session, workspace.id)

    assert safety.can_start is False
    assert safety.accounts[0].reason == "proxy_overloaded"


@pytest.mark.asyncio
async def test_campaign_safety_blocks_invalid_source(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    source.comments_enabled = False
    source.access_status = "invalid_comments_closed"
    source.source_metadata = {"invalid": True, "invalid_reason": "source_comments_not_available"}
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    session.add(campaign)
    await session.commit()

    safety = await campaigns_api.campaign_safety(campaign.id, session, workspace.id)

    assert safety.can_start is False
    assert safety.sources[0].status == "blocked"
    assert safety.sources[0].reason == "source_comments_not_available"


@pytest.mark.asyncio
async def test_campaign_dry_run_returns_counts_and_reasons(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    account.daily_limits = {"restriction_status": {"status": "limited"}}
    account.status = "quarantined"
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    session.add(campaign)
    await session.commit()

    dry_run = await campaigns_api.campaign_dry_run(campaign.id, session, workspace.id)

    assert dry_run.can_start is False
    assert dry_run.accounts["blocked"] == 1
    assert dry_run.sources["safe"] == 1
    assert dry_run.blocking_reasons
    assert dry_run.safety.accounts[0].reason == "telegram_restriction_limited"


@pytest.mark.asyncio
async def test_comment_campaign_skips_posts_with_stop_words(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fail_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        raise AssertionError("generation should not run for stop-word posts")

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fail_generate)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    await create_mock_posts(session, workspace.id, source, limit=2, seed_text="Рекламный пост со скидкой и промокодом.")
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="comment_recent_posts",
        latest_posts_per_source=2,
    )
    campaign.policy = {"stop_words_enabled": True, "stop_words": ["рекламный пост", "скидка", "промокод"]}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    assert run.progress["messages_seen"] == 2
    assert run.progress["posts_skipped_by_stop_words"] == 2
    assert run.progress["comments_generated"] == 0
    assert comments == []
    assert actions == []


@pytest.mark.asyncio
async def test_comment_campaign_skips_short_posts(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fail_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        raise AssertionError("generation should not run for short posts")

    monkeypatch.setattr("app.modules.campaigns.engine.generate_comment_text", fail_generate)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    await create_mock_posts(session, workspace.id, source, limit=2, seed_text="Доброе утро.")
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="comment_recent_posts",
        latest_posts_per_source=2,
    )
    campaign.policy = {"min_post_chars": 200}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    assert run.progress["messages_seen"] == 2
    assert run.progress["posts_skipped_by_length"] == 2
    assert run.progress["comments_generated"] == 0
    assert comments == []
    assert actions == []


@pytest.mark.asyncio
async def test_comment_campaign_allows_short_media_captions(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fake_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Живой комментарий к посту с визуальным контекстом.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fake_generate)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    messages = await create_mock_posts(
        session,
        workspace.id,
        source,
        limit=2,
        seed_text="Дом 130м2 рядом с городом.",
    )
    for message in messages:
        message.raw = {"mode": "test", "has_media": True, "media_type": "photo"}
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="comment_recent_posts",
        latest_posts_per_source=2,
    )
    campaign.policy = {"min_post_chars": 200, "min_media_post_chars": 20}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())

    assert run.status == "completed"
    assert run.progress["messages_seen"] == 2
    assert run.progress["posts_skipped_by_length"] == 0
    assert run.progress["comments_generated"] == 2
    assert len(comments) == 2


@pytest.mark.asyncio
async def test_start_run_writes_events_report_and_handles_no_messages(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fail_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        raise AssertionError("generation should not run without messages")

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fail_generate)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    events = list((await session.execute(select(CampaignEvent))).scalars().all())

    assert run.status == "running"
    assert run.progress["ticks"] == 1
    assert run.progress["sources_checked"] == 1
    assert run.progress["new_posts_found"] == 0
    assert run.progress["cursors_initialized"] == 1
    assert run.report["status"] == "running"
    assert run.report["progress"]["ticks"] == 1
    assert run.report["progress"]["messages_seen"] == 0
    assert source.id in campaign.config.get("source_cursors", {})
    assert [event.event_type for event in events] == [
        "run_created",
        "tick_started",
        "source_monitoring_started",
        "source_posts_loaded",
        "source_posts_filtered",
        "runner_waiting",
    ]


@pytest.mark.asyncio
async def test_auto_join_policy_defaults_are_conservative() -> None:
    assert _policy_auto_join_interval_seconds({}) >= 1800
    assert _policy_auto_join_daily_limit({}) <= 6
    assert _policy_auto_join_max_per_tick({"auto_join_max_per_tick": 10}) == 1


def test_auto_join_daily_limit_is_not_capped_at_twelve() -> None:
    assert _policy_auto_join_daily_limit({"auto_join_daily_limit": 60}) == 60


@pytest.mark.asyncio
async def test_auto_join_daily_budget_blocks_new_join_attempts() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    source = TelegramSource(
        workspace_id="workspace",
        type="channel",
        username="target_channel",
        title="Target channel",
        comments_enabled=True,
        access_status="available",
        source_metadata={},
    )

    assert (
        _should_ensure_source_membership(
            source,
            None,
            True,
            0,
            1,
            None,
            now,
            daily_attempts=6,
            daily_limit=6,
        )
        is False
    )


@pytest.mark.asyncio
async def test_comment_runner_auto_joins_discussion_by_default(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=False)
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob="test-session"))
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {**campaign.config, "post_selection_mode": "historical_posts"}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    assert run.status == "completed"
    assert calls == [{"ensure_membership": True, "join_discussion": True}]


@pytest.mark.asyncio
async def test_dry_idle_pass_only_fetches_source_selected_for_join(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        source = args[2]
        assert isinstance(source, TelegramSource)
        calls.append((source.id, kwargs))
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    second_source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="second_target_channel",
        title="Second target channel",
        comments_enabled=True,
        access_status="available",
    )
    session.add_all([second_source, TelegramSession(account_id=account.id, encrypted_session_blob="test-session")])
    await session.flush()
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_ids": [source.id, second_source.id],
        "post_selection_mode": "historical_posts",
    }
    campaign.policy = {"dry_idle_pass": True, "auto_join_max_per_tick": 1}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    assert run.status == "completed"
    assert calls == [(source.id, {"ensure_membership": True, "join_discussion": True})]
    actions = list((await session.execute(select(CampaignAction))).scalars().all())
    assert actions == []


@pytest.mark.asyncio
async def test_dry_idle_pass_allows_one_join_per_assigned_account_in_same_tick(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        source = args[2]
        account = args[3]
        assert isinstance(source, TelegramSource)
        assert isinstance(account, TelegramAccount)
        calls.append((source.id, account.id))
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    first_source, first_account, prompt = await _comment_campaign_parts(session, workspace)
    second_source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="second_parallel_target",
        title="Second parallel target",
        comments_enabled=True,
        access_status="available",
    )
    second_proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.2",
        port=1080,
        status="active",
    )
    session.add_all([second_source, second_proxy])
    await session.flush()
    second_account = TelegramAccount(
        workspace_id=workspace.id,
        username="second_commenter",
        status="active",
        import_type="session",
        proxy_id=second_proxy.id,
        health_score=90,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={"restriction_status": dict(first_account.daily_limits["restriction_status"])},
    )
    session.add(second_account)
    await session.flush()
    session.add_all(
        [
            TelegramSession(account_id=first_account.id, encrypted_session_blob="first-session"),
            TelegramSession(account_id=second_account.id, encrypted_session_blob="second-session"),
        ]
    )
    await session.flush()
    campaign = _campaign(workspace, first_source, first_account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_ids": [first_source.id, second_source.id],
        "account_ids": [first_account.id, second_account.id],
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {
            first_account.id: [first_source.id],
            second_account.id: [second_source.id],
        },
        "post_selection_mode": "historical_posts",
    }
    campaign.policy = {
        "dry_idle_pass": True,
        "auto_join_max_per_tick": 1,
        "max_sources_per_tick": 2,
    }
    session.add(campaign)
    await session.commit()

    await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)

    assert calls == [
        (first_source.id, first_account.id),
        (second_source.id, second_account.id),
    ]


@pytest.mark.asyncio
async def test_flood_wait_stops_account_source_fetches_for_the_tick(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[str] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        source = args[2]
        assert isinstance(source, TelegramSource)
        calls.append(source.id)
        raise TelegramGatewayError("FLOOD_WAIT", "A wait of 300 seconds is required", {})

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    second_source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="second_flood_target",
        title="Second flood target",
        comments_enabled=True,
        access_status="available",
    )
    session.add_all([second_source, TelegramSession(account_id=account.id, encrypted_session_blob="test-session")])
    await session.flush()
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_ids": [source.id, second_source.id],
        "post_selection_mode": "historical_posts",
    }
    campaign.policy = {"dry_idle_pass": True, "auto_join_max_per_tick": 1}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await session.refresh(account)
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    assert calls == [source.id]
    assert account.daily_limits["source_fetch_wait_until"]
    assert len(actions) == 1
    assert actions[0].error_code == "FLOOD_WAIT"


def test_comment_daily_limit_for_warming_accounts_respects_campaign_policy() -> None:
    account = TelegramAccount(
        workspace_id="workspace",
        username="new_commenter",
        status="warming",
        import_type="session",
        warmup_score=40,
    )

    assert campaigns_engine._comment_daily_limit_for_account(account, 25) == 25


def test_incubator_accounts_are_not_actionable_for_campaign_executor() -> None:
    account = TelegramAccount(
        workspace_id="workspace",
        username="incubator_commenter",
        status="active",
        import_type="session",
        warmup_score=100,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear"},
            "account_metadata": {"incubator_mode": True},
        },
    )

    assert campaigns_engine._account_is_actionable(account) is False
    assert campaigns_engine._account_can_send_comments(account) is False


@pytest.mark.asyncio
async def test_comment_runner_joins_discussion_when_source_comments_state_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        source = args[2]
        assert isinstance(source, TelegramSource)
        source.comments_enabled = True
        calls.append(kwargs)
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=False)
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob="test-session"))
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {**campaign.config, "post_selection_mode": "historical_posts"}
    campaign.policy = {"auto_join_sources": True, "auto_join_discussions": True}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await session.refresh(source)

    assert run.status == "completed"
    assert source.comments_enabled is True
    assert calls
    assert calls[0]["ensure_membership"] is True
    assert calls[0]["join_discussion"] is True


@pytest.mark.asyncio
async def test_comment_runner_skips_source_closed_during_live_refresh(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        source = args[2]
        assert isinstance(source, TelegramSource)
        source.comments_enabled = False
        return []

    async def fail_generate_comment(*args: object, **kwargs: object) -> tuple[str, dict]:
        raise AssertionError("comment generation must not run for a source with closed comments")

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    monkeypatch.setattr("app.modules.campaigns.engine.generate_comment_text", fail_generate_comment)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=True)
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob="test-session"))
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {**campaign.config, "post_selection_mode": "historical_posts"}
    campaign.policy = {"auto_join_sources": False, "auto_join_discussions": False}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await session.refresh(source)
    await session.refresh(campaign)
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    assert source.access_status == "blacklisted"
    assert source.comments_enabled is False
    assert campaign.config["source_ids"] == []
    assert campaign.config["blocked_sources"][source.id]["reason"] == "source_comments_not_available"
    assert actions == []


@pytest.mark.asyncio
async def test_comment_runner_does_not_treat_discussion_join_as_source_join(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=False)
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob="test-session"))
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {**campaign.config, "post_selection_mode": "historical_posts"}
    campaign.policy = {"auto_join_discussions": True}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    assert calls == []
    assert len(actions) == 1
    assert actions[0].error_code == "COMMENTS_CLOSED"


@pytest.mark.asyncio
async def test_comment_runner_blacklists_source_when_discussion_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fake_fetch_real_posts(*args: object, **kwargs: object) -> list[TelegramMessage]:
        return []

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.engine.fetch_real_posts", fake_fetch_real_posts)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace, comments_enabled=False)
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob="test-session"))
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {**campaign.config, "post_selection_mode": "historical_posts"}
    campaign.policy = {"auto_join_sources": True, "auto_join_discussions": True}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    await session.refresh(source)
    await session.refresh(campaign)
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert run.status == "completed"
    blacklist_entries = list((await session.execute(select(TelegramSourceBlacklistEntry))).scalars().all())

    assert source.access_status == "blacklisted"
    assert source.comments_enabled is False
    assert campaign.config["source_ids"] == []
    assert campaign.config["blocked_sources"][source.id]["reason"] == "source_comments_not_available"
    assert len(blacklist_entries) == 1
    assert blacklist_entries[0].source_id == source.id
    assert blacklist_entries[0].reason == "source_comments_not_available"
    assert actions == []


@pytest.mark.asyncio
async def test_frozen_accounts_are_not_actionable_for_campaign_runtime(session: AsyncSession) -> None:
    account = TelegramAccount(
        workspace_id="workspace",
        username="frozen_user",
        status="frozen",
        import_type="session",
        health_score=10,
    )

    assert _account_is_actionable(account) is False


@pytest.mark.asyncio
async def test_campaign_gateway_frozen_error_marks_account_frozen(session: AsyncSession) -> None:
    account = TelegramAccount(
        workspace_id="workspace",
        username="nastya",
        status="active",
        import_type="session",
        health_score=90,
        daily_limits={},
    )
    session.add(account)
    await session.flush()

    campaigns_engine._apply_gateway_error_account_risk(
        session,
        account,
        TelegramGatewayError(
            "FrozenMethodInvalidError",
            "You tried to use a method that is not available for frozen accounts",
            {"rpc_error": "FrozenMethodInvalidError"},
        ),
        datetime.now(UTC),
    )
    await session.flush()

    events = list((await session.execute(select(AccountHealthEvent))).scalars().all())

    assert account.status == "frozen"
    assert account.health_score == 10
    assert account.restriction_status == "frozen"
    assert account.daily_limits["last_frozen_error_code"] == "FrozenMethodInvalidError"
    assert len(events) == 1
    assert events[0].event_type == "account_frozen_detected"
    assert events[0].severity == "error"


@pytest.mark.asyncio
async def test_comment_run_without_account_sessions_fails_without_telegram_alert(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fail_alert(*args: object, **kwargs: object) -> bool:
        raise AssertionError("NO_ACTIONABLE_ACCOUNTS must not send Telegram alert")

    monkeypatch.setattr("app.modules.campaigns.engine.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.operations.service.send_telegram_alert", fail_alert)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    await create_mock_posts(session, workspace.id, source, limit=1)
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="comment_recent_posts",
        latest_posts_per_source=1,
    )
    campaign.policy = {"min_post_chars": 0}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    events = list((await session.execute(select(OperationalEvent))).scalars().all())

    assert run.status == "failed"
    assert run.error_code == "NO_ACTIONABLE_ACCOUNTS"
    assert "живой сессией" in (run.error_message or "")
    assert events[-1].event_type == "campaign_run_failed"
    assert events[-1].notified_at is None


@pytest.mark.asyncio
async def test_stop_endpoint_marks_requested_stop_and_cancelled(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        progress={},
        stop_conditions={},
        report={},
    )
    session.add(run)
    await session.commit()

    stopped = await campaigns_api.stop_campaign_run(run.id, session, workspace.id)

    assert stopped.status == "cancelled"
    assert stopped.requested_stop_at is not None
    assert stopped.report["stop_reason"] == "manual_stop"


@pytest.mark.asyncio
async def test_comment_recent_posts_run_preserves_generation_flow(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    async def fake_generate_comment_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Живой тестовый комментарий по смыслу поста.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fake_generate_comment_text)
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    await create_mock_posts(session, workspace.id, source, limit=2)
    campaign = _campaign(
        workspace,
        source,
        account,
        prompt,
        mode="comment_recent_posts",
        latest_posts_per_source=2,
    )
    campaign.policy = {"min_post_chars": 0}
    session.add(campaign)
    await session.commit()

    run = await campaigns_api.start_campaign_run(campaign.id, session, workspace.id)
    comments = list((await session.execute(select(TelegramComment))).scalars().all())

    assert run.status == "completed"
    assert run.progress["comments_generated"] == 2
    assert len(comments) == 2
    assert all(comment.status == "draft" for comment in comments)


@pytest.mark.asyncio
async def test_source_folder_campaign_split_assigns_sources_per_account(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    folder = await sources_api.create_source_folder(
        SourceFolderCreate(name="Crypto comments"),
        session,
        workspace.id,
    )
    sources = [
        TelegramSource(
            workspace_id=workspace.id,
            type="channel",
            username=f"target_{index}",
            title=f"Target {index}",
            comments_enabled=True,
            access_status="available",
        )
        for index in range(5)
    ]
    proxies = [
        Proxy(workspace_id=workspace.id, type="socks5", host=f"127.0.0.{index + 1}", port=1080, status="active")
        for index in range(2)
    ]
    session.add_all(proxies)
    await session.flush()
    accounts = [
        TelegramAccount(
            workspace_id=workspace.id,
            username=f"sender_{index}",
            status="active",
            import_type="session",
            proxy_id=proxies[index].id,
            warmup_score=100,
            warmup_status="completed",
            daily_limits={
                "restriction_status": {
                    "status": "clear",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "evidence_source": "operator_admission",
                }
            },
        )
        for index in range(2)
    ]
    prompt = CommentPromptTemplate(workspace_id=workspace.id, name="Prompt", prompt_text="Коротко по смыслу")
    session.add_all([*sources, *accounts, prompt])
    await session.flush()
    await sources_api.assign_sources_to_folder(
        folder.id,
        SourceFolderAssignRequest(source_ids=[source.id for source in sources]),
        session,
        workspace.id,
    )

    campaign = await campaigns_api.create_comment_posts_campaign(
        CommentPostsCampaignCreate(
            name="Split comments",
            source_ids=[],
            source_folder_ids=[folder.id],
            account_ids=[account.id for account in accounts],
            source_assignment_mode="multi_account_split",
            comment_prompt_template_id=prompt.id,
        ),
        session,
        workspace.id,
        user,
    )

    assert campaign.config["source_ids"] == [source.id for source in sources]
    assert campaign.config["source_assignment_mode"] == "multi_account_split"
    assignment = campaign.config["source_assignment"]
    assert set(assignment) == {account.id for account in accounts}
    assert sorted(len(items) for items in assignment.values()) == [2, 3]
    assigned_source_ids = sorted(source_id for items in assignment.values() for source_id in items)
    assert assigned_source_ids == sorted(source.id for source in sources)


@pytest.mark.asyncio
async def test_campaign_run_logs_normalize_runtime_payload(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    campaign = Campaign(
        workspace_id=workspace.id,
        type="comment_posts",
        name="Logs",
        status="running",
        config={},
        policy={},
    )
    session.add(campaign)
    await session.flush()
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        progress={},
        stop_conditions={},
        report={},
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            CampaignEvent(
                run_id=run.id,
                campaign_id=campaign.id,
                level="debug",
                event_type="source_monitoring_started",
                message="Monitoring started",
                payload={
                    "account_id": "acc-1",
                    "account_name": "Sender",
                    "source_id": "src-1",
                    "source_title": "Target",
                    "source_ref": "@target",
                },
            ),
            CampaignEvent(
                run_id=run.id,
                campaign_id=campaign.id,
                level="error",
                event_type="source_fetch_failed",
                message="Fetch failed",
                payload={"error_code": "TEST"},
            ),
        ]
    )
    await session.commit()

    logs = await campaigns_api.campaign_run_logs(
        run.id,
        session,
        workspace.id,
        level="debug",
        event_type=None,
        after_id=None,
        q=None,
        limit=100,
    )

    assert len(logs) == 1
    assert logs[0].event_type == "source_monitoring_started"
    assert logs[0].account_name == "Sender"
    assert logs[0].source_ref == "@target"


@pytest.mark.asyncio
async def test_campaign_comment_report_totals_channels_post_url_and_csv(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source_public = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="public_channel",
        title="Public channel",
        url="https://t.me/public_channel",
        comments_enabled=True,
        access_status="available",
    )
    source_url = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        title="URL channel",
        url="https://t.me/url_channel",
        comments_enabled=True,
        access_status="available",
    )
    account_one = TelegramAccount(
        workspace_id=workspace.id,
        username="sender_one",
        display_name="Sender One",
        status="active",
        import_type="session",
    )
    account_two = TelegramAccount(
        workspace_id=workspace.id,
        username="sender_two",
        status="active",
        import_type="session",
    )
    session.add_all([source_public, source_url, account_one, account_two])
    await session.flush()

    campaign = Campaign(
        workspace_id=workspace.id,
        type="comment_posts",
        name="Client campaign",
        status="running",
        config={"source_ids": [source_public.id, source_url.id], "account_ids": [account_one.id, account_two.id]},
        policy={},
    )
    message_public = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source_public.id,
        tg_message_id="101",
        text="Public post text",
        published_at=datetime(2026, 5, 17, 8, 0, tzinfo=UTC),
    )
    message_url = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source_url.id,
        tg_message_id="202",
        text="URL post text",
        published_at=datetime(2026, 5, 17, 9, 0, tzinfo=UTC),
    )
    session.add_all([campaign, message_public, message_url])
    await session.flush()

    sent_at = datetime(2026, 5, 17, 10, 5, tzinfo=UTC)
    draft_created_at = datetime(2026, 5, 17, 10, 10, tzinfo=UTC)
    failed_created_at = datetime(2026, 5, 17, 10, 15, tzinfo=UTC)
    session.add_all(
        [
            TelegramComment(
                workspace_id=workspace.id,
                source_id=source_public.id,
                post_message_id=message_public.id,
                campaign_id=campaign.id,
                telegram_account_id=account_one.id,
                text="Sent client-facing comment",
                status="sent",
                sent_at=sent_at,
                created_at=sent_at,
                tg_comment_message_id="9001",
            ),
            TelegramComment(
                workspace_id=workspace.id,
                source_id=source_public.id,
                post_message_id=message_public.id,
                campaign_id=campaign.id,
                telegram_account_id=account_two.id,
                text="Draft client-facing comment",
                status="draft",
                created_at=draft_created_at,
            ),
            TelegramComment(
                workspace_id=workspace.id,
                source_id=source_url.id,
                post_message_id=message_url.id,
                campaign_id=campaign.id,
                telegram_account_id=account_two.id,
                text="Failed client-facing comment",
                status="failed",
                created_at=failed_created_at,
            ),
        ]
    )
    await session.commit()

    report = await build_campaign_comment_report(session, workspace.id, campaign)

    assert report.campaign_id == campaign.id
    assert report.total_count == 3
    assert report.sent_count == 1
    assert report.draft_count == 1
    assert report.failed_count == 1
    summaries = {item.source_id: item for item in report.by_channel}
    assert summaries[source_public.id].total_count == 2
    assert summaries[source_public.id].sent_count == 1
    assert summaries[source_public.id].draft_count == 1
    assert summaries[source_public.id].failed_count == 0
    assert summaries[source_url.id].total_count == 1
    assert summaries[source_url.id].failed_count == 1

    public_row = next(row for row in report.rows if row.comment_status == "sent")
    assert public_row.post_url == "https://t.me/public_channel/101"
    assert public_row.account_label == "Sender One"

    csv_text = campaign_comment_report_csv(report)
    assert csv_text.startswith("\ufeff")
    assert "Канал,Дата,Время,Статус,Ссылка на пост,ID поста,Текст поста,Комментарий,Аккаунт" in csv_text
    assert "Sent client-facing comment" in csv_text
    assert "https://t.me/public_channel/101" in csv_text


@pytest.mark.asyncio
async def test_comment_write_ban_excludes_source_without_quarantining_account(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="123",
        text="Новый пост с открытым обсуждением.",
        published_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id]},
    }
    session.add_all([message, campaign])
    await session.flush()

    async def fake_generate(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Вопрос по теме поста?", {"total_tokens": 10}

    async def fake_send(**kwargs: object) -> TelegramSendResult:
        raise TelegramGatewayError(
            "UserBannedInChannelError",
            "You're banned from sending messages in supergroups/channels",
            {},
        )

    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(campaigns_engine, "generate_comment_text", fake_generate)
    monkeypatch.setattr(campaigns_engine, "send_comment_with_account", fake_send)

    result = await campaigns_engine._create_comment_action(
        session=session,
        workspace_id=workspace.id,
        campaign=campaign,
        prompt=prompt,
        source=source,
        message=message,
        account=account,
        delivery_mode="generate_and_send",
        now=datetime(2026, 9, 28, tzinfo=UTC),
    )
    await session.flush()

    assert result["comment_status"] == "failed"
    assert account.status == "active"
    assert account.restriction_status == "clear"
    assert source.access_status == "available"
    assert source.comments_enabled is True
    assert campaign.config["source_assignment"][account.id] == []
    assert campaign.config["source_ids"] == []
    assert campaign.config["blocked_sources"][source.id]["account_id"] == account.id
    assert list((await session.execute(select(TelegramSourceBlacklistEntry))).scalars()) == []
    assert len(list((await session.execute(select(AccountHealthEvent))).scalars())) == 1
    assert campaigns_engine._account_can_send_comments(account) is True


@pytest.mark.asyncio
async def test_comment_write_ban_keeps_source_assigned_to_another_account(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    other = TelegramAccount(workspace_id=workspace.id, status="active", import_type="session")
    session.add(other)
    await session.flush()
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "account_ids": [account.id, other.id],
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id], other.id: [source.id]},
    }

    campaigns_engine._exclude_comment_account_source_pair(campaign, source, account, datetime(2026, 9, 28, tzinfo=UTC))

    assert campaign.config["source_ids"] == [source.id]
    assert campaign.config["source_assignment"][account.id] == []
    assert campaign.config["source_assignment"][other.id] == [source.id]
    assert f"{account.id}:{source.id}" in campaign.config["blocked_account_source_pairs"]
    assert source.id not in campaign.config.get("blocked_sources", {})


@pytest.mark.asyncio
async def test_comment_send_does_not_rebind_or_resend_after_invalid_post(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    source.username = "fil100"
    source.title = "Филатов Дмитрий"
    source.comments_enabled = True
    post_text = "Один и тот же текст поста, который Telegram теперь показывает по другой ссылке."
    message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="798",
        text=post_text,
        published_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
        raw={"mode": "test"},
    )
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.status = "running"
    session.add_all([message, campaign])
    await session.flush()

    async def fake_generate_comment_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Живой комментарий по теме поста.", {"total_tokens": 10}

    async def fake_fetch_posts(*args: object, **kwargs: object) -> list[TelegramPostInfo]:
        return [
            TelegramPostInfo(
                tg_message_id="800",
                text=post_text,
                published_at=datetime(2026, 6, 13, 12, 1, tzinfo=UTC),
                views_count=100,
                forwards_count=2,
                replies_count=3,
                raw={"mode": "test_fetch"},
            )
        ]

    send_attempts: list[str] = []

    async def fake_send_comment(
        *,
        session: AsyncSession,
        account: TelegramAccount,
        source_ref: str,
        post_tg_message_id: str,
        text: str,
    ) -> TelegramSendResult:
        send_attempts.append(post_tg_message_id)
        if post_tg_message_id == "798":
            raise TelegramGatewayError(
                "MsgIdInvalidError",
                "Message id invalid",
                {"comment_target_resolution": "direct"},
            )
        return TelegramSendResult(
            tg_message_id="900",
            raw={"mode": "test_send", "requested_comment_to": post_tg_message_id},
        )

    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(campaigns_engine, "generate_comment_text", fake_generate_comment_text)
    monkeypatch.setattr(campaigns_engine, "fetch_posts_with_account", fake_fetch_posts)
    monkeypatch.setattr(campaigns_engine, "send_comment_with_account", fake_send_comment)

    result = await campaigns_engine._create_comment_action(
        session=session,
        workspace_id=workspace.id,
        campaign=campaign,
        prompt=prompt,
        source=source,
        message=message,
        account=account,
        delivery_mode="generate_and_send",
        now=datetime(2026, 6, 13, 12, 5, tzinfo=UTC),
    )
    comments = list((await session.execute(select(TelegramComment))).scalars().all())
    actions = list((await session.execute(select(CampaignAction))).scalars().all())

    assert result == {"action_status": "failed", "comment_status": "failed"}
    assert send_attempts == ["798"]
    assert message.tg_message_id == "798"
    assert "post_id_rebound" not in message.raw
    assert len(comments) == 1
    assert comments[0].status == "failed"
    assert comments[0].post_message_id == message.id
    assert "post_id_rebound" not in comments[0].raw
    assert len(actions) == 1
    assert actions[0].target_id == message.id


@pytest.mark.asyncio
async def test_source_permission_denial_excludes_only_campaign_account(session: AsyncSession) -> None:
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id]},
    }
    campaigns_engine._exclude_comment_account_source_pair(
        campaign, source, account, datetime.now(UTC), "ChannelPrivateError"
    )
    assert campaign.config["source_ids"] == []
    assert campaign.config["blocked_sources"][source.id]["error_code"] == "ChannelPrivateError"
    assert account.status == "active"
    assert source.access_status != "archived"


@pytest.mark.asyncio
async def test_source_exclusion_skips_remaining_posts_in_same_tick(session, monkeypatch):
    from unittest.mock import AsyncMock

    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    campaign.config = {
        **campaign.config,
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id]},
    }
    campaign.policy = {"daily_limit_per_account": 25, "auto_join_sources": False, "auto_join_discussions": False}
    session.add(campaign)
    for n in range(3):
        session.add(
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id=str(n),
                text="Текст тематического поста " * 20 + str(n),
                published_at=datetime.now(UTC),
            )
        )
    await session.commit()
    calls = []

    async def denied(**kwargs):
        calls.append(kwargs["message"].id)
        campaigns_engine._exclude_comment_account_source_pair(
            campaign, source, account, datetime.now(UTC), "ChannelPrivateError"
        )
        return {"action_status": "failed", "comment_status": "failed"}

    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    monkeypatch.setattr(campaigns_engine, "_create_comment_action", denied)
    monkeypatch.setattr(campaigns_engine, "_comment_account_send_wait_reason", AsyncMock(return_value=None))
    await campaigns_engine._process_comment_sources(
        session=session,
        workspace_id=workspace.id,
        campaign=campaign,
        context=campaigns_engine.CommentRuntimeContext(prompt, [source], [account]),
        selection_mode="historical_posts",
        delivery_mode="generate_and_send",
        latest_limit=5,
        max_comments=None,
        dedupe=True,
        update_cursors=True,
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_deferred_comment_survives_cooldown_and_is_sent_next_tick(session, monkeypatch):
    from unittest.mock import AsyncMock

    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="comment_new_posts")
    old_cursor = campaigns_engine._cursor_from_time(datetime.now(UTC) - timedelta(hours=1))
    campaign.config = {
        **campaign.config,
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id]},
        "source_cursors": {source.id: old_cursor},
    }
    campaign.policy = {
        "daily_limit_per_account": 25,
        "auto_join_sources": False,
        "auto_join_discussions": False,
        "retain_deferred_comment_posts": True,
    }
    message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="100",
        text="Содержательный тематический пост " * 20,
        published_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add_all([campaign, message])
    await session.commit()
    wait = AsyncMock(return_value="min_send_interval")
    send = AsyncMock(return_value={"action_status": "succeeded", "comment_status": "sent"})
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    monkeypatch.setattr(campaigns_engine, "_comment_account_send_wait_reason", wait)
    monkeypatch.setattr(campaigns_engine, "_create_comment_action", send)
    kwargs = dict(
        session=session,
        workspace_id=workspace.id,
        campaign=campaign,
        context=campaigns_engine.CommentRuntimeContext(prompt, [source], [account]),
        selection_mode="new",
        delivery_mode="generate_and_send",
        latest_limit=10,
        max_comments=None,
        dedupe=True,
        update_cursors=True,
    )
    deferred = await campaigns_engine._process_comment_sources(**kwargs)
    assert deferred.comments_sent == 0
    assert campaign.config["source_cursors"][source.id] == old_cursor
    send.assert_not_awaited()
    wait.return_value = None
    delivered = await campaigns_engine._process_comment_sources(**kwargs)
    assert delivered.comments_sent == 1
    assert send.await_args.kwargs["message"].id == message.id
    assert campaign.config["source_cursors"][source.id] == campaigns_engine._cursor_from_message(message)


@pytest.mark.asyncio
async def test_pinned_account_daily_limit_keeps_other_accounts_running(session, monkeypatch):
    from unittest.mock import AsyncMock

    _, workspace = await _workspace(session)
    first, capped, prompt = await _comment_campaign_parts(session, workspace)
    second, available, _ = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, first, capped, prompt, mode="comment_new_posts")
    old_cursor = campaigns_engine._cursor_from_time(datetime.now(UTC) - timedelta(hours=1))
    campaign.config = {
        **campaign.config,
        "account_ids": [capped.id, available.id],
        "source_ids": [first.id, second.id],
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {capped.id: [first.id], available.id: [second.id]},
        "source_cursors": {first.id: old_cursor, second.id: old_cursor},
    }
    campaign.policy = {
        "daily_limit_per_account": 25,
        "auto_join_sources": False,
        "auto_join_discussions": False,
        "retain_deferred_comment_posts": True,
    }
    session.add(campaign)
    for source in [first, second]:
        session.add(
            TelegramMessage(
                workspace_id=workspace.id,
                source_id=source.id,
                tg_message_id="100",
                text="Содержательный тематический пост " * 20,
                published_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    await session.commit()
    send = AsyncMock(return_value={"action_status": "succeeded", "comment_status": "sent"})
    monkeypatch.setattr(campaigns_engine, "real_telegram_enabled", lambda: False)
    monkeypatch.setattr(
        campaigns_engine, "_sent_comments_today_by_account", AsyncMock(return_value={capped.id: 25, available.id: 0})
    )
    monkeypatch.setattr(campaigns_engine, "_comment_account_send_wait_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(campaigns_engine, "_create_comment_action", send)
    result = await campaigns_engine._process_comment_sources(
        session=session,
        workspace_id=workspace.id,
        campaign=campaign,
        context=campaigns_engine.CommentRuntimeContext(prompt, [first, second], [capped, available]),
        selection_mode="new",
        delivery_mode="generate_and_send",
        latest_limit=10,
        max_comments=None,
        dedupe=True,
        update_cursors=True,
    )
    assert result.comments_sent == 1
    assert result.stop_reason is None
    assert send.await_args.kwargs["account"].id == available.id
    assert campaign.config["source_cursors"][first.id] == old_cursor
