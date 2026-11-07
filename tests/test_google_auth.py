from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.api.deps import get_session
from app.db.base import Base
from app.main import app
from app.modules.auth.models import OAuthState
from app.modules.auth.service import GoogleOAuthError, create_google_oauth_state
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
async def test_google_callback_keeps_state_when_exchange_fails(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    state = await create_google_oauth_state(session)
    await session.commit()

    async def fake_exchange_google_code(code: str) -> dict:
        raise GoogleOAuthError("Google отклонил одноразовый код входа.")

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr("app.modules.auth.api.exchange_google_code", fake_exchange_google_code)
    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/auth/google/callback",
                json={"code": "bad-code", "state": state},
            )

        oauth_state = (await session.execute(select(OAuthState).where(OAuthState.provider == "google"))).scalar_one()
        assert response.status_code == 400
        assert "одноразовый код" in response.text
        assert oauth_state.consumed_at is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_google_callback_consumes_state_after_success(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    state = await create_google_oauth_state(session)
    await session.commit()

    async def fake_exchange_google_code(code: str) -> dict:
        return {
            "sub": "google-user-1",
            "email": "google@example.com",
            "email_verified": True,
            "name": "Google User",
        }

    async def fake_ensure_internal_full_access_for_user(*args: object, **kwargs: object) -> None:
        return None

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr("app.modules.auth.api.exchange_google_code", fake_exchange_google_code)
    monkeypatch.setattr(
        "app.modules.auth.api.ensure_internal_full_access_for_user",
        fake_ensure_internal_full_access_for_user,
    )
    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/auth/google/callback",
                json={"code": "good-code", "state": state},
            )

        oauth_state = (await session.execute(select(OAuthState).where(OAuthState.provider == "google"))).scalar_one()
        workspace = (
            await session.execute(select(Workspace).where(Workspace.name == "Google User"))
        ).scalar_one_or_none()
        assert response.status_code == 200
        assert response.json()["access_token"]
        assert oauth_state.consumed_at is not None
        assert workspace is not None
    finally:
        app.dependency_overrides.clear()
