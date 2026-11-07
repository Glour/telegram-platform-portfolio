from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.api.deps import get_current_user, get_session, get_workspace_id
from app.core.config import DEFAULT_WARMUP_SYSTEM_SOURCE_REFS, default_warmup_system_source_refs
from app.db.base import Base
from app.main import app
from app.modules.account_warmup import service as warmup_service
from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep
from app.modules.ai.models import AiProfile, CommentPromptTemplate
from app.modules.auth.models import User
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignRun
from app.modules.comments.models import TelegramComment
from app.modules.plans.catalog import internal_mvp_limits
from app.modules.plans.models import Entitlement
from app.modules.proxies.models import Proxy, ProxyCheck
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


@dataclass(frozen=True)
class ApiContext:
    client: AsyncClient
    user: User
    workspace: Workspace


@pytest.fixture
async def api_context(session: AsyncSession) -> AsyncIterator[ApiContext]:
    user, workspace = await _workspace(session)

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_current_user() -> User:
        return user

    async def override_workspace_id() -> str:
        return workspace.id

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_workspace_id] = override_workspace_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield ApiContext(client=client, user=user, workspace=workspace)

    app.dependency_overrides.clear()


async def _workspace(session: AsyncSession) -> tuple[User, Workspace]:
    user = User(email="owner@example.com", password_hash="test")
    session.add(user)
    await session.flush()
    workspace = Workspace(owner_user_id=user.id, name="Test", slug=f"test-{user.id[:8]}")
    session.add(workspace)
    await session.flush()
    session.add_all(
        Entitlement(workspace_id=workspace.id, feature_code=feature_code, limit_value=limit, reset_period="month")
        for feature_code, limit in internal_mvp_limits().items()
        if feature_code.startswith("module.")
    )
    await session.flush()
    return user, workspace


def _operator_admission_limits() -> dict:
    return {
        "restriction_status": {
            "status": "clear",
            "checked_at": datetime.now(UTC).isoformat(),
            "evidence_source": "operator_admission",
        }
    }


async def _comment_campaign_parts(
    session: AsyncSession,
    workspace: Workspace,
    suffix: str,
) -> tuple[TelegramSource, TelegramAccount, CommentPromptTemplate]:
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username=f"target_{suffix}",
        title=f"Target {suffix}",
        comments_enabled=True,
        access_status="available",
    )
    proxy = _proxy(workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=workspace.id,
        username=f"commenter_{suffix}",
        status="active",
        import_type="session",
        health_score=90,
        proxy_id=proxy.id,
        daily_limits=_operator_admission_limits(),
    )
    profile = AiProfile(
        workspace_id=workspace.id,
        name=f"Expert {suffix}",
        system_prompt="Write helpful comments",
    )
    session.add_all([source, account, profile])
    await session.flush()
    prompt = CommentPromptTemplate(
        workspace_id=workspace.id,
        name=f"Default {suffix}",
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
    status: str = "draft",
    mode: str = "comment_new_posts",
    name: str = "Neurocomments",
) -> Campaign:
    return Campaign(
        workspace_id=workspace.id,
        type="comment_posts",
        name=name,
        status=status,
        config={
            "source_ids": [source.id],
            "account_ids": [account.id],
            "comment_prompt_template_id": prompt.id,
            "latest_posts_per_source": 5,
            "mode": mode,
            "stop_conditions": {},
        },
        policy={"moderation": "manual_review"},
    )


async def _campaign_with_active_runs(
    session: AsyncSession,
    workspace: Workspace,
    run_statuses: list[str],
) -> tuple[Campaign, list[CampaignRun]]:
    source, account, prompt = await _comment_campaign_parts(session, workspace, "life")
    campaign = _campaign(workspace, source, account, prompt, status="running")
    session.add(campaign)
    await session.flush()
    runs = [
        CampaignRun(
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            status=run_status,
            progress={},
            stop_conditions={},
            report={"status": run_status},
        )
        for run_status in run_statuses
    ]
    session.add_all(runs)
    await session.commit()
    return campaign, runs


def _proxy(workspace: Workspace) -> Proxy:
    return Proxy(
        workspace_id=workspace.id,
        type="socks5",
        host="127.0.0.1",
        port=1080,
        encrypted_username="old-encrypted-username",
        encrypted_password="old-encrypted-password",
        encrypted_url="old-encrypted-url",
        status="active",
        country="US",
        provider="Old provider",
        max_accounts=1,
        note="old note",
    )


@pytest.mark.asyncio
async def test_campaign_list_excludes_deleted_by_default(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, "list")
    active = _campaign(api_context.workspace, source, account, prompt, status="running", name="Active campaign")
    archived = _campaign(api_context.workspace, source, account, prompt, status="archived", name="Archived campaign")
    deleted = _campaign(api_context.workspace, source, account, prompt, status="deleted", name="Deleted campaign")
    session.add_all([active, archived, deleted])
    await session.commit()

    response = await api_context.client.get("/api/v1/campaigns")

    assert response.status_code == 200, response.text
    campaign_ids = {item["id"] for item in response.json()}
    assert active.id in campaign_ids
    assert archived.id in campaign_ids
    assert deleted.id not in campaign_ids


@pytest.mark.asyncio
async def test_running_comment_posts_campaign_can_be_patched(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source_one, account_one, prompt_one = await _comment_campaign_parts(session, api_context.workspace, "one")
    source_two, account_two, prompt_two = await _comment_campaign_parts(session, api_context.workspace, "two")
    campaign = _campaign(
        api_context.workspace,
        source_one,
        account_one,
        prompt_one,
        status="running",
        mode="comment_recent_posts",
    )
    campaign.config = {**campaign.config, "source_cursors": {source_one.id: {"last_seen_message_id": "old"}}}
    session.add(campaign)
    await session.flush()
    active_run = CampaignRun(
        workspace_id=api_context.workspace.id,
        campaign_id=campaign.id,
        status="running",
        progress={},
        stop_conditions={},
        report={"status": "running"},
    )
    session.add(active_run)
    await session.commit()

    payload = {
        "name": "Updated running campaign",
        "source_ids": [source_two.id],
        "account_ids": [account_two.id],
        "comment_prompt_template_id": prompt_two.id,
        "latest_posts_per_source": 17,
        "mode": "comment_new_posts",
        "policy": {"moderation": "manual_review", "daily_comment_limit": 12},
        "stop_conditions": {"max_comments": 5, "manual_stop": True},
    }
    response = await api_context.client.patch(f"/api/v1/campaigns/{campaign.id}", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["name"] == payload["name"]
    assert body["policy"] == payload["policy"]
    assert body["config"]["source_ids"] == [source_two.id]
    assert body["config"]["account_ids"] == [account_two.id]
    assert body["config"]["comment_prompt_template_id"] == prompt_two.id
    assert body["config"]["latest_posts_per_source"] == 17
    assert body["config"]["mode"] == "comment_new_posts"
    assert body["config"]["stop_conditions"]["max_comments"] == 5
    await session.refresh(active_run)
    assert active_run.status == "cancelled"
    assert active_run.report["stop_reason"] == "config_updated"


@pytest.mark.asyncio
async def test_comment_posts_campaign_patch_limit_ignores_unchanged_legacy_source_state(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, "limit-only")
    campaign = _campaign(api_context.workspace, source, account, prompt, status="running")
    session.add(campaign)
    await session.commit()

    source.comments_enabled = False
    source.access_status = "pending_check"
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/campaigns/{campaign.id}",
        json={
            "source_ids": [source.id],
            "account_ids": [account.id],
            "comment_prompt_template_id": prompt.id,
            "latest_posts_per_source": 5,
            "mode": "comment_new_posts",
            "policy": {"moderation": "manual_review"},
            "stop_conditions": {"max_comments": 10, "manual_stop": True},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config"]["source_ids"] == [source.id]
    assert body["config"]["stop_conditions"]["max_comments"] == 10


@pytest.mark.parametrize(
    "field",
    [
        "source_ids",
        "account_ids",
        "comment_prompt_template_id",
        "latest_posts_per_source",
        "mode",
    ],
)
@pytest.mark.asyncio
async def test_comment_posts_campaign_patch_rejects_null_required_config_fields(
    api_context: ApiContext,
    session: AsyncSession,
    field: str,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, f"null-{field}")
    campaign = _campaign(api_context.workspace, source, account, prompt)
    session.add(campaign)
    await session.commit()
    original_config = dict(campaign.config)
    original_policy = dict(campaign.policy)

    response = await api_context.client.patch(f"/api/v1/campaigns/{campaign.id}", json={field: None})

    assert response.status_code == 422, response.text
    assert field in response.text
    await session.refresh(campaign)
    assert campaign.config == original_config
    assert campaign.policy == original_policy


@pytest.mark.asyncio
async def test_comment_posts_campaign_patch_allows_null_clearable_fields(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, "clearable")
    campaign = _campaign(api_context.workspace, source, account, prompt)
    campaign.config = {**campaign.config, "stop_conditions": {"max_comments": 3}}
    campaign.policy = {"moderation": "manual_review", "daily_comment_limit": 4}
    session.add(campaign)
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/campaigns/{campaign.id}",
        json={"policy": None, "stop_conditions": None},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy"] == {}
    assert body["config"]["stop_conditions"] is None
    await session.refresh(campaign)
    assert campaign.policy == {}
    assert campaign.config["stop_conditions"] is None


@pytest.mark.asyncio
async def test_campaign_stop_marks_campaign_stopped_and_stops_active_runs(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    campaign, runs = await _campaign_with_active_runs(session, api_context.workspace, ["pending", "running"])

    response = await api_context.client.post(f"/api/v1/campaigns/{campaign.id}/stop")

    assert response.status_code == 200, response.text
    await session.refresh(campaign)
    assert campaign.status == "stopped"
    for run in runs:
        await session.refresh(run)
        assert run.status in {"cancelled", "stopping"}
        assert run.requested_stop_at is not None


@pytest.mark.parametrize(
    ("method", "path_suffix", "expected_campaign_status"),
    [
        ("post", "/archive", "archived"),
        ("delete", "", "deleted"),
    ],
)
@pytest.mark.asyncio
async def test_campaign_archive_and_delete_soft_lifecycle_stops_active_runs(
    api_context: ApiContext,
    session: AsyncSession,
    method: str,
    path_suffix: str,
    expected_campaign_status: str,
) -> None:
    campaign, runs = await _campaign_with_active_runs(session, api_context.workspace, ["pending", "running"])

    response = await getattr(api_context.client, method)(f"/api/v1/campaigns/{campaign.id}{path_suffix}")

    assert response.status_code in {200, 204}, response.text
    await session.refresh(campaign)
    assert campaign.status == expected_campaign_status
    for run in runs:
        await session.refresh(run)
        assert run.status in {"cancelled", "stopping"}
        assert run.requested_stop_at is not None


@pytest.mark.asyncio
async def test_proxy_patch_updates_editable_fields_without_exposing_credentials(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/proxies/{proxy.id}",
        json={
            "type": "http",
            "host": "10.10.0.5",
            "port": 3128,
            "username": "updated-user",
            "password": "updated-password",
            "country": "NL",
            "provider": "New provider",
            "max_accounts": 3,
            "note": "rotating pool",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "http"
    assert body["host"] == "10.10.0.5"
    assert body["port"] == 3128
    assert body["country"] == "NL"
    assert body["provider"] == "New provider"
    assert body["max_accounts"] == 3
    assert body["note"] == "rotating pool"
    for forbidden_field in ["username", "password", "encrypted_username", "encrypted_password", "encrypted_url"]:
        assert forbidden_field not in body

    await session.refresh(proxy)
    assert proxy.encrypted_username != "old-encrypted-username"
    assert proxy.encrypted_password != "old-encrypted-password"


@pytest.mark.parametrize("field", ["type", "host", "port", "max_accounts"])
@pytest.mark.asyncio
async def test_proxy_patch_rejects_null_required_fields(
    api_context: ApiContext,
    session: AsyncSession,
    field: str,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.commit()
    original_values = {
        "type": proxy.type,
        "host": proxy.host,
        "port": proxy.port,
        "max_accounts": proxy.max_accounts,
        "encrypted_url": proxy.encrypted_url,
    }

    response = await api_context.client.patch(f"/api/v1/proxies/{proxy.id}", json={field: None})

    assert response.status_code == 422, response.text
    assert field in response.text
    await session.refresh(proxy)
    for key, value in original_values.items():
        assert getattr(proxy, key) == value


@pytest.mark.asyncio
async def test_proxy_patch_allows_null_optional_fields_to_clear(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/proxies/{proxy.id}",
        json={
            "username": None,
            "password": None,
            "country": None,
            "provider": None,
            "note": None,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["country"] is None
    assert body["provider"] is None
    assert body["note"] is None
    await session.refresh(proxy)
    assert proxy.encrypted_username is None
    assert proxy.encrypted_password is None
    assert proxy.country is None
    assert proxy.provider is None
    assert proxy.note is None


@pytest.mark.asyncio
async def test_proxy_delete_soft_deletes_and_list_excludes_it(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.commit()

    response = await api_context.client.delete(f"/api/v1/proxies/{proxy.id}")

    assert response.status_code in {200, 204}, response.text
    await session.refresh(proxy)
    assert proxy.status == "deleted"

    list_response = await api_context.client.get("/api/v1/proxies")
    assert list_response.status_code == 200, list_response.text
    assert proxy.id not in {item["id"] for item in list_response.json()}


@pytest.mark.asyncio
async def test_proxy_list_includes_account_count_and_latest_latency(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    active_account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="active_proxy_account",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
    )
    deleted_account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="deleted_proxy_account",
        status="deleted",
        import_type="session",
        proxy_id=proxy.id,
    )
    older_check = ProxyCheck(
        workspace_id=api_context.workspace.id,
        proxy_id=proxy.id,
        status="succeeded",
        latency_ms=190,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    latest_check = ProxyCheck(
        workspace_id=api_context.workspace.id,
        proxy_id=proxy.id,
        status="succeeded",
        latency_ms=83,
        created_at=datetime.now(UTC),
    )
    session.add_all([active_account, deleted_account, older_check, latest_check])
    await session.commit()

    response = await api_context.client.get("/api/v1/proxies")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body[0]["id"] == proxy.id
    assert body[0]["account_count"] == 1
    assert body[0]["latest_latency_ms"] == 83
    assert body[0]["latest_checked_at"] is not None


@pytest.mark.asyncio
async def test_proxy_assign_account_sets_workspace_account_proxy_required(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="needs_proxy",
        status="active",
        import_type="session",
        proxy_required=False,
    )
    session.add_all([proxy, account])
    await session.commit()

    response = await api_context.client.post(
        f"/api/v1/proxies/{proxy.id}/assign-account",
        json={"telegram_account_id": account.id},
    )

    assert response.status_code == 200, response.text
    await session.refresh(account)
    assert account.proxy_id == proxy.id
    assert account.proxy_required is True


@pytest.mark.asyncio
async def test_session_import_rejects_account_without_proxy(
    api_context: ApiContext,
) -> None:
    response = await api_context.client.post(
        "/api/v1/telegram-accounts/import-session",
        json={"session_blob": "test-session-without-proxy", "proxy_required": True},
    )

    assert response.status_code == 400, response.text
    assert "без прокси запрещен" in response.text


@pytest.mark.asyncio
async def test_session_import_can_create_proxy_from_raw_line(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    response = await api_context.client.post(
        "/api/v1/telegram-accounts/import-session",
        json={
            "session_blob": "test-session-with-new-proxy",
            "display_name": "Imported with proxy",
            "new_proxy_raw": "10.10.10.10:1080:user:pass",
            "proxy_required": True,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["proxy_id"]
    assert body["proxy_required"] is True

    proxy = await session.get(Proxy, body["proxy_id"])
    assert proxy is not None
    assert proxy.host == "10.10.10.10"
    assert proxy.port == 1080
    assert proxy.max_accounts == 1


@pytest.mark.asyncio
async def test_session_import_can_create_proxy_with_custom_account_limit(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    response = await api_context.client.post(
        "/api/v1/telegram-accounts/import-session",
        json={
            "session_blob": "test-session-with-shared-new-proxy",
            "display_name": "Imported with shared proxy",
            "new_proxy_raw": "10.10.10.11:1080:user:pass",
            "new_proxy_max_accounts": 3,
            "proxy_required": True,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    proxy = await session.get(Proxy, body["proxy_id"])
    assert proxy is not None
    assert proxy.max_accounts == 3


@pytest.mark.asyncio
async def test_proxy_assign_account_rejects_when_max_accounts_reached(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    existing = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="existing_proxy_user",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
    )
    target = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="overflow_proxy_user",
        status="active",
        import_type="session",
        proxy_required=False,
    )
    session.add_all([existing, target])
    await session.commit()

    response = await api_context.client.post(
        f"/api/v1/proxies/{proxy.id}/assign-account",
        json={"telegram_account_id": target.id},
    )

    assert response.status_code == 400, response.text
    await session.refresh(target)
    assert target.proxy_id is None


@pytest.mark.asyncio
async def test_account_patch_rejects_proxy_over_capacity(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    existing = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="existing_patch_proxy_user",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
    )
    target = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="target_patch_proxy_user",
        status="active",
        import_type="session",
    )
    session.add_all([existing, target])
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/telegram-accounts/{target.id}",
        json={"proxy_id": proxy.id},
    )

    assert response.status_code == 400, response.text
    await session.refresh(target)
    assert target.proxy_id is None


@pytest.mark.asyncio
async def test_proxy_assign_account_rejects_tainted_proxy(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    proxy.status = "tainted"
    target = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="tainted_proxy_target",
        status="active",
        import_type="session",
        proxy_required=False,
    )
    session.add_all([proxy, target])
    await session.commit()

    response = await api_context.client.post(
        f"/api/v1/proxies/{proxy.id}/assign-account",
        json={"telegram_account_id": target.id},
    )

    assert response.status_code == 400, response.text
    await session.refresh(target)
    assert target.proxy_id is None


@pytest.mark.asyncio
async def test_manual_account_quarantine_taints_proxy(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="quarantine_proxy_user",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
        proxy_required=True,
    )
    session.add(account)
    await session.commit()

    response = await api_context.client.post(f"/api/v1/telegram-accounts/{account.id}/quarantine")

    assert response.status_code == 200, response.text
    await session.refresh(proxy)
    assert proxy.status == "tainted"
    assert proxy.last_error_code == "account_manually_quarantined"


@pytest.mark.asyncio
async def test_account_patch_stores_origin_age_and_incubator_metadata(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="origin_age_user",
        status="active",
        import_type="session",
        proxy_required=False,
    )
    session.add(account)
    await session.commit()

    response = await api_context.client.patch(
        f"/api/v1/telegram-accounts/{account.id}",
        json={
            "origin": "sms_service_manual",
            "claimed_age_days": 90,
            "real_age_days": 83,
            "incubator_mode": True,
            "incubator_until_at": "2026-07-01",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["origin"] == "sms_service_manual"
    assert body["claimed_age_days"] == 90
    assert body["real_age_days"] == 83
    assert body["system_age_days"] >= 0
    assert body["trust_age_bonus"] == 12
    assert body["incubator_mode"] is True
    assert body["incubator_until_at"] == "2026-07-01"


@pytest.mark.asyncio
async def test_account_order_is_persisted_and_rejects_partial_list(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    accounts = [
        TelegramAccount(
            workspace_id=api_context.workspace.id,
            username=f"sortable_{index}",
            status="active",
            import_type="session",
            proxy_required=False,
        )
        for index in range(3)
    ]
    archived = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="sortable_archived",
        status="archived",
        import_type="session",
        proxy_required=False,
    )
    session.add_all([*accounts, archived])
    await session.commit()

    ordered_ids = [accounts[2].id, accounts[0].id, accounts[1].id]
    response = await api_context.client.put(
        "/api/v1/telegram-accounts/order",
        json={"account_ids": ordered_ids},
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == ordered_ids

    listed = await api_context.client.get("/api/v1/telegram-accounts")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == ordered_ids

    rejected = await api_context.client.put(
        "/api/v1/telegram-accounts/order",
        json={"account_ids": ordered_ids[:2]},
    )
    assert rejected.status_code == 400


@pytest.mark.asyncio
async def test_incubator_account_is_blocked_for_campaign_modules(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="incubator_campaign_block",
        status="active",
        import_type="session",
        proxy_required=False,
        health_score=90,
        warmup_score=100,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear"},
            "account_metadata": {"incubator_mode": True},
        },
    )
    session.add(account)
    await session.commit()

    response = await api_context.client.get(f"/api/v1/telegram-accounts/{account.id}/risk")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["module_eligibility"]["neurocommenting"]["status"] == "blocked"
    assert body["module_eligibility"]["neurocommenting"]["reason"] == "incubator_mode"
    assert body["module_eligibility"]["bulk_join_chats"]["status"] == "blocked"
    assert body["module_eligibility"]["bulk_join_chats"]["reason"] == "incubator_mode"
    assert body["module_eligibility"]["warmup_safe"]["status"] in {"allowed", "limited"}


@pytest.mark.asyncio
async def test_immortal_candidates_endpoint_returns_manual_clean_aged_accounts(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    candidate = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="immortal_candidate",
        status="active",
        import_type="session",
        proxy_required=True,
        proxy_id=proxy.id,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear"},
            "account_metadata": {
                "origin": "manual_phone_registered",
                "real_age_days": 95,
            },
        },
    )
    incubator = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="not_candidate_incubator",
        status="active",
        import_type="session",
        proxy_required=True,
        proxy_id=proxy.id,
        warmup_score=90,
        warmup_status="completed",
        daily_limits={
            "restriction_status": {"status": "clear"},
            "account_metadata": {
                "origin": "manual_phone_registered",
                "real_age_days": 95,
                "incubator_mode": True,
            },
        },
    )
    session.add_all([candidate, incubator])
    await session.commit()

    response = await api_context.client.get("/api/v1/telegram-accounts/immortal-candidates")

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [item["account"]["id"] for item in body]
    assert candidate.id in ids
    assert incubator.id not in ids
    item = next(item for item in body if item["account"]["id"] == candidate.id)
    assert item["candidate_score"] >= 70
    assert item["account"]["immortal_candidate"] is True


@pytest.mark.asyncio
async def test_proxy_assign_campaign_preserves_policy_and_stores_proxy_metadata(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, "proxy-campaign")
    proxy = _proxy(api_context.workspace)
    campaign = _campaign(api_context.workspace, source, account, prompt)
    campaign.policy = {"moderation": "manual_review", "daily_comment_limit": 12}
    session.add_all([proxy, campaign])
    await session.commit()

    response = await api_context.client.post(
        f"/api/v1/proxies/{proxy.id}/assign-campaign",
        json={"campaign_id": campaign.id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == campaign.id
    assert body["policy"] == {
        "moderation": "manual_review",
        "daily_comment_limit": 12,
        "proxy_id": proxy.id,
    }

    await session.refresh(campaign)
    assert campaign.policy == body["policy"]


@pytest.mark.asyncio
async def test_proxy_usage_returns_safe_accounts_campaigns_and_no_proxy_secrets(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    source, account, prompt = await _comment_campaign_parts(session, api_context.workspace, "proxy-usage")
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    assigned_account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="proxied_account",
        display_name="Proxied account",
        status="active",
        import_type="session",
        proxy_id=proxy.id,
    )
    campaign = _campaign(api_context.workspace, source, account, prompt, name="Assigned campaign")
    campaign.policy = {"moderation": "manual_review", "proxy_id": proxy.id}
    other_campaign = _campaign(api_context.workspace, source, account, prompt, name="Other campaign")
    deleted_campaign = _campaign(api_context.workspace, source, account, prompt, status="deleted", name="Deleted")
    deleted_campaign.policy = {"proxy_id": proxy.id}
    session.add_all([assigned_account, campaign, other_campaign, deleted_campaign])
    await session.commit()

    response = await api_context.client.get(f"/api/v1/proxies/{proxy.id}/usage")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proxy_id"] == proxy.id
    assert body["account_count"] == 1
    assert body["campaign_count"] == 1
    assert body["accounts"] == [
        {
            "id": assigned_account.id,
            "username": "proxied_account",
            "display_name": "Proxied account",
            "status": "active",
        }
    ]
    assert body["campaigns"] == [{"id": campaign.id, "name": "Assigned campaign", "status": "draft"}]
    assert set(body) == {"proxy_id", "accounts", "campaigns", "account_count", "campaign_count"}
    assert set(body["accounts"][0]) == {"id", "username", "display_name", "status"}
    assert set(body["campaigns"][0]) == {"id", "name", "status"}

    serialized = json.dumps(body)
    for forbidden in ["old-encrypted-username", "old-encrypted-password", "old-encrypted-url", "password"]:
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_warmup_plan_create_persists_accounts_scenario_and_policy(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_account",
        status="active",
        import_type="session",
        warmup_status="not_started",
    )
    session.add(account)
    await session.commit()

    payload = {
        "name": "Safe manual warmup",
        "duration_days": 3,
        "account_ids": [account.id],
        "scenario": "safe_manual_intro",
        "policy": {"daily_minutes": 20, "mode": "manual_drafts_only"},
    }
    response = await api_context.client.post("/api/v1/warmup/plans", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["policy"] == payload["policy"]
    assert body.get("account_ids", body.get("config", {}).get("account_ids")) == [account.id]
    assert body.get("scenario", body.get("config", {}).get("scenario")) == "safe_manual_intro"

    plan = await session.get(WarmupPlan, body["id"])
    assert plan is not None
    assert plan.policy == payload["policy"]
    assert plan.config["account_ids"] == [account.id]
    assert plan.config["scenario"] == "safe_manual_intro"


@pytest.mark.asyncio
async def test_warmup_plan_runs_list_returns_existing_runs_without_creating(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="listed_warmup",
        status="active",
        import_type="session",
        warmup_score=11,
    )
    session.add(account)
    await session.flush()

    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="List warmup runs",
        duration_days=3,
        config={"account_ids": [account.id]},
        policy={},
    )
    other_plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Other warmup runs",
        duration_days=3,
        config={"account_ids": [account.id]},
        policy={},
    )
    session.add_all([plan, other_plan])
    await session.flush()

    now = datetime(2026, 1, 2, tzinfo=UTC)
    older_run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="scheduled",
        current_day=1,
        score_before=11,
        score_current=12,
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
    )
    newer_run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        current_day=2,
        score_before=11,
        score_current=18,
        created_at=now,
        updated_at=now,
    )
    other_plan_run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=other_plan.id,
        status="scheduled",
        current_day=3,
        score_before=11,
        score_current=19,
        created_at=now + timedelta(days=1),
        updated_at=now + timedelta(days=1),
    )
    session.add_all([older_run, newer_run, other_plan_run])
    await session.commit()

    response = await api_context.client.get(f"/api/v1/warmup/plans/{plan.id}/runs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [run["id"] for run in body] == [newer_run.id, older_run.id]
    assert [run["warmup_plan_id"] for run in body] == [plan.id, plan.id]
    assert body[0]["status"] == "running"
    assert body[0]["current_day"] == 2

    runs = list((await session.execute(select(WarmupRun))).scalars().all())
    assert {run.id for run in runs} == {older_run.id, newer_run.id, other_plan_run.id}


@pytest.mark.asyncio
async def test_warmup_plan_steps_list_keeps_completed_run_history_with_active_run(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account_a = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="completed_warmup_history",
        status="active",
        import_type="session",
        warmup_score=20,
    )
    account_b = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="active_warmup_history",
        status="active",
        import_type="session",
        warmup_score=20,
    )
    session.add_all([account_a, account_b])
    await session.flush()

    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Warmup history",
        duration_days=1,
        config={"account_ids": [account_a.id, account_b.id]},
        policy={},
    )
    session.add(plan)
    await session.flush()

    now = datetime(2026, 1, 2, tzinfo=UTC)
    completed_run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account_a.id,
        warmup_plan_id=plan.id,
        status="completed",
        current_day=1,
        score_before=20,
        score_current=35,
        started_at=now - timedelta(hours=2),
        completed_at=now - timedelta(hours=1),
    )
    active_run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account_b.id,
        warmup_plan_id=plan.id,
        status="running",
        current_day=1,
        score_before=20,
        score_current=25,
        started_at=now - timedelta(hours=1),
    )
    session.add_all([completed_run, active_run])
    await session.flush()

    completed_step = WarmupStep(
        warmup_run_id=completed_run.id,
        telegram_account_id=account_a.id,
        action_type="read_messages",
        status="succeeded",
        planned_at=now - timedelta(hours=2),
        executed_at=now - timedelta(hours=2),
        result={"message": "Исторический шаг"},
    )
    active_step = WarmupStep(
        warmup_run_id=active_run.id,
        telegram_account_id=account_b.id,
        action_type="view_posts",
        status="queued",
        planned_at=now + timedelta(minutes=10),
    )
    session.add_all([completed_step, active_step])
    await session.commit()

    response = await api_context.client.get(f"/api/v1/warmup/plans/{plan.id}/steps")

    assert response.status_code == 200, response.text
    body = response.json()
    assert {step["id"] for step in body} == {completed_step.id, active_step.id}
    assert {step["warmup_run_id"] for step in body} == {completed_run.id, active_run.id}


@pytest.mark.asyncio
async def test_warmup_plan_runs_list_404s_for_other_workspace_plan(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    other_workspace = Workspace(
        owner_user_id=api_context.user.id,
        name="Other",
        slug=f"other-{api_context.user.id[:8]}",
    )
    session.add(other_workspace)
    await session.flush()

    other_account = TelegramAccount(
        workspace_id=other_workspace.id,
        username="other_workspace_warmup",
        status="active",
        import_type="session",
    )
    session.add(other_account)
    await session.flush()

    other_plan = WarmupPlan(
        workspace_id=other_workspace.id,
        created_by=api_context.user.id,
        name="Other workspace plan",
        duration_days=3,
        config={"account_ids": [other_account.id]},
        policy={},
    )
    session.add(other_plan)
    await session.flush()

    other_run = WarmupRun(
        workspace_id=other_workspace.id,
        telegram_account_id=other_account.id,
        warmup_plan_id=other_plan.id,
        status="scheduled",
        current_day=1,
        score_before=0,
        score_current=0,
    )
    session.add(other_run)
    await session.commit()

    response = await api_context.client.get(f"/api/v1/warmup/plans/{other_plan.id}/runs")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Warmup plan not found"


@pytest.mark.asyncio
async def test_warmup_plan_run_creates_only_safe_manual_steps_without_telegram_gateway(
    monkeypatch: pytest.MonkeyPatch,
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    import app.modules.telegram_gateway.service as telegram_gateway

    def fail_gateway_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("safe warmup scheduling must not call Telegram gateway")

    for gateway_name in [
        "telegram_client_for_account",
        "inspect_source_with_account",
        "fetch_posts_with_account",
        "fetch_comments_with_account",
        "send_comment_with_account",
    ]:
        monkeypatch.setattr(telegram_gateway, gateway_name, fail_gateway_call, raising=False)

    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="safe_warmup",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="not_started",
        warmup_score=7,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Safe manual plan",
        duration_days=2,
        config={"account_ids": [account.id], "scenario": "safe_manual_intro"},
        policy={"daily_minutes": 15, "mode": "manual_drafts_only"},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    runs = list(
        (
            await session.execute(
                select(WarmupRun).where(
                    WarmupRun.workspace_id == api_context.workspace.id,
                    WarmupRun.warmup_plan_id == plan.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].telegram_account_id == account.id
    assert runs[0].status == "scheduled"
    assert runs[0].score_before == 7

    steps = list(
        (await session.execute(select(WarmupStep).where(WarmupStep.warmup_run_id == runs[0].id))).scalars().all()
    )
    assert {step.action_type for step in steps} == {
        "profile_checklist",
        "manual_reading_window",
        "manual_draft_message",
        "rest_window",
    }
    assert {step.status for step in steps} <= {"queued", "manual"}

    assert list((await session.execute(select(TelegramMessage))).scalars().all()) == []
    assert list((await session.execute(select(TelegramComment))).scalars().all()) == []
    assert list((await session.execute(select(CampaignAction))).scalars().all()) == []

    await session.refresh(account)
    assert account.warmup_status == "scheduled"


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [None, "unknown", "suspected", "limited"])
async def test_unverified_or_restricted_warmup_account_creates_no_steps(
    api_context: ApiContext,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str | None,
) -> None:
    async def unexpected_action(*args: object, **kwargs: object) -> dict:
        raise AssertionError("Unverified/restricted warmup account must not execute")

    monkeypatch.setattr(warmup_service, "_perform_action", unexpected_action)
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="unverified_warmup",
        status="active",
        import_type="session",
        proxy_required=False,
        health_score=90,
        warmup_score=90,
        daily_limits={} if restriction is None else {"restriction_status": {"status": restriction}},
    )
    session.add(account)
    await session.flush()
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Unverified",
        duration_days=1,
        config={"account_ids": [account.id], "activities": ["profile_check"]},
        policy={},
    )
    session.add(plan)
    await session.flush()

    runs = await warmup_service.start_warmup_plan(session, api_context.workspace.id, plan)
    await session.flush()

    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert get_module_eligibility(account, "warmup_balanced").status == "blocked"
    assert runs[0].stop_reason
    steps = await session.execute(select(WarmupStep).where(WarmupStep.warmup_run_id == runs[0].id))
    assert list(steps.scalars()) == []
    assert await warmup_service.tick_active_warmup_runs(session, api_context.workspace.id) == []

    await session.refresh(account)
    assert account.warmup_status == "failed"


@pytest.mark.asyncio
async def test_warmup_plan_run_skips_proxy_required_accounts_without_proxy(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_no_proxy",
        status="active",
        import_type="session",
        proxy_required=True,
        proxy_id=None,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="No proxy warmup",
        duration_days=2,
        config={"account_ids": [account.id]},
        policy={},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 400, response.text
    assert "нет выбранных аккаунтов" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_new_account_validation_completion_marks_account_validated(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="validation_done",
        daily_limits=_operator_admission_limits(),
        status="warming",
        import_type="session",
        warmup_status="running",
        warmup_score=0,
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Validation",
        duration_days=1,
        config={"preset": "new_account_validation", "account_ids": []},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        score_before=0,
        score_current=1,
    )
    session.add(run)
    await session.flush()

    await warmup_service._complete_run_if_finished(session, plan, run, account, datetime.now(UTC))

    assert run.status == "completed"
    assert account.warmup_status == "validated"
    assert account.warmup_score == 1
    assert account.status == "warming"


@pytest.mark.asyncio
async def test_regular_warmup_completion_does_not_activate_account(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="regular_completed_warmup",
        daily_limits=_operator_admission_limits(),
        status="warming",
        import_type="session",
        warmup_status="running",
        warmup_score=10,
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Regular completion",
        duration_days=1,
        config={"preset": "primary_warmup", "account_ids": [account.id]},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        score_before=10,
        score_current=35,
    )
    session.add(run)
    await session.flush()

    await warmup_service._complete_run_if_finished(session, plan, run, account, datetime.now(UTC))

    assert run.status == "completed"
    assert account.warmup_status == "completed"
    assert account.warmup_score == 35
    assert account.status == "warming"


@pytest.mark.asyncio
async def test_primary_warmup_failure_stops_remaining_steps(
    api_context: ApiContext,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="primary_failed_action",
        status="active",
        import_type="session_file",
        proxy_required=False,
        warmup_status="running",
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Primary failure gate",
        status="running",
        duration_days=2,
        config={"preset": "primary_warmup", "account_ids": [account.id]},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="view_posts",
        status="queued",
        planned_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    next_step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="read_messages",
        status="queued",
        planned_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session.add_all([step, next_step])
    await session.commit()

    async def failed_action(*args: object, **kwargs: object) -> dict[str, str]:
        return {"status": "failed", "message": "No accessible reading source"}

    monkeypatch.setattr(warmup_service, "_perform_action", failed_action)
    now = datetime.now(UTC)
    await warmup_service._execute_step(session, plan, run, step, account, now)
    await warmup_service._complete_run_if_finished(session, plan, run, account, now)

    assert step.status == "failed"
    assert next_step.status == "skipped"
    assert run.status == "failed"
    assert account.warmup_status == "failed"
    assert plan.status == "completed"
    assert "No accessible reading source" in (run.stop_reason or "")


@pytest.mark.asyncio
async def test_completed_warmup_reconcile_repairs_phase_without_activating_account(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="stale_completed_warmup",
        daily_limits=_operator_admission_limits(),
        status="warming",
        import_type="session",
        warmup_status="warming",
        warmup_score=12,
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Stale completed warmup",
        status="completed",
        duration_days=1,
        config={"preset": "primary_warmup", "account_ids": [account.id]},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="completed",
        score_before=10,
        score_current=12,
        started_at=datetime.now(UTC) - timedelta(days=1),
        completed_at=datetime.now(UTC) - timedelta(hours=12),
    )
    session.add(run)
    await session.commit()

    updated = await warmup_service.reconcile_completed_warmup_accounts(session, api_context.workspace.id)
    await session.refresh(account)

    assert [item.id for item in updated] == [account.id]
    assert account.status == "warming"
    assert account.warmup_status == "completed"
    assert account.warmup_score == 12


@pytest.mark.asyncio
async def test_warmup_plan_run_includes_default_peer_message_exchange(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account_one = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_one",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session",
        proxy_required=False,
    )
    account_two = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_two",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session",
        proxy_required=False,
    )
    session.add_all([account_one, account_two])
    await session.flush()
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Peer warmup",
        duration_days=1,
        config={
            "account_ids": [account_one.id, account_two.id],
            "daily_action_limit": 3,
            "activities": ["peer_message_exchange", "view_posts", "random_pauses"],
        },
        policy={"guided": True},
    )
    session.add(plan)
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id.in_([run["id"] for run in response.json()]))
            )
        )
        .scalars()
        .all()
    )
    assert {step.action_type for step in steps if step.action_type == "peer_message_exchange"} == {
        "peer_message_exchange"
    }


@pytest.mark.asyncio
async def test_warmup_presets_endpoint_returns_safe_new_account_preset(api_context: ApiContext) -> None:
    response = await api_context.client.get("/api/v1/warmup/presets")

    assert response.status_code == 200, response.text
    presets = {item["id"]: item for item in response.json()}
    assert "primary_warmup" in presets
    assert presets["primary_warmup"]["config"]["require_warmup_sources"] is True
    assert presets["primary_warmup"]["config"]["min_warmup_sources"] == 5
    assert presets["primary_warmup"]["config"]["active_hours_from"] == "13:00"
    assert presets["primary_warmup"]["config"]["active_hours_to"] == "15:00"
    assert presets["primary_warmup"]["config"]["first_rest_hours"] == 0
    assert presets["primary_warmup"]["config"]["sequence_sessions"][0]["duration_minutes"] == 30
    assert presets["primary_warmup"]["config"]["sequence_sessions"][1]["duration_minutes"] == 720
    assert presets["primary_warmup"]["config"]["sequence_sessions"][2]["actions_per_hour"] == 10
    assert "new_account_rest" in presets
    assert presets["new_account_rest"]["duration_days"] == 2
    assert presets["new_account_rest"]["config"]["force_profile_check"] is False
    assert presets["new_account_rest"]["policy"]["zero_telegram_actions"] is True
    assert "new_account_safe" in presets
    assert presets["new_account_safe"]["duration_days"] == 7
    assert presets["new_account_safe"]["config"]["daily_action_limits"][:4] == [1, 1, 1, 1]
    assert presets["new_account_safe"]["config"]["force_profile_check"] is False
    assert "set_reactions" not in presets["new_account_safe"]["config"]["activities"]
    assert "controlled_source_join" in presets
    assert presets["controlled_source_join"]["duration_days"] == 14
    assert presets["controlled_source_join"]["config"]["force_profile_check"] is False
    assert presets["controlled_source_join"]["config"]["max_action_counts"]["subscribe_channels"] == 3
    assert "incubator_safe_v2" in presets
    assert presets["incubator_safe_v2"]["duration_days"] == 14
    assert presets["incubator_safe_v2"]["policy"]["stage_based"] is True
    assert presets["incubator_safe_v2"]["config"]["stage_sessions"][0]["stage"] == "rest"
    assert presets["incubator_safe_v2"]["config"]["max_action_counts"]["subscribe_channels"] == 0


@pytest.mark.asyncio
async def test_new_account_safe_preset_builds_slow_ramp_without_early_reactions(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="new_safe_warmup",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="New account safe",
        duration_days=7,
        config={"account_ids": [account.id], "preset": "new_account_safe"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    run = (await session.execute(select(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))).scalar_one()
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id == run.id).order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert 1 <= len(steps) <= 10
    assert steps[0].action_type == "random_pauses"
    assert steps[0].planned_at is not None
    assert run.started_at is not None
    assert steps[0].planned_at >= run.started_at + timedelta(hours=72)
    assert {step.action_type for step in steps}.issubset({"view_posts", "read_messages", "random_pauses"})
    for step in steps:
        day = int((step.result or {}).get("day") or 1)
        if day < 5:
            assert step.action_type == "random_pauses"
    assert all((step.result or {}).get("preset") == "new_account_safe" for step in steps)


@pytest.mark.asyncio
async def test_incubator_safe_v2_builds_stage_based_read_only_sessions(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="incubator_v2_warmup",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy.id,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Incubator safe v2",
        duration_days=14,
        config={"account_ids": [account.id], "preset": "incubator_safe_v2"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    run = (await session.execute(select(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))).scalar_one()
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id == run.id).order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert steps
    stages = [(step.result or {}).get("stage") for step in steps]
    assert stages[:2] == ["rest", "first_check"]
    assert {"read_only", "soft_idle", "work_ready"}.issubset(set(stages))
    assert {step.action_type for step in steps}.issubset(
        {"profile_check", "view_posts", "read_messages", "random_pauses"}
    )
    assert "subscribe_channels" not in {step.action_type for step in steps}
    assert "set_reactions" not in {step.action_type for step in steps}
    assert "peer_message_exchange" not in {step.action_type for step in steps}
    first_read = next(step for step in steps if step.action_type in {"view_posts", "read_messages"})
    assert run.started_at is not None
    assert first_read.planned_at is not None
    assert first_read.planned_at >= run.started_at + timedelta(days=2)
    assert all((step.result or {}).get("stage_based") is True for step in steps)
    await session.refresh(account)
    assert account.incubator_mode is True
    assert account.incubator_until_at is not None
    eligibility = get_module_eligibility(account, "neurocommenting")
    assert eligibility.status == "blocked"
    assert eligibility.reason == "incubator_mode"


@pytest.mark.asyncio
async def test_default_warmup_source_pool_has_production_seed_catalog() -> None:
    refs = [item for item in default_warmup_system_source_refs().split(",") if item]

    assert len(refs) >= 50
    assert len(refs) == len(set(refs))
    assert refs == list(DEFAULT_WARMUP_SYSTEM_SOURCE_REFS)
    assert {
        "@telegram",
        "@rbc_news",
        "@technomedia",
        "@hh_ru_official",
        "@kuda_v_moskva",
    }.issubset(set(refs))


@pytest.mark.asyncio
async def test_primary_warmup_materializes_source_pool_from_refs(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="primary_source_pool_user",
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    session.add(account)
    await session.commit()

    response = await api_context.client.post(
        "/api/v1/warmup/plans",
        json={
            "name": "Primary warmup",
            "duration_days": 1,
            "account_ids": [account.id],
            "config": {
                "preset": "primary_warmup",
                "warmup_source_refs": [
                    "@warm_one",
                    "@warm_two",
                    "@warm_three",
                    "@warm_four",
                    "@warm_five",
                ],
            },
            "policy": {"guided": True},
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["config"]["source_strategy"] == "warmup_pool"
    assert len(body["config"]["warmup_source_ids"]) >= 10
    sources = list((await session.execute(select(TelegramSource))).scalars().all())
    assert len(sources) >= 10
    assert all((source.source_metadata or {}).get("warmup_pool") is True for source in sources)
    assert any((source.source_metadata or {}).get("warmup_pool_source") == "system_catalog" for source in sources)
    assert any(source.username == "warm_one" for source in sources)


@pytest.mark.asyncio
async def test_primary_warmup_uses_system_source_pool_before_start(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="primary_no_sources",
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy.id,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Primary with system sources",
        duration_days=1,
        config={"account_ids": [account.id], "preset": "primary_warmup"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    await session.refresh(plan)
    assert len(plan.config["warmup_source_ids"]) >= 5
    sources = list((await session.execute(select(TelegramSource))).scalars().all())
    assert len(sources) >= 5
    assert all((source.source_metadata or {}).get("warmup_pool") is True for source in sources)


@pytest.mark.asyncio
async def test_primary_warmup_builds_two_read_sessions_with_rest(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="primary_with_sources",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy.id,
    )
    sources = [
        TelegramSource(
            workspace_id=api_context.workspace.id,
            type="channel",
            username=f"warm_source_{index}",
            access_status="available",
            source_metadata={"warmup_pool": True},
        )
        for index in range(5)
    ]
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Primary with sources",
        duration_days=1,
        config={"account_ids": [account.id], "preset": "primary_warmup"},
        policy={"guided": True},
    )
    session.add_all([account, plan, *sources])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    run = (await session.execute(select(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))).scalar_one()
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id == run.id).order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    stages = [(step.result or {}).get("stage") for step in steps]
    assert "first_session" in stages
    assert "first_rest" in stages
    assert "second_session" in stages
    assert "second_rest" in stages
    assert {step.action_type for step in steps}.issubset({"view_posts", "read_messages", "random_pauses"})
    moscow = ZoneInfo("Europe/Moscow")
    action_steps = [step for step in steps if "rest" not in str((step.result or {}).get("stage") or "")]
    assert action_steps
    assert run.started_at is not None
    first_action_at = min(step.planned_at for step in action_steps if step.planned_at)
    assert first_action_at >= run.started_at
    for step in action_steps:
        planned_at = step.planned_at if step.planned_at.tzinfo else step.planned_at.replace(tzinfo=UTC)
        local_planned_at = planned_at.astimezone(moscow)
        assert local_planned_at.hour in {13, 14}
    session_action_steps = [step for step in action_steps if (step.result or {}).get("stage") == "first_session"]
    assert len({step.planned_at for step in session_action_steps}) == len(session_action_steps)
    first_session_steps = [step for step in action_steps if (step.result or {}).get("stage") == "first_session"]
    second_session_steps = [step for step in action_steps if (step.result or {}).get("stage") == "second_session"]
    assert max(step.planned_at for step in first_session_steps if step.planned_at) < min(
        step.planned_at for step in second_session_steps if step.planned_at
    )
    second_session_at = min(step.planned_at for step in second_session_steps if step.planned_at)
    first_rest_at = next(step.planned_at for step in steps if (step.result or {}).get("stage") == "first_rest")
    second_rest_at = next(step.planned_at for step in steps if (step.result or {}).get("stage") == "second_rest")
    assert first_rest_at >= max(step.planned_at for step in first_session_steps if step.planned_at) + timedelta(
        hours=12
    )
    assert second_session_at >= max(step.planned_at for step in first_session_steps if step.planned_at) + timedelta(
        hours=12
    )
    assert second_rest_at >= max(step.planned_at for step in second_session_steps if step.planned_at) + timedelta(
        hours=12
    )


@pytest.mark.asyncio
async def test_warmup_source_picker_uses_only_available_sources(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    blocked = TelegramSource(
        workspace_id=api_context.workspace.id,
        type="channel",
        username="blocked_warmup_source",
        title="Blocked",
        access_status="failed",
    )
    available = TelegramSource(
        workspace_id=api_context.workspace.id,
        type="channel",
        username="available_warmup_source",
        title="Available",
        access_status="available",
    )
    session.add_all([blocked, available])
    await session.flush()
    step = WarmupStep(
        warmup_run_id="run",
        telegram_account_id="account",
        action_type="read_messages",
        status="queued",
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Source picker",
        duration_days=1,
        config={"activities": ["read_messages"]},
        policy={},
    )

    source = await warmup_service._pick_source_for_step(session, api_context.workspace.id, plan, step)

    assert source is not None
    assert source.id == available.id


@pytest.mark.asyncio
async def test_warmup_pool_picker_requires_pool_marker_even_for_source_ids(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    unmarked = TelegramSource(
        workspace_id=api_context.workspace.id,
        type="channel",
        username="unmarked_warmup_source",
        access_status="available",
    )
    marked = TelegramSource(
        workspace_id=api_context.workspace.id,
        type="channel",
        username="marked_warmup_source",
        access_status="available",
        source_metadata={"warmup_pool": True},
    )
    session.add_all([unmarked, marked])
    await session.flush()
    step = WarmupStep(
        warmup_run_id="run",
        telegram_account_id="account",
        action_type="read_messages",
        status="queued",
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Source picker",
        duration_days=1,
        config={
            "activities": ["read_messages"],
            "source_strategy": "warmup_pool",
            "warmup_source_ids": [unmarked.id, marked.id],
        },
        policy={},
    )

    source = await warmup_service._pick_source_for_step(session, api_context.workspace.id, plan, step)

    assert source is not None
    assert source.id == marked.id


@pytest.mark.asyncio
async def test_controlled_source_join_preset_delays_and_caps_join_actions(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="controlled_join_account",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Controlled join",
        duration_days=14,
        config={"account_ids": [account.id], "preset": "controlled_source_join"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    run = (await session.execute(select(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))).scalar_one()
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id == run.id).order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    join_steps = [step for step in steps if step.action_type == "subscribe_channels"]

    assert 1 <= len(steps) <= 16
    assert len(join_steps) <= 3
    assert steps[0].action_type == "random_pauses"
    assert run.started_at is not None
    assert steps[0].planned_at is not None
    assert steps[0].planned_at >= run.started_at + timedelta(hours=96)
    for step in steps:
        day = int((step.result or {}).get("day") or 1)
        if step.action_type == "subscribe_channels":
            assert day >= 8


@pytest.mark.asyncio
async def test_warmup_tick_waits_when_account_is_used_by_active_campaign(
    api_context: ApiContext,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_busy_account",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    session.add(account)
    await session.flush()
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Busy account warmup",
        duration_days=1,
        config={"account_ids": [account.id], "activities": ["profile_check"], "daily_action_limit": 1},
        policy={"guided": True},
    )
    campaign = Campaign(
        workspace_id=api_context.workspace.id,
        type="comments",
        name="Busy campaign",
        status="running",
        config={"account_ids": [account.id]},
        policy={},
    )
    session.add_all([plan, campaign])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=1),
        score_before=0,
        score_current=0,
    )
    session.add(run)
    await session.flush()
    step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="profile_check",
        status="queued",
        planned_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    campaign_run = CampaignRun(
        workspace_id=api_context.workspace.id,
        campaign_id=campaign.id,
        status="running",
        progress={},
        stop_conditions={},
        report={},
    )
    session.add_all([step, campaign_run])
    await session.commit()

    async def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("warmup must not call Telegram while account is used by an active campaign")

    monkeypatch.setattr(warmup_service, "check_real_account", fail_if_called)

    ticked = await warmup_service.tick_active_warmup_runs(session, api_context.workspace.id, limit=10)
    await session.refresh(step)
    await session.refresh(account)
    await session.refresh(run)

    assert ticked == []
    assert step.status == "queued"
    assert account.warmup_status == "paused_by_campaign"
    assert "активной кампанией" in (run.stop_reason or "")


@pytest.mark.asyncio
async def test_warmup_tick_completes_due_pause_when_account_is_used_by_active_campaign(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_busy_pause_account",
        daily_limits={},
        status="active",
        import_type="session_file",
        proxy_required=False,
        warmup_score=100,
    )
    session.add(account)
    await session.flush()
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Busy account pause warmup",
        duration_days=1,
        config={"account_ids": [account.id], "activities": ["random_pauses"], "daily_action_limit": 1},
        policy={"guided": True},
    )
    campaign = Campaign(
        workspace_id=api_context.workspace.id,
        type="mass_reactions",
        name="Busy reactions",
        status="running",
        config={"account_ids": [account.id]},
        policy={},
    )
    session.add_all([plan, campaign])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=2),
        score_before=100,
        score_current=100,
        stop_reason="Аккаунт используется активной кампанией; прогрев ждет свободного окна.",
    )
    session.add(run)
    await session.flush()
    step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="random_pauses",
        status="queued",
        planned_at=datetime.now(UTC) - timedelta(hours=1),
    )
    campaign_run = CampaignRun(
        workspace_id=api_context.workspace.id,
        campaign_id=campaign.id,
        status="running",
        progress={},
        stop_conditions={},
        report={},
    )
    session.add_all([step, campaign_run])
    await session.commit()

    ticked = await warmup_service.tick_active_warmup_runs(session, api_context.workspace.id, limit=10)
    await session.refresh(step)
    await session.refresh(account)
    await session.refresh(run)
    await session.refresh(plan)

    assert [item.id for item in ticked] == [run.id]
    assert step.status == "succeeded"
    assert step.executed_at is not None
    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.stop_reason is None
    assert plan.status == "completed"
    assert account.warmup_status == "completed"


@pytest.mark.asyncio
async def test_warmup_tick_persists_runtime_terminal_events(
    api_context: ApiContext,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="warmup_terminal_account",
        daily_limits=_operator_admission_limits(),
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Terminal warmup",
        duration_days=1,
        config={"account_ids": [account.id], "activities": ["profile_check"], "daily_action_limit": 1},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=api_context.workspace.id,
        telegram_account_id=account.id,
        warmup_plan_id=plan.id,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=1),
        score_before=0,
        score_current=0,
    )
    session.add(run)
    await session.flush()
    step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="profile_check",
        status="queued",
        planned_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    future_step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="random_pauses",
        status="queued",
        planned_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add_all([step, future_step])
    await session.commit()

    async def fake_check_real_account(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(username="warmup_terminal_account")

    monkeypatch.setattr(warmup_service, "check_real_account", fake_check_real_account)

    ticked = await warmup_service.tick_active_warmup_runs(session, api_context.workspace.id, limit=10)
    await session.refresh(step)
    await session.refresh(future_step)

    assert [run.id for run in ticked] == [run.id]
    assert step.status == "succeeded"
    events = (step.result or {}).get("terminal_events")
    assert isinstance(events, list)
    assert [event["level"] for event in events] == ["info", "info", "success", "success"]
    assert any("Подключаю Telegram-сессию" in event["message"] for event in events)
    assert future_step.status == "queued"
    assert future_step.result is None


@pytest.mark.asyncio
async def test_warmup_tick_processes_due_step_for_each_active_run(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    accounts = [
        TelegramAccount(
            workspace_id=api_context.workspace.id,
            username=f"parallel_warmup_{index}",
            daily_limits={},
            status="active",
            import_type="session_file",
            proxy_required=False,
        )
        for index in range(2)
    ]
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Parallel warmup",
        duration_days=1,
        config={"account_ids": [account.id for account in accounts], "activities": ["random_pauses"]},
        policy={"guided": True},
    )
    session.add_all([*accounts, plan])
    await session.flush()

    runs: list[WarmupRun] = []
    steps: list[WarmupStep] = []
    for account in accounts:
        run = WarmupRun(
            workspace_id=api_context.workspace.id,
            telegram_account_id=account.id,
            warmup_plan_id=plan.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            score_before=0,
            score_current=0,
        )
        session.add(run)
        await session.flush()
        step = WarmupStep(
            warmup_run_id=run.id,
            telegram_account_id=account.id,
            action_type="random_pauses",
            status="queued",
            planned_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        session.add(step)
        runs.append(run)
        steps.append(step)
    await session.commit()

    ticked = await warmup_service.tick_active_warmup_runs(session, api_context.workspace.id, limit=10)
    for step in steps:
        await session.refresh(step)

    assert {run.id for run in ticked} == {run.id for run in runs}
    assert [step.status for step in steps] == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_new_account_rest_preset_creates_no_telegram_actions(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="new_rest_warmup",
        daily_limits={},
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="New account rest",
        duration_days=2,
        config={"account_ids": [account.id], "preset": "new_account_rest"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    run = (await session.execute(select(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))).scalar_one()
    steps = list(
        (
            await session.execute(
                select(WarmupStep).where(WarmupStep.warmup_run_id == run.id).order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [step.action_type for step in steps] == ["random_pauses"]
    assert steps[0].planned_at is not None
    assert run.started_at is not None
    assert steps[0].planned_at >= run.started_at + timedelta(hours=24)
    assert (steps[0].result or {}).get("preset") == "new_account_rest"


@pytest.mark.asyncio
async def test_balanced_preset_delays_risky_actions_until_later_days(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy_one = _proxy(api_context.workspace)
    proxy_two = _proxy(api_context.workspace)
    proxy_two.host = "127.0.0.2"
    session.add_all([proxy_one, proxy_two])
    await session.flush()
    account_one = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="balanced_one",
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy_one.id,
    )
    account_two = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="balanced_two",
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy_two.id,
    )
    session.add_all([account_one, account_two])
    await session.flush()
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Balanced",
        duration_days=7,
        config={"account_ids": [account_one.id, account_two.id], "preset": "balanced_human"},
        policy={"guided": True},
    )
    session.add(plan)
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 201, response.text
    steps = list(
        (
            await session.execute(
                select(WarmupStep)
                .where(WarmupStep.warmup_run_id.in_([run["id"] for run in response.json()]))
                .order_by(WarmupStep.planned_at.asc())
            )
        )
        .scalars()
        .all()
    )
    for step in steps:
        day = int((step.result or {}).get("day") or 1)
        if step.action_type in {"set_reactions", "peer_message_exchange"}:
            assert day >= 3
        if step.action_type == "subscribe_channels":
            assert day >= 5


@pytest.mark.asyncio
async def test_balanced_warmup_preset_rejects_accounts_without_proxy(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="balanced_without_proxy",
        status="active",
        import_type="session_file",
        proxy_required=False,
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Balanced no proxy",
        duration_days=7,
        config={"account_ids": [account.id], "preset": "balanced_human"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 400, response.text
    assert "требует назначенный прокси" in response.json()["detail"]


@pytest.mark.asyncio
async def test_aggressive_warmup_preset_rejects_new_accounts(
    api_context: ApiContext,
    session: AsyncSession,
) -> None:
    proxy = _proxy(api_context.workspace)
    session.add(proxy)
    await session.flush()
    account = TelegramAccount(
        workspace_id=api_context.workspace.id,
        username="aggressive_new",
        status="active",
        import_type="session_file",
        proxy_required=True,
        proxy_id=proxy.id,
        daily_limits={"restriction_status": "clear"},
    )
    plan = WarmupPlan(
        workspace_id=api_context.workspace.id,
        created_by=api_context.user.id,
        name="Aggressive new",
        duration_days=5,
        config={"account_ids": [account.id], "preset": "aggressive_old_account"},
        policy={"guided": True},
    )
    session.add_all([account, plan])
    await session.commit()

    response = await api_context.client.post(f"/api/v1/warmup/plans/{plan.id}/runs", json={})

    assert response.status_code == 400, response.text
    assert "Агрессивный автоматический прогрев отключен" in response.json()["detail"]
