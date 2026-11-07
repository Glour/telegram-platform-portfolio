from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.modules.account_warmup import service as warmup
from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep
from app.modules.telegram_accounts.models import TelegramAccount
from test_campaign_runner_foundation import _workspace, session  # noqa: F401, F811


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, False),
        ({"activities": []}, True),
        ({"activities": ["random_pauses"]}, True),
        ({"activities": ["random_pauses"], "force_profile_check": True}, False),
        ({"activities": ["random_pauses", "profile_check"]}, False),
        ({"sequence_sessions": [{"actions": ["random_pauses", "view_posts"]}]}, False),
        ({"stage_sessions": [{"actions": ["read_messages"]}]}, False),
    ],
)
def test_rest_eligibility_requires_proven_zero_rpc_actions(config, expected):
    assert warmup._warmup_plan_zero_rpc(WarmupPlan(config=config, policy={})) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [None, "unknown", "limited"])
@pytest.mark.parametrize("mode", ["preset", "empty_activities", "pauses", "sequence", "stages", "rest_only"])
@pytest.mark.parametrize("initial_status", ["warming", "quarantined"])
async def test_rest_schedules_and_completes_without_rpc_or_admission(
    session,  # noqa: F811
    monkeypatch,
    restriction,
    mode,
    initial_status,
):
    user, workspace = await _workspace(session)
    limits = {
        "flood_wait_unknown": True,
        "source_fetch_wait_until": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    if restriction:
        limits["restriction_status"] = {"status": restriction}
    account = TelegramAccount(
        workspace_id=workspace.id,
        status=initial_status,
        import_type="session",
        proxy_required=True,
        health_score=80,
        warmup_score=7,
        daily_limits=limits,
    )
    session.add(account)
    await session.flush()
    config = {"account_ids": [account.id], "max_steps_total": 1, "daily_action_limit": 1}
    policy = {}
    if mode == "preset":
        config["preset"] = "new_account_rest"
    elif mode == "empty_activities":
        config["activities"] = []
    elif mode == "pauses":
        config["activities"] = ["random_pauses"]
    elif mode == "sequence":
        config["sequence_sessions"] = [{"stage": "rest", "actions": ["random_pauses"], "action_limit": 1}]
    elif mode == "stages":
        config["stage_sessions"] = [{"stage": "rest", "actions": ["random_pauses"], "daily_action_limit": 1}]
    else:
        policy["rest_only"] = True
        config["activities"] = ["profile_check", "view_posts"]
    plan = WarmupPlan(
        workspace_id=workspace.id,
        created_by=user.id,
        name="Rest",
        duration_days=1,
        config=config,
        policy=policy,
    )
    session.add(plan)
    await session.flush()
    rpc = AsyncMock(side_effect=AssertionError("Calendar rest must never call Telegram"))
    for name in (
        "check_real_account",
        "ensure_source_access_with_account",
        "fetch_posts_with_account",
        "fetch_chat_messages_with_account",
        "react_to_latest_post_with_account",
        "send_chat_reply_with_account",
    ):
        monkeypatch.setattr(warmup, name, rpc)

    assert warmup._warmup_module_for_plan(plan) == "warmup_rest"
    runs = await warmup.start_warmup_plan(session, workspace.id, plan)
    await session.flush()
    steps = list((await session.scalars(select(WarmupStep).where(WarmupStep.warmup_run_id == runs[0].id))).all())
    assert runs[0].status == "running"
    assert steps
    assert all(step.action_type == "random_pauses" for step in steps)
    for step in steps:
        step.planned_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    for _ in steps:
        await warmup.tick_active_warmup_runs(session, workspace.id)

    assert runs[0].status == "completed"
    assert all(step.status == "succeeded" for step in steps)
    assert account.status == "quarantined"
    assert account.restriction_status == restriction
    assert account.warmup_score == 7
    assert account.daily_limits == limits
    rpc.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["complete", "reconcile"])
@pytest.mark.parametrize("restriction", [None, "limited", "clear_stale", "clear_fresh"])
async def test_completion_and_reconcile_never_activate_accounts(session, restriction, path):  # noqa: F811
    user, workspace = await _workspace(session)
    now = datetime.now(UTC)
    limits = {}
    if restriction:
        limits["restriction_status"] = {
            "status": "clear" if restriction.startswith("clear") else restriction,
            "checked_at": (now - timedelta(hours=25 if restriction == "clear_stale" else 1)).isoformat(),
            "evidence_source": "operator_admission",
        }
    account = TelegramAccount(
        workspace_id=workspace.id,
        status="warming",
        import_type="session",
        proxy_required=False,
        warmup_status="running",
        warmup_score=7,
        daily_limits=limits,
    )
    plan = WarmupPlan(
        workspace_id=workspace.id,
        created_by=user.id,
        name="Read completion",
        duration_days=1,
        config={"activities": ["view_posts"]},
        policy={},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(
        workspace_id=workspace.id,
        warmup_plan_id=plan.id,
        telegram_account_id=account.id,
        status="completed" if path == "reconcile" else "running",
        score_current=90,
        completed_at=now if path == "reconcile" else None,
    )
    session.add(run)
    await session.flush()
    if path == "complete":
        await warmup._complete_run_if_finished(session, plan, run, account, now)
    else:
        await warmup.reconcile_completed_warmup_accounts(session, workspace.id)

    assert account.warmup_status == "completed"
    assert account.warmup_score == 90
    assert account.status == ("warming" if restriction == "clear_fresh" else "quarantined")
    assert account.daily_limits == limits


@pytest.mark.asyncio
async def test_rest_plan_cannot_execute_legacy_rpc_step(session, monkeypatch):  # noqa: F811
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id, status="quarantined", import_type="session", proxy_required=True, daily_limits={}
    )
    plan = WarmupPlan(
        workspace_id=workspace.id,
        created_by=user.id,
        name="Rest",
        duration_days=1,
        config={"activities": ["random_pauses"]},
        policy={},
    )
    session.add_all([account, plan])
    await session.flush()
    run = WarmupRun(workspace_id=workspace.id, warmup_plan_id=plan.id, telegram_account_id=account.id, status="running")
    session.add(run)
    await session.flush()
    step = WarmupStep(
        warmup_run_id=run.id,
        telegram_account_id=account.id,
        action_type="profile_check",
        status="queued",
        planned_at=datetime.now(UTC),
    )
    session.add(step)
    await session.flush()
    perform = AsyncMock(side_effect=AssertionError("Legacy RPC step must not run under rest eligibility"))
    monkeypatch.setattr(warmup, "_perform_action", perform)
    await warmup.tick_active_warmup_runs(session, workspace.id)
    assert run.status == "failed"
    assert step.error_code == "ZERO_RPC_PLAN_ACTION_BLOCKED"
    perform.assert_not_awaited()
