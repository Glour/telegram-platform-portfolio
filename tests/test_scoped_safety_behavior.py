from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.account_warmup import service as warmup
from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep
from app.modules.campaigns import engine
from app.modules.campaigns.models import Campaign
from app.modules.comments.models import TelegramComment
from app.modules.sources.models import TelegramMessage
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from test_campaign_runner_foundation import _comment_campaign_parts, _workspace, session  # noqa: F401


def _account(**kwargs):
    return TelegramAccount(
        workspace_id="workspace",
        status="active",
        health_score=90,
        daily_limits={"restriction_status": {"status": "clear", "checked_at": datetime.now(UTC).isoformat()}},
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["comment_posts", "mass_reactions", "neuro_chat", "neuro_dialog"])
def test_polling_replaces_listener(kind):
    campaign = Campaign(type=kind, config={"post_selection_mode": "new_posts"})
    assert engine._campaign_uses_live_listener(campaign) is False


def test_polling_cutover_does_not_replay_old_posts():
    cutover = datetime.now(UTC)
    old = TelegramMessage(published_at=cutover - timedelta(seconds=1))
    fresh = TelegramMessage(published_at=cutover + timedelta(seconds=1))
    assert engine._messages_after_polling_cutover([old, fresh], {"polling_cutover_at": cutover.isoformat()}) == [fresh]
    assert engine._messages_after_polling_cutover([old, fresh], {}) == [old, fresh]


async def test_direct_neuro_reply_preserves_business_intent(monkeypatch):
    monkeypatch.setattr(engine, "real_telegram_enabled", lambda: True)
    send = AsyncMock(return_value=SimpleNamespace(tg_message_id="123", raw={}))
    monkeypatch.setattr(engine, "send_chat_reply_with_account", send)
    result = await engine._deliver_neuro_reply_action(
        session=Mock(),
        account=_account(),
        peer_ref="peer",
        reply_text="reply",
        reply_to_tg_message_id=None,
        business_intent="neuro:campaign:peer:incoming-42",
    )
    assert result.status == "succeeded"
    assert send.await_args.kwargs["reply_to_tg_message_id"] is None
    assert send.await_args.kwargs["business_intent"] == "neuro:campaign:peer:incoming-42"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("A wait of 86400 seconds is required", 86400),
        ("FLOOD_WAIT_7200", 7200),
        ("FLOOD_WAIT", None),
        ("FLOOD_WAIT_0", 0),
        ("FLOOD_WAIT_-30", None),
        ("A wait of 1.5 seconds is required", None),
    ],
)
def test_flood_duration_has_no_invented_fallback(message, expected):
    assert engine._telegram_flood_wait_seconds(message) == expected


@pytest.mark.parametrize("seconds", [0, 1, 59, 300, 86400])
@pytest.mark.parametrize("message", ["Telegram operation stopped", "FLOOD_WAIT_99999"])
def test_structured_flood_duration_is_exact_and_preferred(seconds, message):
    exc = TelegramGatewayError("FLOOD_WAIT", message, {"seconds": seconds})
    assert engine._telegram_gateway_flood_wait_seconds(exc) == seconds
    assert warmup._warmup_flood_wait_seconds(exc) == seconds
    assert f"на {seconds} сек." in engine._friendly_gateway_error(exc)


@pytest.mark.parametrize("invalid", [None, -1, True, False, "30", 1.5])
def test_invalid_structured_flood_duration_falls_back_to_parser(invalid):
    exc = TelegramGatewayError("FLOOD_WAIT", "FLOOD_WAIT_0", {"seconds": invalid})
    assert engine._telegram_gateway_flood_wait_seconds(exc) == 0
    assert warmup._warmup_flood_wait_seconds(exc) == 0
    exc = TelegramGatewayError("FLOOD_WAIT", "Telegram operation stopped", {"seconds": invalid})
    assert engine._telegram_gateway_flood_wait_seconds(exc) is None
    assert warmup._warmup_flood_wait_seconds(exc) is None


@pytest.mark.parametrize("module", [engine, warmup])
@pytest.mark.parametrize("seconds", [0, 1, 59, 86400])
def test_structured_flood_receipt_deadline_without_minimum(module, seconds):
    account = _account()
    exc = TelegramGatewayError("FLOOD_WAIT", "Telegram operation stopped", {"seconds": seconds})
    stale = datetime.now(UTC) - timedelta(days=2)
    if module is engine:
        module._apply_gateway_error_account_risk(Mock(), account, exc, stale)
    else:
        module._apply_warmup_gateway_error_risk(account, exc, stale)
    received = datetime.fromisoformat(account.daily_limits["last_flood_wait_at"])
    assert received > stale
    assert account.daily_limits["last_flood_wait_seconds"] == seconds
    assert datetime.fromisoformat(account.daily_limits["source_fetch_wait_until"]) == received + timedelta(
        seconds=seconds + 15,
    )
    assert account.status == "active"
    assert not account.daily_limits.get("flood_wait_unknown")


@pytest.mark.parametrize(
    "restriction",
    [
        {},
        {"status": "unknown"},
        {"status": "suspected"},
        {"status": "limited"},
        {"status": "clear"},
        {"status": "clear", "checked_at": "not-a-date"},
    ],
)
def test_comment_account_requires_explicit_dated_clear(restriction):
    account = _account()
    account.daily_limits = {"restriction_status": restriction}
    assert not engine._account_can_send_comments(account)


@pytest.mark.parametrize("hours,expected", [(-1, False), (0.1, True), (23, True), (25, False)])
def test_comment_clear_baseline_must_be_fresh_and_not_future(hours, expected):
    account = _account()
    account.daily_limits = {
        "restriction_status": {
            "status": "clear",
            "checked_at": (datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
        }
    }
    assert engine._account_can_send_comments(account) is expected


@pytest.mark.parametrize("module", [engine, warmup])
@pytest.mark.parametrize("unknown", [False, True])
def test_flood_uses_receipt_time_and_unknown_fails_closed(module, unknown):
    account = _account()
    stale = datetime.now(UTC) - timedelta(days=2)
    receipt = datetime.now(UTC)
    exc = TelegramGatewayError("FLOOD_WAIT", "FLOOD_WAIT" if unknown else "FLOOD_WAIT_86400")
    if module is engine:
        module._apply_gateway_error_account_risk(Mock(), account, exc, stale)
    else:
        module._apply_warmup_gateway_error_risk(account, exc, stale)
    recorded = datetime.fromisoformat(account.daily_limits["last_flood_wait_at"])
    assert recorded >= receipt
    if unknown:
        assert account.status == "quarantined"
        assert account.daily_limits["flood_wait_unknown"] is True
        assert not engine._account_can_send_comments(account)
        assert warmup._warmup_flood_wait_active(account, receipt + timedelta(days=7))
        assert "source_fetch_wait_until" not in account.daily_limits
    else:
        deadline = datetime.fromisoformat(account.daily_limits["source_fetch_wait_until"])
        assert deadline == recorded + timedelta(seconds=86415)
        assert warmup._warmup_flood_wait_active(account, receipt)


@pytest.mark.parametrize("status", ["limited", "blocked", "frozen"])
def test_write_denied_does_not_downgrade_confirmed_restriction(status):
    account = _account()
    original = {"status": status, "source": "spambot", "raw_text": "Confirmed evidence"}
    account.daily_limits = {"restriction_status": original}
    engine._apply_gateway_error_account_risk(
        Mock(),
        account,
        TelegramGatewayError("UserBannedInChannelError", "denied"),
        datetime.now(UTC),
        comment_send=True,
    )
    assert account.daily_limits["restriction_status"] == original


def test_join_attempt_budget_survives_midnight_and_does_not_shorten_flood():
    now = datetime(2026, 10, 3, 0, 5, tzinfo=UTC)
    account = _account()
    deadline = now + timedelta(days=2)
    account.daily_limits = {
        "auto_join_daily_date": "2026-10-02",
        "auto_join_daily_attempts": 3,
        "last_auto_join_attempt_at": (now - timedelta(minutes=10)).isoformat(),
        "auto_join_wait_until": deadline.isoformat(),
    }
    assert engine._account_auto_join_daily_attempts(account, now) == 3
    engine._record_auto_join_attempt(Campaign(config={}), account, now, 1800)
    assert engine._account_auto_join_daily_attempts(account, now) == 4
    assert engine._account_auto_join_wait_until(account) == deadline
    assert engine._account_auto_join_daily_attempts(account, now + timedelta(hours=24)) == 0


@pytest.mark.asyncio
async def test_warmup_does_not_execute_during_flood_pause(monkeypatch):
    account = _account()
    account.daily_limits = {"source_fetch_wait_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    perform = AsyncMock(side_effect=AssertionError("No Telegram action during pause"))
    monkeypatch.setattr(warmup, "_perform_action", perform)
    step = Mock(status="queued")
    await warmup._execute_step(
        AsyncMock(),
        WarmupPlan(config={}, policy={}),
        Mock(),
        step,
        account,
        datetime.now(UTC),
    )
    assert step.status == "queued"
    perform.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_budget_counts_recent_previous_day_and_failed_attempts(session):  # noqa: F811
    _, workspace = await _workspace(session)
    source, account, _ = await _comment_campaign_parts(session, workspace)
    now = datetime.now(UTC)
    account.daily_limits = {"restriction_status": {"status": "clear", "checked_at": now.isoformat()}}
    message = TelegramMessage(workspace_id=workspace.id, source_id=source.id, tg_message_id="budget", text="Post")
    session.add(message)
    await session.flush()
    session.add(
        TelegramComment(
            workspace_id=workspace.id,
            source_id=source.id,
            post_message_id=message.id,
            telegram_account_id=account.id,
            text="Previous",
            status="sent",
            sent_at=now - timedelta(hours=23),
        )
    )
    await session.commit()
    assert await engine._sent_comments_today_by_account(session, workspace.id, [account.id], now) == {account.id: 1}
    engine._record_account_attempt(account, "comment_send_attempts_24h", now - timedelta(minutes=10))
    engine._record_account_attempt(account, "comment_send_attempts_24h", now)
    await session.flush()
    assert (
        await engine._comment_account_send_wait_reason(
            session,
            workspace.id,
            account,
            {"daily_limit_per_account": 3},
            now,
        )
        == "daily_limit"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [0, 1, 86400])
async def test_preparation_records_failed_join_before_io_and_keeps_flood_deadline(monkeypatch, seconds):
    account = _account()
    account.id = "account"
    campaign = Campaign(config={}, policy={})
    source = Mock(id="source", source_metadata={}, comments_enabled=False, title="Source")
    monkeypatch.setattr(engine, "real_telegram_enabled", lambda: True)

    async def fail(*args, **kwargs):
        assert engine._account_auto_join_daily_attempts(account, datetime.now(UTC)) == 1
        raise TelegramGatewayError("FLOOD_WAIT", "Telegram operation stopped", {"seconds": seconds})

    monkeypatch.setattr(engine, "fetch_real_posts", fail)
    result = await engine._try_prepare_source_comments(
        session=Mock(),
        workspace_id="workspace",
        campaign=campaign,
        run=None,
        source=source,
        account=account,
        latest_limit=1,
        auto_join_sources=True,
        auto_join_discussions=True,
        auto_join_attempts=0,
        auto_join_max_per_tick=1,
        auto_join_wait_until=None,
        daily_attempts=0,
        daily_limit=6,
        now=datetime.now(UTC) - timedelta(days=2),
    )
    assert result.attempted
    received = datetime.fromisoformat(account.daily_limits["last_flood_wait_at"])
    # The ordinary join interval can be longer than short/zero Flood waits.
    assert engine._account_source_fetch_wait_until(account) == received + timedelta(seconds=seconds + 15)
    assert engine._account_auto_join_wait_until(account) >= received + timedelta(seconds=seconds + 15)
    assert account.daily_limits["last_flood_wait_seconds"] == seconds
    assert account.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "succeeded", "running"])
async def test_warmup_rolling_attempt_limit_across_runs(session, monkeypatch, status):  # noqa: F811
    user, workspace = await _workspace(session)
    _, account, _ = await _comment_campaign_parts(session, workspace)
    now = datetime.now(UTC)
    plan = WarmupPlan(
        workspace_id=workspace.id,
        created_by=user.id,
        name="Budget",
        duration_days=2,
        config={"daily_action_limit": 1},
        policy={},
    )
    session.add(plan)
    await session.flush()
    previous = WarmupRun(workspace_id=workspace.id, warmup_plan_id=plan.id, telegram_account_id=account.id)
    current = WarmupRun(workspace_id=workspace.id, warmup_plan_id=plan.id, telegram_account_id=account.id)
    session.add_all([previous, current])
    await session.flush()
    attempt = WarmupStep(
        warmup_run_id=previous.id,
        telegram_account_id=account.id,
        action_type="view_posts",
        status=status,
        planned_at=now - timedelta(hours=23),
        executed_at=now - timedelta(hours=23),
    )
    queued = WarmupStep(
        warmup_run_id=current.id,
        telegram_account_id=account.id,
        action_type="read_messages",
        status="queued",
        planned_at=now,
    )
    session.add_all([attempt, queued])
    await session.flush()
    perform = AsyncMock(side_effect=AssertionError("Budget must stop the next action"))
    monkeypatch.setattr(warmup, "_perform_action", perform)
    await warmup._execute_step(session, plan, current, queued, account, now)
    assert queued.status == "queued"
    assert queued.executed_at is None
    perform.assert_not_awaited()
    attempt.executed_at = now - timedelta(hours=25)
    await session.flush()
    assert not await warmup._warmup_attempt_budget_exhausted(session, plan, account, now)


@pytest.mark.asyncio
async def test_rebind_never_performs_followup_rpc(monkeypatch):
    account = _account()
    monkeypatch.setattr(
        engine,
        "fetch_posts_with_account",
        AsyncMock(
            side_effect=TelegramGatewayError("FLOOD_WAIT", "FLOOD_WAIT_3600"),
        ),
    )
    result = await engine._retry_comment_after_post_rebind(
        session=Mock(),
        workspace_id="workspace",
        source=Mock(username="source", url=None, tg_id=None),
        message=Mock(),
        account=account,
        comment_text="Comment",
        original_error=TelegramGatewayError("MsgIdInvalidError", "message id invalid"),
    )
    assert result is None
    engine.fetch_posts_with_account.assert_not_awaited()
