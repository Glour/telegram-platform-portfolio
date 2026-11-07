from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.account_trust.service import collect_account_signals, recompute_account_trust, score_account
from app.modules.proxies.models import Proxy, ProxyCheck
from app.modules.telegram_accounts import api as accounts_api
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility
from app.modules.telegram_accounts.schemas import TelegramAccountPatch
from app.modules.telegram_gateway.contracts import TelegramAccountInfo
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
    workspace = Workspace(owner_user_id="owner", name="Test", slug="trust")
    session.add(workspace)
    await session.flush()
    return workspace


async def _account(session: AsyncSession, workspace: Workspace, **kwargs: object) -> TelegramAccount:
    account = TelegramAccount(
        workspace_id=workspace.id,
        username=kwargs.pop("username", "trust_user"),
        display_name=kwargs.pop("display_name", "Trust User"),
        status=kwargs.pop("status", "active"),
        import_type="session",
        proxy_required=kwargs.pop("proxy_required", False),
        health_score=kwargs.pop("health_score", 90),
        warmup_score=kwargs.pop("warmup_score", 0),
        warmup_status=kwargs.pop("warmup_status", "not_started"),
        daily_limits=kwargs.pop("daily_limits", {}),
        **kwargs,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _proxy(session: AsyncSession, workspace: Workspace) -> Proxy:
    proxy = Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        status="active",
        telegram_connectivity_status="ok",
        proxy_class="static_residential",
        exclusivity="dedicated",
        max_accounts=1,
    )
    session.add(proxy)
    await session.commit()
    await session.refresh(proxy)
    return proxy


@pytest.mark.asyncio
async def test_unknown_restriction_is_not_critical(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, health_score=95)

    scored = score_account(await collect_account_signals(session, account))

    assert scored["safety_status"] == "unknown"
    assert scored["label"] in {"medium", "risky", "good"}
    assert scored["score_100"] >= 40
    assert not scored["hard_blocks"]
    assert scored["recommendations"]["next_best_action"] == "runtime_review_before_launch"


@pytest.mark.asyncio
async def test_not_fully_warmed_neurocommenting_warns_without_forced_limits(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        warmup_score=40,
        warmup_status="not_started",
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
    )

    eligibility = get_module_eligibility(account, "neurocommenting")

    assert eligibility.status == "limited"
    assert eligibility.reason == "not_fully_warmed"
    assert eligibility.allowed_limits is None


@pytest.mark.asyncio
async def test_account_patch_allows_manual_warmup_trust_status(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, warmup_score=0, warmup_status="not_started")

    updated = await accounts_api.update_account(
        account.id,
        TelegramAccountPatch(warmup_score=100, warmup_status="completed"),
        session,
        workspace.id,
    )

    assert updated.warmup_score == 100
    assert updated.warmup_status == "completed"


@pytest.mark.asyncio
async def test_limited_blocks_outbound_even_with_high_health(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        health_score=95,
        warmup_score=100,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {
                "status": "limited",
                "checked_at": datetime.now(UTC).isoformat(),
            }
        },
    )

    snapshot = await recompute_account_trust(session, account, reason="test")

    assert snapshot.safety_status == "limited"
    assert snapshot.score_100 <= 35
    assert snapshot.hard_blocks
    assert "neurocommenting" in snapshot.recommendations["blocked_modules"]


@pytest.mark.asyncio
async def test_clear_does_not_override_new_import_cap(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    proxy = await _proxy(session, workspace)
    account = await _account(
        session,
        workspace,
        proxy_id=proxy.id,
        proxy_required=True,
        warmup_score=100,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {
                "status": "clear",
                "checked_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    session.add(
        ProxyCheck(
            workspace_id=workspace.id,
            proxy_id=proxy.id,
            telegram_account_id=account.id,
            status="ok",
            telegram_latency_ms=500,
        )
    )
    await session.commit()

    scored = score_account(await collect_account_signals(session, account))

    assert scored["safety_status"] == "clear"
    assert scored["score_100"] <= 70


@pytest.mark.asyncio
async def test_score_account_adds_age_and_manual_origin_bonus(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    account = await _account(
        session,
        workspace,
        warmup_score=100,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {
                "status": "clear",
                "checked_at": datetime(2026, 6, 15, tzinfo=UTC).isoformat(),
            },
            "account_metadata": {
                "origin": "manual_phone_registered",
                "real_age_days": 95,
            },
        },
    )

    signals = await collect_account_signals(session, account)
    scored = score_account(signals, now=datetime(2026, 6, 15, tzinfo=UTC))

    positive_keys = {factor["key"] for factor in scored["positive_factors"]}
    assert "account_age_90d" in positive_keys
    assert "origin_manual_phone_registered" in positive_keys
    assert scored["score_100"] >= 90


@pytest.mark.asyncio
async def test_technical_check_does_not_call_service_bots(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session)
    account = await _account(session, workspace, proxy_required=False)
    monkeypatch.setattr("app.modules.telegram_accounts.service.real_telegram_enabled", lambda: True)

    async def fake_check_real_account(*args: object, **kwargs: object) -> TelegramAccountInfo:
        return TelegramAccountInfo(
            tg_user_id=100,
            username="trust_user",
            display_name="Trust User",
            phone="+10000000000",
        )

    monkeypatch.setattr("app.modules.telegram_accounts.service.check_real_account", fake_check_real_account)
    response = await accounts_api.technical_check_account(account.id, session, workspace.id)

    assert response["account_id"] == account.id
    assert response["safety_status"] == "unknown"
    assert response["trust_score"] >= 40
