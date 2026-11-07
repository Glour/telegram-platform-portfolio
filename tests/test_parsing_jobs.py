from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.api.deps import get_current_user, get_session, get_workspace_id
from app.db.base import Base
from app.main import app
from app.modules.auth.models import User
from app.modules.parsing.models import ParsingJobLog, ParsingResult
from app.modules.parsing.service import (
    channel_search_from_atlas,
    channel_search_mixed,
    complete_parsing_job,
    create_parsing_job,
    export_parsing_results,
    persist_parsing_results,
    start_parsing_job,
)
from app.modules.plans.models import Entitlement
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount, TelegramSession
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
    workspace = Workspace(owner_user_id="owner", name="Test", slug="test")
    session.add(workspace)
    await session.flush()
    return workspace


async def _account(session: AsyncSession, workspace: Workspace) -> TelegramAccount:
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="parser",
        display_name="Parser",
        status="active",
        import_type="session",
        health_score=90,
    )
    session.add(account)
    await session.flush()
    session.add(
        TelegramSession(
            account_id=account.id,
            encrypted_session_blob="test-session",
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


async def _source(session: AsyncSession, workspace: Workspace) -> TelegramSource:
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="target",
        title="Target",
        comments_enabled=True,
        access_status="available",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return source


@pytest.mark.asyncio
async def test_parsing_job_persists_results_and_exports(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    date_from = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    job = await create_parsing_job(
        session,
        workspace_id=workspace.id,
        job_type="message_users",
        account=account,
        input_payload={"source_ref": "@target"},
        filters={"date_from": date_from, "keywords": ["ипотека"]},
        limits={"message_limit": 100},
    )

    saved = await persist_parsing_results(
        session,
        job=job,
        entity_type="user",
        items=[
            {
                "tg_user_id": "100",
                "username": "buyer",
                "display_name": "Buyer",
                "messages_count": 2,
                "matched_messages_count": 2,
                "matched_keywords": ["ипотека"],
                "last_activity_at": date_from,
            },
            {
                "tg_user_id": "100",
                "username": "buyer",
                "display_name": "Buyer duplicate",
            },
        ],
    )

    rows = list((await session.execute(select(ParsingResult))).scalars().all())
    media_type, filename, csv_content = await export_parsing_results(session, job, export_format="csv")
    _, _, json_content = await export_parsing_results(session, job, export_format="json")
    _, _, txt_content = await export_parsing_results(session, job, export_format="txt")

    assert saved == 1
    assert len(rows) == 1
    assert job.filters["date_from"] == "2026-06-01T12:00:00+00:00"
    assert media_type.startswith("text/csv")
    assert filename.endswith(".csv")
    assert "buyer" in csv_content
    assert "Buyer" in json_content
    assert "buyer" in txt_content


@pytest.mark.asyncio
async def test_channel_search_mixed_falls_back_to_atlas_when_telegram_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    job = await create_parsing_job(
        session,
        workspace_id=workspace.id,
        job_type="channel_search",
        account=account,
        input_payload={"source": "mixed", "keywords": ["fpv"]},
        progress_total=10,
    )
    await start_parsing_job(session, job)

    async def fake_telegram_search(*args: object, **kwargs: object) -> list[dict]:
        raise RuntimeError("telegram unavailable")

    async def fake_atlas_search(*args: object, **kwargs: object) -> list[dict]:
        assert kwargs.get("category_id") is None
        assert kwargs.get("subcategory_id") is None
        return [
            {
                "title": "FPV канал",
                "username": "fpv_channel",
                "ref": "@fpv_channel",
                "participants_count": 2500,
                "comments_enabled": True,
                "engagement_score": 90,
                "status": "found",
                "raw": {"source": "atlas"},
            }
        ]

    monkeypatch.setattr(
        "app.modules.parsing.service.channel_search_from_telegram",
        fake_telegram_search,
    )
    monkeypatch.setattr(
        "app.modules.parsing.service.channel_search_from_atlas",
        fake_atlas_search,
    )

    items = await channel_search_mixed(
        session,
        workspace.id,
        account,
        keywords=["fpv"],
        keyword_suffixes=["обучение"],
        category_id=123,
        subcategory_id=456,
        min_participants=1000,
        max_participants=None,
        active_days=180,
        comments_required=True,
        comments_mode="open",
        limit=10,
        language_code="ru",
        country_code="RU",
        min_engagement_score=80,
        job=job,
    )

    await session.refresh(job)
    logs = list(
        (
            await session.execute(
                select(ParsingJobLog).where(ParsingJobLog.job_id == job.id).order_by(ParsingJobLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert job.status == "completed"
    assert job.found_count == 1
    assert items[0]["ref"] == "@fpv_channel"
    assert any(log.level == "warning" and "Telegram global search" in log.message for log in logs)


@pytest.mark.asyncio
async def test_channel_search_from_atlas_requires_confirmed_open_comments(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load_atlas() -> dict:
        return {
            "clusters": [{"id": 1, "label": "FPV"}],
            "sub_clusters": [],
            "channels": [[None, None, 1, "fpv_channel", "FPV обучение", 2500, "", "123"]],
            "meta": {},
        }

    monkeypatch.setattr("app.modules.parsing.service.load_atlas", fake_load_atlas)

    items = await channel_search_from_atlas(
        session,
        "workspace",
        keywords=["fpv"],
        keyword_suffixes=["обучение"],
        category_id=None,
        subcategory_id=None,
        min_participants=1000,
        max_participants=None,
        comments_required=True,
        comments_mode="open",
        language_code="ru",
        country_code="RU",
        min_engagement_score=80,
        limit=10,
    )

    assert items == []


@pytest.mark.asyncio
async def test_channel_search_mixed_validates_atlas_candidates_for_open_comments(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)

    async def fake_telegram_search(*args: object, **kwargs: object) -> list[dict]:
        return []

    async def fake_atlas_search(*args: object, **kwargs: object) -> list[dict]:
        assert kwargs.get("comments_required") is False
        assert kwargs.get("comments_mode") == "any"
        return [
            {
                "title": "Open channel",
                "username": "open_channel",
                "ref": "@open_channel",
                "participants_count": 2500,
                "comments_enabled": None,
                "status": "needs_telegram_validation",
                "raw": {"source": "atlas"},
            },
            {
                "title": "Closed channel",
                "username": "closed_channel",
                "ref": "@closed_channel",
                "participants_count": 3000,
                "comments_enabled": None,
                "status": "needs_telegram_validation",
                "raw": {"source": "atlas"},
            },
        ]

    class FakeChannel:
        def __init__(self, username: str, title: str) -> None:
            self.id = 100 if username == "open_channel" else 200
            self.username = username
            self.title = title
            self.broadcast = True
            self.participants_count = 2500

    class FakeFull:
        def __init__(self, linked_chat_id: int | None) -> None:
            self.full_chat = type(
                "FakeFullChat",
                (),
                {
                    "linked_chat_id": linked_chat_id,
                    "participants_count": 2500,
                    "about": "Русский FPV канал",
                },
            )()

    class FakeGetFullChannelRequest:
        def __init__(self, channel: object) -> None:
            self.channel = channel

    class FakeClient:
        def __init__(self) -> None:
            self.last_username = ""

        async def get_entity(self, ref: str) -> FakeChannel:
            username = ref.strip("@")
            self.last_username = username
            return FakeChannel(username, "Открытый канал" if username == "open_channel" else "Закрытый канал")

        async def __call__(self, request: object) -> FakeFull:
            return FakeFull(777 if self.last_username == "open_channel" else None)

    class FakeTelegramContext:
        async def __aenter__(self) -> FakeClient:
            return FakeClient()

        async def __aexit__(self, *args: object) -> None:
            return None

    async def fake_latest_message_at(*args: object, **kwargs: object) -> datetime:
        return datetime.now(UTC)

    monkeypatch.setattr("app.modules.parsing.service.channel_search_from_telegram", fake_telegram_search)
    monkeypatch.setattr("app.modules.parsing.service.channel_search_from_atlas", fake_atlas_search)
    monkeypatch.setattr("app.modules.parsing.service.Channel", FakeChannel)
    monkeypatch.setattr("app.modules.parsing.service.GetFullChannelRequest", FakeGetFullChannelRequest)
    monkeypatch.setattr("app.modules.parsing.service.telegram_client_for_account", lambda *args: FakeTelegramContext())
    monkeypatch.setattr("app.modules.parsing.service._latest_channel_message_at", fake_latest_message_at)

    items = await channel_search_mixed(
        session,
        workspace.id,
        account,
        keywords=["fpv"],
        keyword_suffixes=["обучение"],
        category_id=None,
        subcategory_id=None,
        min_participants=1000,
        max_participants=None,
        active_days=180,
        comments_required=True,
        comments_mode="open",
        limit=10,
        language_code="ru",
        country_code="RU",
        min_engagement_score=80,
    )

    assert len(items) == 1
    assert items[0]["ref"] == "@open_channel"
    assert items[0]["comments_enabled"] is True
    assert items[0]["linked_discussion"] == "777"


@pytest.mark.asyncio
async def test_heavy_parsing_job_blocks_same_account_until_finished(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    await create_parsing_job(
        session,
        workspace_id=workspace.id,
        job_type="source_comments",
        account=account,
    )

    with pytest.raises(Exception, match="уже выполняет задачу парсинга"):
        await create_parsing_job(
            session,
            workspace_id=workspace.id,
            job_type="message_users",
            account=account,
        )


@pytest.mark.asyncio
async def test_stale_heavy_parsing_job_is_failed_before_blocking_account(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    stale_job = await create_parsing_job(
        session,
        workspace_id=workspace.id,
        job_type="channel_search",
        account=account,
    )
    stale_job.created_at = datetime.now(UTC) - timedelta(hours=3)
    stale_job.updated_at = datetime.now(UTC) - timedelta(hours=3)
    await session.commit()

    new_job = await create_parsing_job(
        session,
        workspace_id=workspace.id,
        job_type="message_users",
        account=account,
    )
    await session.refresh(stale_job)

    assert stale_job.status == "failed"
    assert stale_job.error_code == "StaleParsingJob"
    assert new_job.status == "queued"


@pytest.mark.asyncio
async def test_parsing_api_covers_all_mvp_entity_types(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user = User(email="owner@example.com", password_hash="test")
    session.add(user)
    await session.flush()
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    source = await _source(session, workspace)
    for feature_code in [
        "module.parsing.dialogs",
        "module.parsing.posts",
        "module.parsing.users",
        "module.parsing.comments",
    ]:
        session.add(Entitlement(workspace_id=workspace.id, feature_code=feature_code, limit_value=1000))
    await session.commit()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_current_user() -> User:
        return user

    async def override_workspace_id() -> str:
        return workspace.id

    async def fake_dialogs(*args: object, **kwargs: object) -> list[dict]:
        return [
            {
                "tg_id": "10",
                "type": "channel",
                "title": "Target channel",
                "username": "target",
                "participants_count": 1200,
                "unread_count": 0,
                "last_message_at": datetime.now(UTC),
                "raw": {},
            }
        ]

    async def fake_users(*args: object, **kwargs: object) -> list[dict]:
        assert kwargs.get("min_messages") == 2
        assert kwargs.get("keywords") == ["ипотека"]
        return [
            {
                "tg_user_id": "100",
                "username": "buyer",
                "display_name": "Buyer",
                "messages_count": 1,
                "comments_count": 0,
                "matched_keywords": [],
                "raw": {},
            }
        ]

    async def fake_comments(*args: object, **kwargs: object) -> list[dict]:
        return [
            {
                "post_tg_message_id": "1",
                "comment_tg_message_id": "2",
                "text": "Интересный комментарий про ипотеку",
                "author_tg_id": "100",
                "author_username": "buyer",
                "author_display_name": "Buyer",
                "published_at": datetime.now(UTC),
                "matched_keywords": ["ипотека"],
                "raw": {},
            }
        ]

    async def fake_message_users(*args: object, **kwargs: object) -> list[dict]:
        return [
            {
                "tg_user_id": "101",
                "username": "chatbuyer",
                "display_name": "Chat Buyer",
                "messages_count": 3,
                "matched_messages_count": 2,
                "matched_keywords": ["новостройка"],
                "last_activity_at": datetime.now(UTC),
                "sample_messages": ["Ищу новостройку"],
                "raw": {},
            }
        ]

    async def fake_channel_search(*args: object, **kwargs: object) -> list[dict]:
        assert args[1] == workspace.id
        assert args[2].id == account.id
        if kwargs.get("keywords") == ["crypto"]:
            assert kwargs.get("keyword_suffixes") == ["signals"]
            assert kwargs.get("min_participants") == 100
            assert kwargs.get("active_days") == 30
            assert kwargs.get("comments_required") is True
            assert kwargs.get("comments_mode") == "open"
            assert kwargs.get("delay_seconds_min") == 1
            assert kwargs.get("delay_seconds_max") == 2
            items = [
                {
                    "title": "Crypto Signals",
                    "username": "cryptosignals",
                    "ref": "@cryptosignals",
                    "participants_count": 1200,
                    "comments_enabled": True,
                    "language_code": "en",
                    "source_risk_level": "unknown",
                    "status": "found",
                    "raw": {"source": "mixed"},
                }
            ]
        else:
            assert kwargs.get("language_code") == "ru"
            assert kwargs.get("country_code") == "RU"
            assert kwargs.get("min_engagement_score") == 40
            assert kwargs.get("comments_mode") == "any"
            items = [
                {
                    "title": "Real Estate",
                    "username": "realestate",
                    "ref": "@realestate",
                    "participants_count": 5000,
                    "source_risk_level": "unknown",
                    "status": "found",
                    "raw": {},
                }
            ]
        job = kwargs.get("job")
        if job:
            await persist_parsing_results(session, job=job, entity_type="channel", items=items)
            await complete_parsing_job(session, job, found_count=len(items))
        return items

    async def fake_group_search(*args: object, **kwargs: object) -> list[dict]:
        assert kwargs.get("keyword_suffixes") == ["чат"]
        assert kwargs.get("min_activity_score") == 30
        assert kwargs.get("access_type") == "public"
        job = kwargs.get("job")
        items = [
            {
                "title": "Invest chat",
                "username": "investchat",
                "ref": "@investchat",
                "participants_count": 700,
                "activity_score": 80,
                "raw": {},
            }
        ]
        if job:
            await persist_parsing_results(session, job=job, entity_type="group", items=items)
            await complete_parsing_job(session, job, found_count=len(items))
        return items

    async def fake_channel_validate(*args: object, **kwargs: object) -> list[dict]:
        assert kwargs.get("source_ids") == [source.id]
        assert kwargs.get("comments_required") is True
        job = kwargs.get("job")
        items = [
            {
                "title": "Target",
                "username": "target",
                "ref": "@target",
                "participants_count": 1200,
                "comments_enabled": True,
                "source_risk_level": "low",
                "status": "valid",
                "raw": {},
            }
        ]
        if job:
            await persist_parsing_results(session, job=job, entity_type="channel", items=items)
            await complete_parsing_job(session, job, found_count=len(items))
        return items

    monkeypatch.setattr("app.modules.parsing.api.parse_account_dialogs", fake_dialogs)
    monkeypatch.setattr("app.modules.parsing.api.parse_source_users", fake_users)
    monkeypatch.setattr("app.modules.parsing.api.parse_source_comments", fake_comments)
    monkeypatch.setattr("app.modules.parsing.api.parse_message_users", fake_message_users)
    monkeypatch.setattr("app.modules.parsing.api.channel_search_mixed", fake_channel_search)
    monkeypatch.setattr("app.modules.parsing.api.group_search_mixed", fake_group_search)
    monkeypatch.setattr("app.modules.parsing.api.validate_channel_sources", fake_channel_validate)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_workspace_id] = override_workspace_id
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            cases = [
                (
                    "post",
                    "/api/v1/parsing/jobs/mock-source",
                    {"source_id": source.id, "limit": 1},
                    "messages",
                ),
                (
                    "dialogs",
                    "/api/v1/parsing/dialogs",
                    {"telegram_account_id": account.id, "entity_type": "channels", "limit": 1},
                    "items",
                ),
                (
                    "users",
                    "/api/v1/parsing/source-users",
                    {
                        "telegram_account_id": account.id,
                        "source_id": source.id,
                        "limit": 1,
                        "min_messages": 2,
                        "keywords": ["ипотека"],
                    },
                    "users",
                ),
                (
                    "comments",
                    "/api/v1/parsing/source-comments",
                    {"telegram_account_id": account.id, "source_id": source.id, "post_limit": 1},
                    "comments",
                ),
                (
                    "message_users",
                    "/api/v1/parsing/message-users",
                    {"telegram_account_id": account.id, "source_id": source.id, "keywords": ["новостройка"]},
                    "users",
                ),
                (
                    "channels",
                    "/api/v1/parsing/channel-search",
                    {
                        "keywords": ["real estate"],
                        "limit": 1,
                        "language_code": "ru",
                        "country_code": "RU",
                        "min_engagement_score": 40,
                    },
                    "items",
                ),
                (
                    "telegram_channels",
                    "/api/v1/parsing/channel-search",
                    {
                        "telegram_account_id": account.id,
                        "keywords": ["crypto"],
                        "keyword_suffixes": ["signals"],
                        "source": "mixed",
                        "limit": 1,
                        "min_participants": 100,
                        "active_days": 30,
                        "language_code": "en",
                        "comments_required": True,
                        "delay_seconds_min": 1,
                        "delay_seconds_max": 2,
                    },
                    "items",
                ),
                (
                    "channel_validate",
                    "/api/v1/parsing/channel-validate",
                    {"telegram_account_id": account.id, "source_ids": [source.id], "comments_required": True},
                    "items",
                ),
                (
                    "groups",
                    "/api/v1/parsing/group-search",
                    {
                        "telegram_account_id": account.id,
                        "keywords": ["invest"],
                        "keyword_suffixes": ["чат"],
                        "min_activity_score": 30,
                        "access_type": "public",
                        "limit": 1,
                    },
                    "items",
                ),
            ]
            job_ids: list[str] = []
            for _name, path, payload, result_key in cases:
                response = await client.post(path, json=payload)
                assert response.status_code < 300, response.text
                body = response.json()
                assert body["job_id"]
                assert body["count"] >= 1 if "count" in body else body["created"] >= 1
                assert body[result_key]
                job_ids.append(body["job_id"])

            export_response = await client.get(f"/api/v1/parsing/jobs/{job_ids[-1]}/export?format=json")
            assert export_response.status_code == 200
            assert "investchat" in export_response.text
    finally:
        app.dependency_overrides.clear()
