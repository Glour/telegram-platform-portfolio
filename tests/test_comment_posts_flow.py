from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.core.security import hash_password
from app.db.base import Base
from app.modules.ai.models import AiProfile, CommentPromptTemplate
from app.modules.auth.models import User
from app.modules.campaigns.models import Campaign, CampaignAction
from app.modules.campaigns.service import start_comment_posts_campaign
from app.modules.comments.models import TelegramComment
from app.modules.parsing.service import create_mock_posts
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.workspaces.models import Workspace


@pytest.mark.asyncio
async def test_comment_posts_campaign_creates_sent_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_generate_comment_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Живой тестовый комментарий по смыслу поста.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fake_generate_comment_text)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(email="owner@example.com", password_hash=hash_password("password123"))
        session.add(user)
        await session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Test", slug="test")
        session.add(workspace)
        await session.flush()

        source = TelegramSource(
            workspace_id=workspace.id,
            type="channel",
            username="target_channel",
            title="Target channel",
            comments_enabled=True,
            access_status="available",
        )
        account = TelegramAccount(
            workspace_id=workspace.id,
            username="commenter",
            status="active",
            import_type="session",
            health_score=90,
            daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
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

        await create_mock_posts(session, workspace.id, source, limit=2)
        campaign = Campaign(
            workspace_id=workspace.id,
            type="comment_posts",
            name="Neurocomments",
            status="draft",
            config={
                "source_ids": [source.id],
                "account_ids": [account.id],
                "comment_prompt_template_id": prompt.id,
                "latest_posts_per_source": 2,
                "mode": "generate_and_send",
            },
            policy={"min_post_chars": 0},
        )
        session.add(campaign)
        await session.commit()

        result = await start_comment_posts_campaign(session, workspace.id, campaign)

        assert result.status == "completed"
        comments = list((await session.execute(select(TelegramComment))).scalars().all())
        actions = list((await session.execute(select(CampaignAction))).scalars().all())
        assert len(comments) == 2
        assert len(actions) == 2
        assert all(comment.status == "sent" for comment in comments)

    await engine.dispose()


@pytest.mark.asyncio
async def test_historical_comment_posts_process_immediately_even_with_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_generate_comment_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        return "Исторический комментарий по смыслу поста.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fake_generate_comment_text)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(email="owner@example.com", password_hash=hash_password("password123"))
        session.add(user)
        await session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Test", slug="test")
        session.add(workspace)
        await session.flush()

        source = TelegramSource(
            workspace_id=workspace.id,
            type="channel",
            username="target_channel",
            title="Target channel",
            comments_enabled=True,
            access_status="available",
        )
        account = TelegramAccount(
            workspace_id=workspace.id,
            username="commenter",
            status="active",
            import_type="session",
            health_score=90,
            daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
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

        await create_mock_posts(session, workspace.id, source, limit=2)
        campaign = Campaign(
            workspace_id=workspace.id,
            type="comment_posts",
            name="Historical neurocomments",
            status="draft",
            config={
                "source_ids": [source.id],
                "account_ids": [account.id],
                "comment_prompt_template_id": prompt.id,
                "latest_posts_per_source": 1,
                "post_selection_mode": "historical_posts",
                "delivery_mode": "draft_only",
            },
            policy={"delay_seconds_min": 3600, "delay_seconds_max": 7200, "min_post_chars": 0},
        )
        session.add(campaign)
        await session.commit()

        result = await start_comment_posts_campaign(session, workspace.id, campaign)

        assert result.status == "completed"
        comments = list((await session.execute(select(TelegramComment))).scalars().all())
        assert len(comments) == 1
        assert comments[0].status == "draft"

    await engine.dispose()


@pytest.mark.asyncio
async def test_comment_posts_campaign_policy_length_reaches_ai_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    async def fake_generate_comment_text(*args: object, **kwargs: object) -> tuple[str, dict]:
        prompt = args[1]
        assert isinstance(prompt, CommentPromptTemplate)
        captured["max_length"] = int(prompt.max_length)
        return "Комментарий в нужном диапазоне.", {"mode": "test_ai"}

    monkeypatch.setattr("app.modules.campaigns.service.generate_comment_text", fake_generate_comment_text)
    monkeypatch.setattr("app.modules.campaigns.service.real_telegram_enabled", lambda: False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(email="owner@example.com", password_hash=hash_password("password123"))
        session.add(user)
        await session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Test", slug="test")
        session.add(workspace)
        await session.flush()

        source = TelegramSource(
            workspace_id=workspace.id,
            type="channel",
            username="target_channel",
            title="Target channel",
            comments_enabled=True,
            access_status="available",
        )
        account = TelegramAccount(
            workspace_id=workspace.id,
            username="commenter",
            status="active",
            import_type="session",
            health_score=90,
            daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
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

        await create_mock_posts(session, workspace.id, source, limit=1)
        campaign = Campaign(
            workspace_id=workspace.id,
            type="comment_posts",
            name="Neurocomments",
            status="draft",
            config={
                "source_ids": [source.id],
                "account_ids": [account.id],
                "comment_prompt_template_id": prompt.id,
                "latest_posts_per_source": 1,
                "mode": "generate_and_send",
            },
            policy={"min_post_chars": 0, "max_length": 100},
        )
        session.add(campaign)
        await session.commit()

        result = await start_comment_posts_campaign(session, workspace.id, campaign)

        assert result.status == "completed"
        assert captured["max_length"] == 100
        assert prompt.max_length == 280

    await engine.dispose()
