from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.parsing.service import discover_account_channels_into_sources
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.schemas import TelegramAccountImport
from app.modules.telegram_accounts.service import import_session_account
from app.modules.telegram_gateway.contracts import TelegramDialogSourceInfo
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
        username="collector",
        display_name="Collector",
        status="active",
        import_type="session",
        health_score=90,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


def _channel(
    *,
    username: str | None,
    title: str,
    participants_count: int | None,
    last_message_at: datetime | None,
    tg_id: int = 100,
) -> TelegramDialogSourceInfo:
    return TelegramDialogSourceInfo(
        tg_id=tg_id,
        username=username,
        title=title,
        source_type="channel",
        comments_enabled=False,
        linked_discussion_tg_id=None,
        linked_discussion_username=None,
        metadata={
            "participants_count": participants_count,
            "last_message_at": last_message_at.isoformat() if last_message_at else None,
        },
    )


@pytest.mark.asyncio
async def test_auto_discovery_saves_only_public_active_channels(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    now = datetime.now(UTC)

    async def fake_discover(*args: object, **kwargs: object) -> list[TelegramDialogSourceInfo]:
        return [
            _channel(username="big_live", title="Big Live", participants_count=4200, last_message_at=now),
            _channel(username=None, title="Private", participants_count=5000, last_message_at=now, tg_id=101),
            _channel(username="tiny", title="Tiny", participants_count=50, last_message_at=now, tg_id=102),
            _channel(
                username="old",
                title="Old",
                participants_count=5000,
                last_message_at=now - timedelta(days=400),
                tg_id=103,
            ),
        ]

    monkeypatch.setattr("app.modules.parsing.service.discover_channels_with_account", fake_discover)

    result = await discover_account_channels_into_sources(
        session,
        account,
        min_participants=1000,
        active_days=180,
    )
    sources = list((await session.execute(select(TelegramSource))).scalars().all())

    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["skipped_reasons"] == {
        "private_or_no_username": 1,
        "too_small_or_unknown": 1,
        "inactive": 1,
    }
    assert len(sources) == 1
    assert sources[0].username == "big_live"
    assert sources[0].access_status == "available"
    assert sources[0].source_metadata["auto_discovered"] is True
    assert account.id in sources[0].source_metadata["discovered_accounts"]


@pytest.mark.asyncio
async def test_auto_discovery_updates_existing_channel_by_username(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace)
    existing = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="big_live",
        title="Old title",
        access_status="pending_check",
        source_metadata={"manual": True},
    )
    session.add(existing)
    await session.commit()

    async def fake_discover(*args: object, **kwargs: object) -> list[TelegramDialogSourceInfo]:
        return [
            _channel(
                username="Big_Live",
                title="Fresh title",
                participants_count=4200,
                last_message_at=datetime.now(UTC),
                tg_id=777,
            )
        ]

    monkeypatch.setattr("app.modules.parsing.service.discover_channels_with_account", fake_discover)

    result = await discover_account_channels_into_sources(session, account)
    sources = list((await session.execute(select(TelegramSource))).scalars().all())

    assert result["created"] == 0
    assert result["updated"] == 1
    assert len(sources) == 1
    assert sources[0].tg_id == "777"
    assert sources[0].title == "Fresh title"
    assert sources[0].source_metadata["manual"] is True


@pytest.mark.asyncio
async def test_session_import_does_not_queue_channel_discovery_by_default(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    sent_tasks: list[tuple[str, dict]] = []

    def fake_send_task(name: str, kwargs: dict) -> None:
        sent_tasks.append((name, kwargs))

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.telegram_accounts.service.celery_app.send_task", fake_send_task)
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        max_accounts=1,
    )
    session.add(proxy)
    await session.flush()

    account = await import_session_account(
        session,
        workspace.id,
        TelegramAccountImport(
            session_blob="session",
            phone="+15551234567",
            proxy_id=proxy.id,
        ),
    )

    assert account.id
    assert sent_tasks == []


@pytest.mark.asyncio
async def test_session_import_stays_offline_when_legacy_discovery_enabled(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)
    sent_tasks: list[tuple[str, dict]] = []

    def fake_send_task(name: str, kwargs: dict) -> None:
        sent_tasks.append((name, kwargs))

    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.telegram_accounts.service.get_settings",
        lambda: type("Settings", (), {"telegram_auto_discovery_on_import": True})(),
    )
    monkeypatch.setattr("app.modules.telegram_accounts.service.celery_app.send_task", fake_send_task)
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.2",
        port=1080,
        status="active",
        max_accounts=1,
    )
    session.add(proxy)
    await session.flush()

    account = await import_session_account(
        session,
        workspace.id,
        TelegramAccountImport(
            session_blob="session-2",
            phone="+15551234568",
            proxy_id=proxy.id,
        ),
    )

    assert account.id
    assert sent_tasks == []
