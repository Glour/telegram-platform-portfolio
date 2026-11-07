from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.campaigns import account_replacement as replacement
from app.modules.campaigns import api as campaigns_api
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignRun
from app.modules.campaigns.schemas import CampaignAccountReplacementRequest, CommentPostsCampaignUpdate
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_safety.admission import owner_join_limits, replacement_pacing_scope
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
from app.modules.workspaces.models import Workspace


@pytest.fixture
async def rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(email="replacement@test.local", password_hash="x", status="active")
        session.add(user)
        await session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Replacement", slug="replacement")
        session.add(workspace)
        await session.flush()
        proxy = Proxy(
            workspace_id=workspace.id, type="socks5", host="127.0.0.1", port=1080, status="active", max_accounts=3
        )
        session.add(proxy)
        await session.flush()
        accounts = [
            TelegramAccount(
                workspace_id=workspace.id,
                status="active",
                import_type="session",
                proxy_id=proxy.id,
                health_score=90,
                warmup_score=90,
                warmup_status="completed",
                daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
            )
            for i in range(3)
        ]
        session.add_all(accounts)
        await session.flush()
        old, new, other = accounts
        for a in accounts:
            session.add(
                TelegramSafetyState(
                    account_id=a.id,
                    manual_hold=False,
                    restriction_status="clear",
                    restriction_checked_at=datetime.now(UTC),
                    operator_approved_at=datetime.now(UTC),
                )
            )
        sources = [
            TelegramSource(
                workspace_id=workspace.id,
                type="channel",
                username=f"channel{i}",
                comments_enabled=True,
                access_status="available",
                source_metadata={"account_access": {old.id: {"joined": True, "access_checked": True}}},
            )
            for i in range(3)
        ]
        session.add_all(sources)
        await session.flush()
        campaign = Campaign(
            workspace_id=workspace.id,
            type="comment_posts",
            name="Replacement",
            status="running",
            config={
                "account_ids": [old.id],
                "source_ids": [s.id for s in sources],
                "source_assignment_mode": "manual_pinned",
                "source_assignment": {old.id: [s.id for s in sources]},
                "source_cursors": {sources[0].id: 42},
                "comment_prompt_template_id": "unchanged",
            },
            policy={"daily_limit_per_account": 5, "auto_join_sources": False, "auto_join_discussions": False},
        )
        session.add(campaign)
        await session.flush()
        run = CampaignRun(
            workspace_id=workspace.id, campaign_id=campaign.id, status="running", progress={"comments_sent": 3}
        )
        session.add(run)
        action = CampaignAction(
            campaign_id=campaign.id,
            telegram_account_id=old.id,
            target_type="post",
            target_id="post",
            action_type="generate_and_send_comment",
            status="queued",
        )
        session.add(action)
        await session.commit()
        clock = [datetime.now(UTC)]
        calls = []
        joined = set()
        settings = SimpleNamespace(telegram_operations_enabled=True)
        monkeypatch.setattr(replacement, "now", lambda: clock[0])
        monkeypatch.setattr(replacement, "get_settings", lambda: settings)
        monkeypatch.setattr(replacement, "real_telegram_enabled", lambda: True)
        discussion = [False]
        failure = [None]

        class Client:
            async def get_entity(self, ref):
                index = int(str(ref).removeprefix("channel"))
                return SimpleNamespace(
                    id=index + 100,
                    broadcast=True,
                    megagroup=False,
                    join_request=False,
                    left=(index + 100) not in joined,
                )

            async def __call__(self, request):
                if isinstance(request, JoinChannelRequest):
                    calls.append((request.channel.id, clock[0]))
                    if failure[0]:
                        raise RuntimeError(failure[0])
                    joined.add(request.channel.id)
                    session.add(
                        TelegramOperation(
                            account_id=new.id, method="JoinChannelRequest", outcome="accepted", created_at=clock[0]
                        )
                    )
                    await session.commit()
                    return SimpleNamespace()
                assert isinstance(request, GetFullChannelRequest)
                linked_id = request.channel.id + 1000
                linked = SimpleNamespace(
                    id=linked_id,
                    username=f"discussion{linked_id}",
                    left=linked_id not in joined,
                    megagroup=True,
                    broadcast=False,
                    join_request=False,
                    join_to_send=discussion[0],
                )
                return SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=linked_id), chats=[linked])

        @asynccontextmanager
        async def client(*args, **kwargs):
            yield Client()

        monkeypatch.setattr(replacement, "telegram_client_for_account", client)
        yield SimpleNamespace(
            session=session,
            user=user,
            workspace=workspace,
            old=old,
            new=new,
            other=other,
            campaign=campaign,
            sources=sources,
            run=run,
            action=action,
            clock=clock,
            calls=calls,
            joined=joined,
            settings=settings,
            discussion=discussion,
            failure=failure,
        )
    await engine.dispose()


def payload(rig, interval=300, resume=False):
    return CampaignAccountReplacementRequest(
        old_account_id=rig.old.id,
        new_account_id=rig.new.id,
        join_interval_seconds=interval,
        resume_when_ready=resume,
        request_id="once",
    )


async def request(rig, **kwargs):
    return await replacement.request_replacement(rig.session, rig.campaign, payload(rig, **kwargs), rig.user.id)


async def test_replacement_is_transactional_and_keeps_sources_policy_and_history(rig):
    await request(rig)
    assert rig.calls == []
    assert rig.campaign.status == "paused"
    assert rig.campaign.config["source_ids"] == [s.id for s in rig.sources]
    assert rig.campaign.config["source_assignment"] == {rig.new.id: [s.id for s in rig.sources]}
    assert rig.campaign.config["source_cursors"] == {rig.sources[0].id: 42}
    assert rig.campaign.policy["daily_limit_per_account"] == 5
    assert rig.run.status == "cancelled" and rig.run.progress["comments_sent"] == 3
    assert rig.action.status == "cancelled"
    assert rig.sources[0].source_metadata["account_access"][rig.old.id]["joined"]
    assert not rig.sources[0].source_metadata["account_access"].get(rig.new.id)
    assert await request(rig) is rig.campaign


@pytest.mark.parametrize("interval", [60, 300, 600])
async def test_channel_and_discussion_share_selected_interval_and_resume_only_when_ready(rig, interval):
    rig.discussion[0] = True
    await request(rig, interval=interval, resume=True)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1 and rig.campaign.status == "paused"
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    for i in range(1, 6):
        rig.clock[0] += timedelta(seconds=interval + 1)
        await replacement.tick_replacement(rig.session, rig.campaign.id)
        assert len(rig.calls) == i + 1
        if i < 5:
            assert rig.campaign.status == "paused"
    assert rig.campaign.config["account_replacement"]["status"] == "ready"
    assert rig.campaign.status == "pending"
    assert all(j["status"] == "joined" for j in rig.campaign.config["account_replacement"]["jobs"])
    for source in rig.sources:
        assert source.source_metadata["account_access"][rig.new.id]["discussion_joined"] is True
    new_run = (await rig.session.scalars(select(CampaignRun).where(CampaignRun.id != rig.run.id))).one()
    assert new_run.progress == {"comments_sent": 3}
    assert all((b[1] - a[1]).total_seconds() >= interval for a, b in zip(rig.calls, rig.calls[1:], strict=False))


async def test_existing_memberships_are_checked_without_rejoining(rig):
    rig.joined.update([100, 101, 102])
    await request(rig)
    for _ in range(3):
        await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls
    assert rig.campaign.config["account_replacement"]["status"] == "ready"
    assert rig.campaign.status == "paused"


async def test_failed_or_interrupted_attempt_is_not_replayed(rig):
    await request(rig)
    rig.failure[0] = "failure"
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    assert rig.campaign.config["account_replacement"]["status"] == "needs_review"
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    assert not rig.sources[0].source_metadata["account_access"].get(rig.new.id)


async def test_attempting_job_requires_reconciliation(rig):
    await request(rig)
    data = deepcopy(rig.campaign.config["account_replacement"])
    data["jobs"][0]["status"] = "attempting"
    rig.campaign.config = {**rig.campaign.config, "account_replacement": data}
    await rig.session.commit()
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls
    assert rig.campaign.config["account_replacement"]["status"] == "needs_review"
    with pytest.raises(HTTPException):
        await replacement.cancel_replacement(rig.session, rig.campaign, rig.user.id)


async def test_hold_waits_without_fake_admission_and_global_barrier_does_not_start(rig):
    state = await rig.session.get(TelegramSafetyState, rig.new.id)
    state.manual_hold = True
    state.reason = "operator_review"
    await rig.session.commit()
    await request(rig)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls
    assert rig.campaign.config["account_replacement"]["status"] == "waiting_account"
    assert state.manual_hold
    rig.settings.telegram_operations_enabled = False
    state.manual_hold = False
    await rig.session.commit()
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls
    assert rig.campaign.config["account_replacement"]["status"] == "waiting_account"


async def test_all_accounts_visible_with_occupied_and_invalid_reasons(rig):
    occupied = Campaign(
        workspace_id=rig.workspace.id,
        type="comment_posts",
        status="paused",
        name="Other",
        config={"account_ids": [rig.other.id]},
        policy={},
    )
    rig.session.add(occupied)
    rig.new.status = "invalid"
    await rig.session.commit()
    rows = await replacement.replacement_accounts(rig.session, rig.campaign)
    assert len(rows) == 3
    by_id = {a["id"]: a for a in rows}
    assert not by_id[rig.new.id]["can_select"]
    assert "Other" in by_id[rig.other.id]["reason"]
    assert not by_id[rig.other.id]["can_select"]
    with pytest.raises(HTTPException):
        await request(rig)


async def test_foreign_workspace_and_nonowner_cannot_replace(rig):
    with pytest.raises(HTTPException) as exc:
        await replacement.request_replacement(rig.session, rig.campaign, payload(rig), "someone-else")
    assert exc.value.status_code == 403
    rig.new.workspace_id = "foreign"
    await rig.session.commit()
    with pytest.raises(HTTPException) as exc:
        await request(rig)
    assert exc.value.status_code == 400
    assert rig.campaign.config["account_ids"] == [rig.old.id]


async def test_multiaccount_transfer_only_moves_its_pinned_sources(rig):
    cfg = dict(rig.campaign.config)
    cfg["account_ids"] = [rig.old.id, rig.other.id]
    cfg["source_assignment"] = {rig.old.id: [rig.sources[0].id], rig.other.id: [s.id for s in rig.sources[1:]]}
    rig.campaign.config = cfg
    await rig.session.commit()
    await request(rig)
    assert rig.campaign.config["source_assignment"][rig.other.id] == [s.id for s in rig.sources[1:]]
    assert rig.campaign.config["source_assignment"][rig.new.id] == [rig.sources[0].id]
    assert len(rig.campaign.config["account_replacement"]["jobs"]) == 1


async def test_cancel_keeps_confirmed_membership_and_remaining_tasks_do_not_run(rig):
    await request(rig)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    await replacement.cancel_replacement(rig.session, rig.campaign, rig.user.id)
    rig.clock[0] += timedelta(hours=2)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    assert rig.sources[0].source_metadata["account_access"][rig.new.id]["joined"]
    assert rig.campaign.status == "paused"


async def test_patch_does_not_bypass_preparation_and_start_is_blocked(rig):
    with pytest.raises(HTTPException) as exc:
        await campaigns_api.update_campaign(
            rig.campaign.id, CommentPostsCampaignUpdate(account_ids=[rig.new.id]), rig.session, rig.workspace.id
        )
    assert exc.value.status_code == 409
    await request(rig)
    with pytest.raises(HTTPException) as exc:
        await campaigns_api.start_campaign(rig.campaign.id, rig.session, rig.workspace.id)
    assert exc.value.status_code == 409
    assert not rig.calls


def test_pacing_is_context_scoped_expires_and_does_not_grant_admission():
    at = datetime.now(UTC)
    account = SimpleNamespace(id="a", workspace_id="w", status="new", restriction_status="unknown", daily_limits={})
    pacing = {
        "replacement_id": "r",
        "campaign_id": "c",
        "account_id": "a",
        "workspace_id": "w",
        "actor_id": "owner",
        "min_interval_seconds": 60,
        "approved_at": at.isoformat(),
        "expires_at": (at + timedelta(hours=1)).isoformat(),
    }
    account.daily_limits = {"replacement_join_pacing": pacing}
    assert owner_join_limits(account, at) == (2, 1801)
    with replacement_pacing_scope(pacing):
        assert owner_join_limits(account, at) == (60, 60)
        assert owner_join_limits(account, at + timedelta(hours=2)) == (2, 1801)
    assert owner_join_limits(account, at) == (2, 1801)
    assert "owner_admission" not in account.daily_limits


async def test_replacement_preserves_rolling_comment_budget_and_spacing(rig, monkeypatch):
    from app.modules.campaigns.engine import _comment_account_send_wait_reason

    for i in range(5):
        rig.session.add(
            TelegramOperation(
                account_id=rig.old.id,
                method="SendMessageRequest",
                outcome="accepted",
                created_at=rig.clock[0] - timedelta(minutes=i + 1),
            )
        )
    await rig.session.commit()
    await request(rig)
    reason = await _comment_account_send_wait_reason(
        rig.session, rig.workspace.id, rig.new, rig.campaign.policy, rig.clock[0], campaign=rig.campaign
    )
    assert reason == "daily_limit"
    rig.clock[0] += timedelta(hours=25)
    from app.modules.campaigns import engine as campaign_engine

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return rig.clock[0]

    monkeypatch.setattr(campaign_engine, "datetime", Clock)
    # A refreshed account admission is independent of the inherited attempt window.
    rig.new.daily_limits = {
        **rig.new.daily_limits,
        "restriction_status": {"status": "clear", "checked_at": rig.clock[0].isoformat()},
    }
    await rig.session.commit()
    reason = await _comment_account_send_wait_reason(
        rig.session, rig.workspace.id, rig.new, rig.campaign.policy, rig.clock[0], campaign=rig.campaign
    )
    assert reason is None


async def test_stop_while_preparing_does_not_continue_joins(rig):
    await request(rig, resume=True)
    rig.campaign.status = "stopped"
    await rig.session.commit()
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls and rig.campaign.status == "stopped"


async def test_cancelled_incomplete_preparation_cannot_be_started(rig):
    await request(rig)
    await replacement.cancel_replacement(rig.session, rig.campaign, rig.user.id)
    with pytest.raises(HTTPException) as exc:
        await campaigns_api.start_campaign(rig.campaign.id, rig.session, rig.workspace.id)
    assert exc.value.status_code == 409


async def test_owner_attested_account_prepares_without_a_clear_restriction_check(rig):
    rig.new.daily_limits = {
        "restriction_status": {"status": "unknown"},
        "owner_admission": {
            "basis": "owner_attestation",
            "account_id": rig.new.id,
            "workspace_id": rig.workspace.id,
            "actor_id": rig.user.id,
            "approved_at": rig.clock[0].isoformat(),
            "active": True,
            "modules": ["auto_join", "neurocommenting"],
            "comment_attempt_limit": 5,
        },
    }
    state = await rig.session.get(TelegramSafetyState, rig.new.id)
    state.restriction_status = "unknown"
    state.restriction_checked_at = None
    state.reason = "owner_attested_admission"
    state.actor_id = rig.user.id
    await rig.session.commit()
    rows = await replacement.replacement_accounts(rig.session, rig.campaign)
    assert next(a for a in rows if a["id"] == rig.new.id)["ready"]
    await request(rig)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    assert rig.new.restriction_status == "unknown"


async def test_local_budget_refusal_waits_and_recovers_without_permanent_failure(rig, monkeypatch):
    from app.modules.telegram_gateway.contracts import TelegramGatewayError

    original = replacement.telegram_client_for_account
    refused = [True]

    @asynccontextmanager
    async def guarded(*args, **kwargs):
        if refused[0]:
            raise TelegramGatewayError("SAFETY_ATTEMPT_BUDGET", "Local pilot budget exhausted")
        async with original(*args, **kwargs) as client:
            yield client

    monkeypatch.setattr(replacement, "telegram_client_for_account", guarded)
    await request(rig, resume=True)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    r = rig.campaign.config["account_replacement"]
    assert r["status"] == "waiting_account" and r["jobs"][0]["status"] == "queued"
    assert r["jobs"][0]["local_wait_history"][0]["code"] == "SAFETY_ATTEMPT_BUDGET"
    assert not rig.calls
    refused[0] = False
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert not rig.calls
    rig.clock[0] += timedelta(seconds=2)
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert len(rig.calls) == 1
    for _ in range(2):
        rig.clock[0] += timedelta(seconds=301)
        await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert rig.campaign.config["account_replacement"]["status"] == "ready"
    assert rig.campaign.status == "pending"


async def test_local_budget_wait_uses_rolling_reset_and_ignores_operator_audits(rig):
    state = await rig.session.get(TelegramSafetyState, rig.new.id)
    state.outbound_attempt_limit = 1
    state.rpc_attempt_limit = 1
    at = rig.clock[0]
    rig.session.add(
        TelegramOperation(
            account_id=rig.new.id,
            method="JoinChannelRequest",
            outcome="accepted",
            outbound=True,
            created_at=at - timedelta(hours=2),
        )
    )
    rig.session.add(
        TelegramOperation(
            account_id=rig.new.id, method="operator.owner_attest", outcome="approved", outbound=False, created_at=at
        )
    )
    await rig.session.commit()
    for code in ["SAFETY_ATTEMPT_BUDGET", "SAFETY_RPC_BUDGET"]:
        assert await replacement.local_budget_retry_at(rig.session, rig.new.id, code, at) == at + timedelta(
            hours=22, seconds=1
        )


@pytest.mark.parametrize("denial", ["SOURCE_ACCESS_DENIED", "JOIN_APPROVAL_PENDING", "DISCUSSION_APPROVAL_REQUIRED"])
async def test_local_denial_drops_only_its_source_and_finishes_other_joins(rig, monkeypatch, denial):
    from app.modules.telegram_gateway.contracts import TelegramGatewayError

    original = replacement.telegram_client_for_account
    refused = [True]

    @asynccontextmanager
    async def guarded(*args, **kwargs):
        if refused[0]:
            raise TelegramGatewayError(denial, "Local source unavailable")
        async with original(*args, **kwargs) as client:
            yield client

    monkeypatch.setattr(replacement, "telegram_client_for_account", guarded)
    await request(rig, resume=True)
    denied_source = rig.campaign.config["source_ids"][0]
    await replacement.tick_replacement(rig.session, rig.campaign.id)
    r = rig.campaign.config["account_replacement"]
    assert r["status"] == "joining"
    assert denied_source not in rig.campaign.config["source_ids"]
    assert rig.campaign.config["blocked_sources"][denied_source]["error_code"] == denial
    assert r["rejected_jobs"][0]["source_id"] == denied_source
    refused[0] = False
    for _ in range(2):
        rig.clock[0] += timedelta(seconds=301)
        await replacement.tick_replacement(rig.session, rig.campaign.id)
    assert rig.campaign.config["account_replacement"]["status"] == "ready"
    assert rig.campaign.status == "pending"
    assert len(rig.calls) == 2
