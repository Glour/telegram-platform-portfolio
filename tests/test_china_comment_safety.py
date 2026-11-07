from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.modules.campaigns import engine
from app.modules.campaigns.models import CampaignAction, CampaignEvent, CampaignRun
from app.modules.comments.models import TelegramComment
from app.modules.sources.models import TelegramMessage
from app.workers import live_comment_listener as listener
from app.workers.live_comment_listener import _active_campaign_runs_for_source
from test_campaign_runner_foundation import _campaign, _comment_campaign_parts, _workspace, session  # noqa: F401


@pytest.mark.parametrize(
    "message, seconds",
    [
        ("A wait of 86400 seconds is required", 86400),
        ("Telegram flood wait: 86400 seconds.", 86400),
        ("FLOOD_WAIT 7200 seconds", 7200),
    ],
)
def test_flood_wait_duration(message, seconds):
    assert engine._telegram_flood_wait_seconds(message) == seconds


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["daily_limit", "send_interval", "wrong_assignment", "unknown_restriction"])
async def test_compatibility_live_comment_guards_prevent_generation(session, monkeypatch, reason):  # noqa: F811
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    if reason == "unknown_restriction":
        account.daily_limits = {}
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "delivery_mode": "generate_and_send",
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id]},
    }
    campaign.policy = {"daily_limit_per_account": 1, "min_send_interval_seconds": 3600}
    session.add(campaign)
    await session.flush()
    now = datetime.now(UTC)
    message = TelegramMessage(
        workspace_id=workspace.id,
        source_id=source.id,
        tg_message_id="new",
        text="A new relevant post with enough detail to discuss usefully.",
        published_at=now,
    )
    run = CampaignRun(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        status="running",
        started_at=now - timedelta(minutes=1),
        heartbeat_at=now,
        progress={},
        stop_conditions={},
        report={},
    )
    session.add_all([message, run])
    await session.flush()
    if reason != "wrong_assignment":
        previous = TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id="previous",
            text="Previous post",
            published_at=now - timedelta(minutes=20),
        )
        session.add(previous)
        await session.flush()
        session.add(
            TelegramComment(
                workspace_id=workspace.id,
                source_id=source.id,
                post_message_id=previous.id,
                campaign_id=campaign.id,
                telegram_account_id=account.id,
                text="Previously sent",
                status="sent",
                sent_at=now - timedelta(minutes=10),
            )
        )
        if reason == "send_interval":
            campaign.policy = {**campaign.policy, "daily_limit_per_account": 3}
    await session.commit()

    unexpected_generation = AsyncMock(side_effect=AssertionError("Generation must not happen before safety gates"))
    monkeypatch.setattr(engine, "generate_comment_text", unexpected_generation)
    result = await engine.process_live_comment_message(
        session,
        workspace.id,
        campaign,
        run,
        source,
        message,
        account_id="other-account" if reason == "wrong_assignment" else account.id,
    )
    unexpected_generation.assert_not_awaited()
    assert list((await session.scalars(select(CampaignAction))).all()) == []
    if reason == "unknown_restriction":
        assert result.status == "failed"
        assert result.error_code == "ACCOUNT_SAFETY_BLOCKED"
        assert "неизвестен" in result.error_message
        return
    assert result.status == "running"
    events = list((await session.scalars(select(CampaignEvent))).all())
    if reason == "wrong_assignment":
        assert not events
    else:
        assert events[-1].payload["reason"] == reason


@pytest.mark.asyncio
async def test_compatibility_live_matcher_only_routes_pinned_account(session):  # noqa: F811
    _, workspace = await _workspace(session)
    source, account, prompt = await _comment_campaign_parts(session, workspace)
    campaign = _campaign(workspace, source, account, prompt, mode="generate_and_send")
    campaign.status = "running"
    campaign.config = {
        **campaign.config,
        "post_selection_mode": "new_posts",
        "account_ids": [account.id, "another-account"],
        "source_assignment_mode": "manual_pinned",
        "source_assignment": {account.id: [source.id], "another-account": []},
    }
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
    assert len(await _active_campaign_runs_for_source(session, workspace.id, account.id, source.id)) == 1
    assert await _active_campaign_runs_for_source(session, workspace.id, "another-account", source.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["main", "_run_account_listener"])
async def test_persistent_listener_is_disabled_before_any_generation(monkeypatch, entrypoint):
    generation = AsyncMock(side_effect=AssertionError("Disabled listener must not generate"))
    monkeypatch.setattr(engine, "generate_comment_text", generation)
    with pytest.raises(RuntimeError, match="Persistent Telegram listener is disabled; use the guarded polling engine"):
        if entrypoint == "main":
            await listener.main()
        else:
            await listener._run_account_listener(listener.LiveAccountBinding(account_id="account", sources=[]))
    generation.assert_not_awaited()
