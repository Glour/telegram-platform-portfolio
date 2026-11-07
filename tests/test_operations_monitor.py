from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.operations.models import OperationalEvent
from app.modules.operations.service import build_operations_health, run_operations_monitor
from app.modules.sources.models import TelegramSource
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


@pytest.mark.asyncio
async def test_operations_monitor_resolves_live_listener_failure_after_new_heartbeat(
    session: AsyncSession,
) -> None:
    failed_at = datetime.now(UTC) - timedelta(minutes=10)
    account_id = "account-1"
    failed = OperationalEvent(
        workspace_id="workspace-1",
        severity="critical",
        category="live_listener",
        event_type="live_listener_failed",
        title="Live-listener остановился",
        message="Слушатель новых постов Telegram упал.",
        entity_type="telegram_account",
        entity_id=account_id,
        dedupe_key=f"live_listener_failed:{account_id}",
        payload={},
        created_at=failed_at,
        updated_at=failed_at,
    )
    heartbeat = OperationalEvent(
        workspace_id="workspace-1",
        severity="info",
        category="live_listener",
        event_type="live_listener_heartbeat",
        title="Live-listener активен",
        message="Аккаунт слушает источники: 10.",
        entity_type="telegram_account",
        entity_id=account_id,
        dedupe_key=None,
        payload={},
        created_at=failed_at + timedelta(minutes=5),
        updated_at=failed_at + timedelta(minutes=5),
    )
    session.add_all([failed, heartbeat])
    await session.commit()

    await run_operations_monitor(session)

    await session.refresh(failed)
    assert failed.resolved_at is not None


@pytest.mark.asyncio
async def test_operations_monitor_bounds_unresolved_listener_failures(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    session.add_all(
        OperationalEvent(
            severity="critical",
            category="live_listener",
            event_type="live_listener_failed",
            title="Listener failed",
            message="Listener failed",
            entity_id=f"account-{index}",
            created_at=now - timedelta(minutes=index),
            updated_at=now,
        )
        for index in range(150)
    )
    await session.commit()

    checked = 0

    async def fake_latest_heartbeat(*args: object) -> None:
        nonlocal checked
        checked += 1

    monkeypatch.setattr(
        "app.modules.operations.service._latest_live_listener_heartbeat_after",
        fake_latest_heartbeat,
    )

    await run_operations_monitor(session)

    assert checked == 100


@pytest.mark.asyncio
async def test_operations_monitor_resolves_source_errors_after_source_is_archived(
    session: AsyncSession,
) -> None:
    failed_at = datetime.now(UTC) - timedelta(minutes=5)
    user = User(email="owner@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    workspace = Workspace(name="Test", slug="test", owner_user_id=user.id)
    session.add(workspace)
    await session.flush()

    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        title="Closed source",
        access_status="archived",
        comments_enabled=False,
    )
    session.add(source)
    await session.flush()

    event = OperationalEvent(
        workspace_id=workspace.id,
        severity="error",
        category="telegram",
        event_type="comment_send_failed",
        title="Комментарий не отправлен",
        message="Нет доступа к источнику.",
        entity_type="campaign",
        entity_id="campaign-1",
        dedupe_key="comment_send_failed:campaign-1:source-1",
        payload={"source_id": source.id},
        created_at=failed_at,
        updated_at=failed_at,
    )
    session.add(event)
    await session.commit()

    await run_operations_monitor(session)

    await session.refresh(event)
    assert event.resolved_at is not None


@pytest.mark.asyncio
async def test_operations_health_does_not_include_sentry_check(
    session: AsyncSession,
) -> None:
    health = await build_operations_health(session, "workspace-1")

    assert all(check.key != "sentry" for check in health.checks)
