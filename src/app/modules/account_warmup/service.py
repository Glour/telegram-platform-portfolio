from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.modules.telegram_gateway.service import (
    TelegramGatewayError,
    check_real_account,
    ensure_source_access_with_account,
    fetch_chat_messages_with_account,
    fetch_posts_with_account,
    react_to_latest_post_with_account,
    send_chat_reply_with_account,
    source_ref_from_fields,
)
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep
from app.modules.campaigns.models import Campaign, CampaignRun
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility

DEFAULT_ACTIVITIES = ["peer_message_exchange", "view_posts", "read_messages", "random_pauses"]
SOURCE_ACTIONS = {"subscribe_channels", "view_posts", "read_messages", "set_reactions"}
WARMUP_PRESETS: dict[str, dict[str, Any]] = {
    "primary_warmup": {
        "duration_days": 2,
        "config": {
            "intensity": "primary_safe",
            "daily_action_limit": 10,
            "active_hours_from": "13:00",
            "active_hours_to": "15:00",
            "timezone": "Europe/Moscow",
            "random_breaks": True,
            "auto_adapt_by_age": True,
            "safety_limits_enabled": True,
            "actions_per_hour_limit": 5,
            "daily_join_limit": 0,
            "daily_message_limit": 0,
            "progressive_ramp_enabled": True,
            "activities": ["view_posts", "read_messages", "random_pauses"],
            "source_strategy": "warmup_pool",
            "require_warmup_sources": True,
            "min_warmup_sources": 5,
            "first_rest_hours": 0,
            "force_profile_check": False,
            "sequence_sessions": [
                {
                    "stage": "first_session",
                    "start_after_minutes": 0,
                    "duration_minutes": 30,
                    "actions": ["view_posts", "read_messages", "random_pauses"],
                    "action_limit": 2,
                    "actions_per_hour": 5,
                },
                {
                    "stage": "first_rest",
                    "start_after_minutes": 30,
                    "duration_minutes": 720,
                    "actions": ["random_pauses"],
                    "action_limit": 1,
                },
                {
                    "stage": "second_session",
                    "start_after_minutes": 750,
                    "duration_minutes": 30,
                    "actions": ["view_posts", "read_messages", "random_pauses"],
                    "action_limit": 5,
                    "actions_per_hour": 10,
                },
                {
                    "stage": "second_rest",
                    "start_after_minutes": 780,
                    "duration_minutes": 720,
                    "actions": ["random_pauses"],
                    "action_limit": 1,
                },
            ],
            "disabled_actions_before_day": {"subscribe_channels": 99, "set_reactions": 99, "peer_message_exchange": 99},
            "max_action_counts": {"subscribe_channels": 0, "set_reactions": 0, "peer_message_exchange": 0},
        },
        "policy": {
            "guided": True,
            "primary_warmup": True,
            "warmup_pool_required": True,
            "random_pauses": True,
            "read_only": True,
        },
    },
    "new_account_rest": {
        "duration_days": 2,
        "config": {
            "intensity": "rest",
            "daily_action_limit": 1,
            "daily_action_limits": [1, 1],
            "active_hours_from": "11:00",
            "active_hours_to": "20:00",
            "activities": ["random_pauses"],
            "first_rest_hours": 24,
            "min_interval_minutes": 720,
            "batch_start_spread_minutes": 60,
            "max_steps_total": 1,
            "force_profile_check": False,
        },
        "policy": {"guided": True, "rest_only": True, "zero_telegram_actions": True, "random_pauses": True},
    },
    "new_account_validation": {
        "duration_days": 1,
        "config": {
            "intensity": "validation",
            "daily_action_limit": 1,
            "daily_action_limits": [1],
            "active_hours_from": "11:00",
            "active_hours_to": "20:00",
            "activities": ["profile_check", "random_pauses"],
            "first_rest_hours": 24,
            "min_interval_minutes": 360,
            "batch_start_spread_minutes": 45,
            "max_steps_total": 1,
        },
        "policy": {"guided": True, "validation_only": True, "random_pauses": True},
    },
    "new_account_safe": {
        "duration_days": 7,
        "config": {
            "intensity": "new_safe",
            "daily_action_limit": 2,
            "daily_action_limits": [1, 1, 1, 1, 1, 2, 2],
            "active_hours_from": "10:30",
            "active_hours_to": "22:30",
            "activities": ["random_pauses", "view_posts", "read_messages"],
            "first_rest_hours": 72,
            "min_interval_minutes": 720,
            "batch_start_spread_minutes": 37,
            "max_steps_total": 10,
            "force_profile_check": False,
            "action_weights": {"random_pauses": 8, "view_posts": 1, "read_messages": 1},
            "disabled_actions_before_day": {
                "view_posts": 5,
                "read_messages": 5,
                "set_reactions": 8,
                "subscribe_channels": 8,
                "peer_message_exchange": 8,
            },
        },
        "policy": {"guided": True, "new_account_safe": True, "random_pauses": True},
    },
    "incubator_safe_v2": {
        "duration_days": 14,
        "config": {
            "intensity": "incubator_v2",
            "daily_action_limit": 2,
            "daily_action_limits": [1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            "active_hours_from": "11:00",
            "active_hours_to": "21:30",
            "activities": ["random_pauses", "view_posts", "read_messages"],
            "first_rest_hours": 24,
            "min_interval_minutes": 720,
            "batch_start_spread_minutes": 67,
            "max_steps_total": 30,
            "force_profile_check": False,
            "stage_sessions": [
                {
                    "stage": "rest",
                    "start_day": 1,
                    "end_day": 1,
                    "actions": ["random_pauses"],
                    "daily_action_limit": 1,
                    "session_duration_minutes": 30,
                    "actions_per_session": 1,
                    "min_interval_minutes": 720,
                },
                {
                    "stage": "first_check",
                    "start_day": 2,
                    "end_day": 2,
                    "actions": ["profile_check", "random_pauses"],
                    "daily_action_limit": 1,
                    "session_duration_minutes": 30,
                    "actions_per_session": 1,
                    "min_interval_minutes": 720,
                },
                {
                    "stage": "read_only",
                    "start_day": 3,
                    "end_day": 5,
                    "actions": ["view_posts", "read_messages", "random_pauses"],
                    "daily_action_limit": 1,
                    "session_duration_minutes": 45,
                    "actions_per_session": 1,
                    "min_interval_minutes": 720,
                },
                {
                    "stage": "soft_idle",
                    "start_day": 6,
                    "end_day": 9,
                    "actions": ["view_posts", "read_messages", "random_pauses"],
                    "daily_action_limit": 2,
                    "session_duration_minutes": 60,
                    "actions_per_session": 2,
                    "min_interval_minutes": 360,
                },
                {
                    "stage": "work_ready",
                    "start_day": 10,
                    "end_day": 14,
                    "actions": ["view_posts", "read_messages", "random_pauses"],
                    "daily_action_limit": 2,
                    "session_duration_minutes": 75,
                    "actions_per_session": 2,
                    "min_interval_minutes": 360,
                },
            ],
            "disabled_actions_before_day": {"subscribe_channels": 99, "set_reactions": 99, "peer_message_exchange": 99},
            "max_action_counts": {
                "profile_check": 1,
                "subscribe_channels": 0,
                "set_reactions": 0,
                "peer_message_exchange": 0,
            },
        },
        "policy": {
            "guided": True,
            "incubator_safe_v2": True,
            "stage_based": True,
            "read_only": True,
            "random_pauses": True,
        },
    },
    "controlled_source_join": {
        "duration_days": 14,
        "config": {
            "intensity": "join_controlled",
            "daily_action_limit": 2,
            "daily_action_limits": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2],
            "active_hours_from": "11:00",
            "active_hours_to": "21:30",
            "activities": ["random_pauses", "view_posts", "subscribe_channels"],
            "first_rest_hours": 96,
            "min_interval_minutes": 720,
            "batch_start_spread_minutes": 53,
            "max_steps_total": 16,
            "force_profile_check": False,
            "action_weights": {"random_pauses": 10, "view_posts": 2, "subscribe_channels": 1},
            "disabled_actions_before_day": {
                "view_posts": 6,
                "subscribe_channels": 8,
                "read_messages": 15,
                "set_reactions": 15,
                "peer_message_exchange": 15,
            },
            "max_action_counts": {"subscribe_channels": 3},
        },
        "policy": {
            "guided": True,
            "controlled_source_join": True,
            "random_pauses": True,
            "join_discussion": False,
        },
    },
    "balanced_human": {
        "duration_days": 7,
        "config": {
            "intensity": "normal",
            "daily_action_limit": 4,
            "daily_action_limits": [1, 2, 2, 3, 3, 4, 4],
            "active_hours_from": "09:30",
            "active_hours_to": "23:00",
            "activities": ["view_posts", "read_messages", "random_pauses"],
            "first_rest_hours": 24,
            "min_interval_minutes": 360,
            "batch_start_spread_minutes": 29,
            "max_steps_total": 24,
            "action_weights": {
                "view_posts": 4,
                "read_messages": 5,
                "random_pauses": 4,
            },
            "disabled_actions_before_day": {"set_reactions": 3, "peer_message_exchange": 3, "subscribe_channels": 5},
        },
        "policy": {"guided": True, "balanced_human": True, "random_pauses": True},
    },
    "trusted_account_growth": {
        "duration_days": 10,
        "config": {
            "intensity": "growth",
            "daily_action_limit": 6,
            "daily_action_limits": [2, 2, 3, 3, 4, 4, 5, 5, 6, 6],
            "active_hours_from": "08:30",
            "active_hours_to": "23:30",
            "activities": ["view_posts", "read_messages", "random_pauses"],
            "first_rest_hours": 48,
            "min_interval_minutes": 240,
            "batch_start_spread_minutes": 23,
            "max_steps_total": 42,
            "action_weights": {
                "view_posts": 4,
                "read_messages": 5,
                "random_pauses": 3,
            },
            "disabled_actions_before_day": {"subscribe_channels": 99, "set_reactions": 99, "peer_message_exchange": 99},
        },
        "policy": {"guided": True, "trusted_growth": True, "random_pauses": True},
    },
}
ACTION_LABELS = {
    "profile_check": "Проверка профиля",
    "profile_checklist": "Чек-лист профиля",
    "manual_reading_window": "Окно ручного чтения",
    "manual_draft_message": "Черновик ручного сообщения",
    "rest_window": "Окно отдыха",
    "subscribe_channels": "Подписка на канал",
    "view_posts": "Просмотр постов",
    "read_messages": "Чтение сообщений",
    "peer_message_exchange": "Обмен сообщениями",
    "set_reactions": "Постановка реакции",
    "random_pauses": "Пауза между действиями",
}
WARMUP_PEER_MESSAGES = [
    "Привет, как дела?",
    "Да нормально, потихоньку. У тебя как?",
    "Все окей, рабочий день идет.",
    "Понял, тогда позже еще спишемся.",
]


async def start_warmup_plan(session: AsyncSession, workspace_id: str, plan: WarmupPlan) -> list[WarmupRun]:
    accounts = await _plan_accounts(session, workspace_id, plan)
    if not accounts:
        raise ValueError("В плане прогрева нет выбранных аккаунтов")
    source_error = await _warmup_source_pool_error(session, workspace_id, plan)
    if source_error:
        raise ValueError(source_error)

    now = datetime.now(UTC)
    manual_drafts_only = (plan.policy or {}).get("mode") == "manual_drafts_only"
    run_status = "scheduled" if manual_drafts_only else "running"
    plan.status = run_status
    runs: list[WarmupRun] = []
    preset_violations = [_warmup_preset_violation(account, plan, now) for account in accounts]
    if preset_violations and all(preset_violations):
        raise ValueError(str(preset_violations[0]))
    for account_index, account in enumerate(accounts):
        preset_violation = preset_violations[account_index]
        if preset_violation:
            failed_run = WarmupRun(
                workspace_id=workspace_id,
                telegram_account_id=account.id,
                warmup_plan_id=plan.id,
                status="failed",
                current_day=1,
                score_before=account.warmup_score,
                score_current=account.warmup_score,
                completed_at=now,
                stop_reason=preset_violation,
            )
            session.add(failed_run)
            account.warmup_status = "failed"
            runs.append(failed_run)
            continue
        eligibility = (
            None
            if _warmup_plan_zero_rpc(plan)
            else get_module_eligibility(
                account,
                _warmup_module_for_plan(plan),
            )
        )
        if eligibility and eligibility.status == "blocked":
            failed_run = WarmupRun(
                workspace_id=workspace_id,
                telegram_account_id=account.id,
                warmup_plan_id=plan.id,
                status="failed",
                current_day=1,
                score_before=account.warmup_score,
                score_current=account.warmup_score,
                completed_at=now,
                stop_reason=eligibility.message or eligibility.reason or "Аккаунт не подходит для прогрева",
            )
            session.add(failed_run)
            account.warmup_status = "failed"
            runs.append(failed_run)
            continue
        existing = await session.execute(
            select(WarmupRun).where(
                WarmupRun.workspace_id == workspace_id,
                WarmupRun.warmup_plan_id == plan.id,
                WarmupRun.telegram_account_id == account.id,
                WarmupRun.status.in_(["scheduled", "running"]),
            )
        )
        run = existing.scalar_one_or_none()
        if not run:
            run = WarmupRun(
                workspace_id=workspace_id,
                telegram_account_id=account.id,
                warmup_plan_id=plan.id,
                status=run_status,
                current_day=1,
                score_before=account.warmup_score,
                score_current=account.warmup_score,
                started_at=None if manual_drafts_only else now,
            )
            session.add(run)
            await session.flush()
            session.add_all(
                _build_steps(
                    plan,
                    run,
                    account.id,
                    now,
                    account_index=account_index,
                    account_count=len(accounts),
                )
            )
        else:
            run.status = run_status
            if not manual_drafts_only:
                run.started_at = run.started_at or now
            run.stop_reason = None
            await _replace_queued_steps(session, plan, run, account.id, now, account_index, len(accounts))
        _mark_incubator_account_for_plan(account, plan, now)
        account.warmup_status = run_status
        if account.status == "new" and not manual_drafts_only and not _warmup_plan_zero_rpc(plan):
            account.status = "warming"
        runs.append(run)
    if all(run.status == "failed" for run in runs):
        plan.status = "failed"
    await session.flush()
    return runs


async def delete_warmup_plan(session: AsyncSession, workspace_id: str, plan: WarmupPlan) -> None:
    result = await session.execute(
        select(WarmupRun).where(WarmupRun.workspace_id == workspace_id, WarmupRun.warmup_plan_id == plan.id)
    )
    runs = list(result.scalars().all())
    run_ids = [run.id for run in runs]
    if run_ids:
        await session.execute(delete(WarmupStep).where(WarmupStep.warmup_run_id.in_(run_ids)))
    await session.execute(delete(WarmupRun).where(WarmupRun.warmup_plan_id == plan.id))
    await session.delete(plan)
    await session.flush()


async def stop_warmup_plan(
    session: AsyncSession,
    workspace_id: str,
    plan: WarmupPlan,
    reason: str = "manual_stop",
) -> list[WarmupRun]:
    now = datetime.now(UTC)
    plan.status = "stopped"
    result = await session.execute(
        select(WarmupRun).where(
            WarmupRun.workspace_id == workspace_id,
            WarmupRun.warmup_plan_id == plan.id,
            WarmupRun.status.in_(["scheduled", "running"]),
        )
    )
    runs = list(result.scalars().all())
    for run in runs:
        run.status = "stopped"
        run.completed_at = now
        run.stop_reason = reason
        pending_steps = await session.execute(
            select(WarmupStep).where(
                WarmupStep.warmup_run_id == run.id,
                WarmupStep.status.in_(["queued", "running"]),
            )
        )
        for step in pending_steps.scalars().all():
            step.status = "skipped"
            step.error_message = "План прогрева остановлен"
        account = await session.get(TelegramAccount, run.telegram_account_id)
        if account and account.warmup_status in {"scheduled", "running"}:
            account.warmup_status = "stopped"
    await session.flush()
    return runs


async def tick_active_warmup_runs(
    session: AsyncSession,
    workspace_id: str | None = None,
    *,
    limit: int = 20,
) -> list[WarmupRun]:
    now = datetime.now(UTC)
    await reconcile_completed_warmup_accounts(session, workspace_id=workspace_id, limit=100)
    query = select(WarmupRun).where(WarmupRun.status.in_(["scheduled", "running"]))
    if workspace_id:
        query = query.where(WarmupRun.workspace_id == workspace_id)
    query = query.order_by(WarmupRun.started_at.asc().nullsfirst(), WarmupRun.created_at.asc()).limit(limit)
    runs = list((await session.execute(query)).scalars().all())
    ticked: list[WarmupRun] = []
    for run in runs:
        plan = await session.get(WarmupPlan, run.warmup_plan_id)
        account = await session.get(TelegramAccount, run.telegram_account_id)
        if not plan or not account or plan.status in {"stopped", "archived", "deleted"}:
            continue
        preset_violation = _warmup_preset_violation(account, plan, now)
        if preset_violation:
            await _fail_remaining_run(session, run, account, preset_violation)
            await _complete_plan_if_no_active_runs(session, plan, now)
            ticked.append(run)
            continue
        zero_rpc = _warmup_plan_zero_rpc(plan)
        eligibility = None if zero_rpc else get_module_eligibility(account, _warmup_module_for_plan(plan))
        if eligibility and eligibility.status == "blocked":
            await _fail_remaining_run(
                session,
                run,
                account,
                eligibility.message or "Аккаунт сейчас не подходит для прогрева",
            )
            await _complete_plan_if_no_active_runs(session, plan, now)
            ticked.append(run)
            continue
        if not zero_rpc and account.proxy_required and not account.proxy_id:
            await _fail_remaining_run(session, run, account, "У аккаунта нет назначенного прокси")
            await _complete_plan_if_no_active_runs(session, plan, now)
            ticked.append(run)
            continue
        if account.status in {"archived", "deleted"} or (
            not zero_rpc and account.status in {"frozen", "quarantined", "spam_blocked", "blocked"}
        ):
            await _fail_remaining_run(session, run, account, "Аккаунт сейчас не подходит для прогрева")
            await _complete_plan_if_no_active_runs(session, plan, now)
            ticked.append(run)
            continue
        if not zero_rpc and _warmup_flood_wait_active(account, datetime.now(UTC)):
            run.stop_reason = "Telegram запросил паузу; прогрев отложен."
            continue
        step = await _next_due_step(session, run.id, now)
        if await _account_has_active_campaign(session, run.workspace_id, account.id) and (
            not step or step.action_type != "random_pauses"
        ):
            run.stop_reason = "Аккаунт используется активной кампанией; прогрев ждет свободного окна."
            account.warmup_status = "paused_by_campaign"
            continue
        run.status = "running"
        run.started_at = run.started_at or now
        account.warmup_status = "running"
        run.stop_reason = None
        if step:
            await _execute_step(session, plan, run, step, account, now)
            ticked.append(run)
            await session.flush()
        await _complete_run_if_finished(session, plan, run, account, now)
    await session.flush()
    return ticked


async def reconcile_completed_warmup_accounts(
    session: AsyncSession,
    workspace_id: str | None = None,
    *,
    limit: int = 100,
) -> list[TelegramAccount]:
    query = (
        select(WarmupRun, WarmupPlan, TelegramAccount)
        .join(WarmupPlan, WarmupPlan.id == WarmupRun.warmup_plan_id)
        .join(TelegramAccount, TelegramAccount.id == WarmupRun.telegram_account_id)
        .where(
            WarmupRun.status == "completed",
            TelegramAccount.warmup_status.in_(["scheduled", "running", "warming", "paused_by_campaign"]),
            TelegramAccount.status.notin_(["archived", "deleted", "frozen", "quarantined", "blocked", "spam_blocked"]),
        )
        .order_by(WarmupRun.completed_at.desc().nullslast(), WarmupRun.updated_at.desc())
        .limit(limit)
    )
    if workspace_id:
        query = query.where(WarmupRun.workspace_id == workspace_id)
    rows = list((await session.execute(query)).all())
    updated: list[TelegramAccount] = []
    seen_account_ids: set[str] = set()
    for run, plan, account in rows:
        if account.id in seen_account_ids:
            continue
        active = await session.execute(
            select(WarmupRun.id)
            .where(
                WarmupRun.telegram_account_id == account.id,
                WarmupRun.status.in_(["scheduled", "running"]),
            )
            .limit(1)
        )
        if active.scalar_one_or_none():
            continue
        account.warmup_status = _completed_warmup_status(plan, run)
        account.warmup_score = max(account.warmup_score, run.score_current)
        _hold_unverified_warmup_account(account, datetime.now(UTC))
        seen_account_ids.add(account.id)
        updated.append(account)
    if updated:
        await session.flush()
    return updated


async def _execute_step(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    step: WarmupStep,
    account: TelegramAccount,
    now: datetime,
) -> None:
    if _warmup_plan_zero_rpc(plan) and step.action_type != "random_pauses":
        step.status = "failed"
        step.error_code = "ZERO_RPC_PLAN_ACTION_BLOCKED"
        step.error_message = "План отдыха не может выполнять Telegram-действия."
        await _fail_remaining_run(session, run, account, step.error_message)
        return
    if step.action_type != "random_pauses" and _warmup_flood_wait_active(account, datetime.now(UTC)):
        return
    now = datetime.now(UTC)
    if step.action_type != "random_pauses" and await _warmup_attempt_budget_exhausted(session, plan, account, now):
        run.stop_reason = "Лимит попыток прогрева за последние 24 часа исчерпан."
        return
    events: list[dict[str, Any]] = []
    step.status = "running"
    step.executed_at = now
    events.append(_warmup_terminal_event(step, "info", "Шаг прогрева взят в работу"))
    step.result = {"terminal_events": events}
    await session.flush()
    try:
        result = await _perform_action(session, plan, run, step, account, events)
        step.status = result.get("status", "succeeded")
        events.append(
            _warmup_terminal_event(
                step,
                "success" if step.status == "succeeded" else "warning",
                str(result.get("message") or "Шаг прогрева завершен"),
            )
        )
        step.result = {**result, "terminal_events": events}
        step.error_code = None
        step.error_message = None
        if step.status == "succeeded" and step.action_type != "random_pauses":
            run.last_action_at = now
            run.score_current = min(100, max(run.score_current, account.warmup_score) + _score_increment(plan))
            account.warmup_score = max(account.warmup_score, run.score_current)
    except TelegramGatewayError as exc:
        _apply_warmup_gateway_error_risk(account, exc, now)
        step.status = "failed"
        step.error_code = exc.code
        step.error_message = _warmup_error_message(exc)
        events.append(_warmup_terminal_event(step, "error", step.error_message, code=exc.code))
        step.result = {"raw_error": {"code": exc.code, **exc.raw}, "terminal_events": events}
        if exc.code == "SESSION_UNAUTHORIZED":
            await _stop_warmup_for_unauthorized_session(session, plan, run, account, now)
    except Exception as exc:  # noqa: BLE001
        step.status = "failed"
        step.error_code = type(exc).__name__
        step.error_message = "Действие прогрева не выполнено. Система попробует следующие шаги позже."
        events.append(_warmup_terminal_event(step, "error", step.error_message, code=type(exc).__name__))
        step.result = {"raw_error": str(exc)[:500], "terminal_events": events}

    if (plan.config or {}).get("preset") == "primary_warmup" and step.status == "failed" and run.status != "failed":
        reason = step.error_message or (step.result or {}).get("message") or "Шаг первичного прогрева не выполнен"
        await _fail_remaining_run(session, run, account, str(reason))
        await _complete_plan_if_no_active_runs(session, plan, now)

    elapsed_days = max(1, ((now - (run.started_at or now)).days + 1))
    run.current_day = min(plan.duration_days, elapsed_days)


async def _stop_warmup_for_unauthorized_session(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    account: TelegramAccount,
    now: datetime,
) -> None:
    account.status = "failed"
    account.warmup_status = "failed"
    account.health_score = min(account.health_score, 20)
    run.status = "failed"
    run.completed_at = now
    run.stop_reason = "Telegram-сессия больше не авторизована. Нужно заново подключить аккаунт."
    pending_steps = await session.execute(
        select(WarmupStep).where(
            WarmupStep.warmup_run_id == run.id,
            WarmupStep.status.in_(["queued", "running"]),
        )
    )
    for pending_step in pending_steps.scalars().all():
        pending_step.status = "skipped"
        pending_step.error_code = "SESSION_UNAUTHORIZED"
        pending_step.error_message = "Telegram-сессия больше не авторизована."
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="account_session_unauthorized",
            severity="error",
            message="Telegram-сессия больше не авторизована. Аккаунт остановлен до повторного подключения.",
            raw_error={"code": "SESSION_UNAUTHORIZED", "warmup_plan_id": plan.id, "warmup_run_id": run.id},
        )
    )


async def _perform_action(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    step: WarmupStep,
    account: TelegramAccount,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    action = step.action_type
    if action == "random_pauses":
        events.append(_warmup_terminal_event(step, "info", "Поведенческая защита: выполняю случайную паузу"))
        return {"status": "succeeded", "message": "Пауза выполнена", "label": ACTION_LABELS[action]}
    if action == "profile_check":
        events.append(_warmup_terminal_event(step, "info", "Подключаю Telegram-сессию для проверки профиля"))
        info = await check_real_account(session, account)
        events.append(
            _warmup_terminal_event(
                step,
                "success",
                f"Профиль Telegram проверен{f': @{info.username}' if info.username else ''}",
            )
        )
        return {
            "status": "succeeded",
            "message": "Профиль проверен",
            "label": ACTION_LABELS[action],
            "username": info.username,
        }
    if action == "peer_message_exchange":
        return await _send_peer_warmup_message(session, plan, run, step, account, events)

    source = await _pick_source_for_step(session, run.workspace_id, plan, step)
    if not source:
        events.append(_warmup_terminal_event(step, "warning", "Нет подходящего источника для действия прогрева"))
        return {
            "status": "failed",
            "message": "Нет подходящих источников прогрева. Добавьте каналы в пул прогрева и проверьте доступ.",
            "label": ACTION_LABELS.get(action, action),
        }
    source_ref = source_ref_from_fields(source.username, source.url, source.tg_id)

    if action == "subscribe_channels":
        events.append(
            _warmup_terminal_event(
                step,
                "info",
                f"Проверяю доступ к источнику «{source.title or source_ref}»",
                source=source,
            )
        )
        access = await ensure_source_access_with_account(
            session,
            account,
            source_ref,
            join_if_needed=True,
            join_discussion=False,
        )
        events.append(
            _warmup_terminal_event(
                step,
                "success",
                "Вступил в источник" if access.joined else "Доступ к источнику уже есть",
                source=source,
            )
        )
        return {
            "status": "succeeded",
            "message": "Подписка/доступ к каналу проверены",
            "label": ACTION_LABELS[action],
            "source_id": source.id,
            "source_title": source.title,
            "joined": access.joined,
        }
    if action == "view_posts":
        events.append(
            _warmup_terminal_event(
                step,
                "info",
                f"Открываю источник «{source.title or source_ref}» и загружаю посты",
                source=source,
            )
        )
        posts = await fetch_posts_with_account(session, account, source_ref, limit=3)
        source.access_status = "available"
        events.append(
            _warmup_terminal_event(
                step,
                "success",
                f"Просмотрено постов: {len(posts)}",
                source=source,
            )
        )
        return {
            "status": "succeeded",
            "message": f"Просмотрено постов: {len(posts)}",
            "label": ACTION_LABELS[action],
            "source_id": source.id,
            "source_title": source.title,
            "count": len(posts),
        }
    if action == "read_messages":
        events.append(
            _warmup_terminal_event(
                step,
                "info",
                f"Читаю сообщения в «{source.title or source_ref}»",
                source=source,
            )
        )
        messages = await fetch_chat_messages_with_account(session, account, source_ref, limit=5)
        source.access_status = "available"
        events.append(
            _warmup_terminal_event(
                step,
                "success",
                f"Прочитано сообщений: {len(messages)}",
                source=source,
            )
        )
        return {
            "status": "succeeded",
            "message": f"Прочитано сообщений: {len(messages)}",
            "label": ACTION_LABELS[action],
            "source_id": source.id,
            "source_title": source.title,
            "count": len(messages),
        }
    if action == "set_reactions":
        reaction = _reaction_for_plan(plan)
        events.append(
            _warmup_terminal_event(
                step,
                "info",
                f"Ставлю реакцию {reaction} на последний пост в «{source.title or source_ref}»",
                source=source,
            )
        )
        result = await react_to_latest_post_with_account(session, account, source_ref, reaction)
        events.append(
            _warmup_terminal_event(
                step,
                "success",
                f"Поставлена реакция {reaction} на пост {result.tg_message_id}",
                source=source,
            )
        )
        return {
            "status": "succeeded",
            "message": f"Поставлена реакция {reaction}",
            "label": ACTION_LABELS[action],
            "source_id": source.id,
            "source_title": source.title,
            "tg_message_id": result.tg_message_id,
        }
    return {"status": "skipped", "message": "Неизвестное действие прогрева", "label": action}


async def _send_peer_warmup_message(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    step: WarmupStep,
    account: TelegramAccount,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    peer = await _pick_peer_account_for_step(session, run.workspace_id, plan, account, step)
    if not peer:
        events.append(_warmup_terminal_event(step, "warning", "Нет второго аккаунта для обмена сообщениями"))
        return {
            "status": "skipped",
            "message": "Нет второго активного аккаунта для обмена сообщениями",
            "label": ACTION_LABELS["peer_message_exchange"],
        }
    peer_ref = f"@{peer.username}" if peer.username else None
    if not peer_ref:
        events.append(
            _warmup_terminal_event(
                step,
                "warning",
                "У второго аккаунта нет username для безопасного сообщения",
            )
        )
        return {
            "status": "skipped",
            "message": "У второго аккаунта нет username для безопасной отправки сообщения",
            "label": ACTION_LABELS["peer_message_exchange"],
            "target_account_id": peer.id,
        }
    message = _peer_warmup_message(step)
    events.append(_warmup_terminal_event(step, "info", f"Отправляю peer-сообщение в {peer_ref}"))
    result = await send_chat_reply_with_account(
        session,
        account,
        peer_ref,
        message,
        business_intent=f"warmup:{step.id}",
    )
    events.append(_warmup_terminal_event(step, "success", f"Peer-сообщение отправлено: {result.tg_message_id}"))
    return {
        "status": "succeeded",
        "message": "Отправлено короткое сообщение другому аккаунту прогрева",
        "label": ACTION_LABELS["peer_message_exchange"],
        "target_account_id": peer.id,
        "target_ref": peer_ref,
        "text": message,
        "tg_message_id": result.tg_message_id,
    }


def _warmup_terminal_event(
    step: WarmupStep,
    level: str,
    message: str,
    *,
    code: str | None = None,
    source: TelegramSource | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "level": level,
        "message": message,
        "code": code or step.action_type,
        "action_type": step.action_type,
    }
    if source:
        event.update(
            {
                "source_id": source.id,
                "source_title": source.title,
                "source_ref": source_ref_from_fields(source.username, source.url, source.tg_id),
            }
        )
    return event


async def _pick_peer_account_for_step(
    session: AsyncSession,
    workspace_id: str,
    plan: WarmupPlan,
    account: TelegramAccount,
    step: WarmupStep,
) -> TelegramAccount | None:
    account_ids = [item for item in (plan.config or {}).get("account_ids", []) if isinstance(item, str) and item]
    if len(account_ids) < 2:
        return None
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(account_ids),
            TelegramAccount.id != account.id,
            TelegramAccount.status.notin_(["deleted", "archived", "frozen", "quarantined", "blocked", "spam_blocked"]),
        )
    )
    peers = list(result.scalars().all())
    if not peers:
        return None
    by_id = {peer.id: peer for peer in peers}
    ordered = [by_id[item] for item in account_ids if item in by_id]
    return ordered[abs(hash(step.id)) % len(ordered)]


def _peer_warmup_message(step: WarmupStep) -> str:
    return WARMUP_PEER_MESSAGES[abs(hash(step.id)) % len(WARMUP_PEER_MESSAGES)]


async def _pick_source_for_step(
    session: AsyncSession,
    workspace_id: str,
    plan: WarmupPlan,
    step: WarmupStep,
) -> TelegramSource | None:
    config = _effective_warmup_config(plan)
    source_ids = [item for item in config.get("warmup_source_ids", []) if isinstance(item, str) and item]
    statement = select(TelegramSource).where(
        TelegramSource.workspace_id == workspace_id,
        TelegramSource.access_status.in_(["available", "pending_check", "unknown"]),
        TelegramSource.type.in_(["channel", "group", "dialog"]),
        TelegramSource.is_private.is_(False),
        TelegramSource.is_join_request_required.is_(False),
    )
    if source_ids:
        statement = statement.where(TelegramSource.id.in_(source_ids))
    result = await session.execute(
        statement.order_by(TelegramSource.updated_at.desc(), TelegramSource.created_at.desc()).limit(100)
    )
    sources = list(result.scalars().all())
    needs_pool = config.get("source_strategy") == "warmup_pool" or config.get("require_warmup_sources") is True
    if needs_pool:
        sources = [source for source in sources if (source.source_metadata or {}).get("warmup_pool") is True]
    if not sources:
        return None
    offset = abs(hash(step.id)) % len(sources)
    return sources[offset]


async def _warmup_source_pool_error(session: AsyncSession, workspace_id: str, plan: WarmupPlan) -> str | None:
    config = _effective_warmup_config(plan)
    activities = {str(item) for item in config.get("activities", []) if isinstance(item, str)}
    sequence_sessions = [item for item in config.get("sequence_sessions", []) if isinstance(item, dict)]
    for session_config in sequence_sessions:
        activities.update(str(item) for item in session_config.get("actions", []) if isinstance(item, str))
    stage_sessions = [item for item in config.get("stage_sessions", []) if isinstance(item, dict)]
    for stage_config in stage_sessions:
        activities.update(str(item) for item in stage_config.get("actions", []) if isinstance(item, str))
    if not activities.intersection(SOURCE_ACTIONS):
        return None

    min_sources = _int_between(config.get("min_warmup_sources"), 0, 500, 0)
    required = config.get("require_warmup_sources") is True or config.get("source_strategy") == "warmup_pool"
    if not required and min_sources <= 0:
        return None

    source_ids = [item for item in config.get("warmup_source_ids", []) if isinstance(item, str) and item]
    statement = select(TelegramSource).where(
        TelegramSource.workspace_id == workspace_id,
        TelegramSource.access_status.in_(["available", "pending_check", "unknown"]),
        TelegramSource.type.in_(["channel", "group", "dialog"]),
        TelegramSource.is_private.is_(False),
        TelegramSource.is_join_request_required.is_(False),
    )
    if source_ids:
        statement = statement.where(TelegramSource.id.in_(source_ids))
    result = await session.execute(statement.limit(500))
    sources = list(result.scalars().all())
    if required:
        sources = [source for source in sources if (source.source_metadata or {}).get("warmup_pool") is True]
    if min_sources and len(sources) < min_sources:
        return (
            f"Для первичного прогрева нужно минимум {min_sources} каналов/групп в пуле прогрева. "
            f"Сейчас доступно: {len(sources)}."
        )
    if required and not sources:
        return "Для прогрева с чтением нужен пул каналов/групп. Добавьте источники прогрева перед запуском."
    return None


async def _account_has_active_campaign(session: AsyncSession, workspace_id: str, account_id: str) -> bool:
    result = await session.execute(
        select(Campaign, CampaignRun)
        .join(CampaignRun, CampaignRun.campaign_id == Campaign.id)
        .where(
            Campaign.workspace_id == workspace_id,
            Campaign.status.in_(["running", "active"]),
            CampaignRun.status.in_(["pending", "running"]),
        )
        .order_by(CampaignRun.created_at.desc())
        .limit(200)
    )
    for campaign, _run in result.all():
        account_ids = (campaign.config or {}).get("account_ids")
        if isinstance(account_ids, list) and account_id in {str(item) for item in account_ids if item}:
            return True
    return False


async def _next_due_step(session: AsyncSession, run_id: str, now: datetime) -> WarmupStep | None:
    result = await session.execute(
        select(WarmupStep)
        .where(
            WarmupStep.warmup_run_id == run_id,
            WarmupStep.status == "queued",
            WarmupStep.planned_at <= now,
        )
        .order_by(WarmupStep.planned_at.asc(), WarmupStep.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _complete_run_if_finished(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    account: TelegramAccount,
    now: datetime,
) -> None:
    if run.status == "failed":
        return
    remaining = await session.execute(
        select(WarmupStep.id)
        .where(
            WarmupStep.warmup_run_id == run.id,
            WarmupStep.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if remaining.scalar_one_or_none():
        return
    run.status = "completed"
    run.completed_at = run.completed_at or now
    account.warmup_status = _completed_warmup_status(plan, run)
    account.warmup_score = max(account.warmup_score, run.score_current)
    _hold_unverified_warmup_account(account, now)

    active = await session.execute(
        select(WarmupRun.id)
        .where(
            WarmupRun.warmup_plan_id == plan.id,
            WarmupRun.status.in_(["scheduled", "running"]),
        )
        .limit(1)
    )
    if not active.scalar_one_or_none():
        plan.status = "completed"


async def _complete_plan_if_no_active_runs(session: AsyncSession, plan: WarmupPlan, now: datetime) -> None:
    active = await session.execute(
        select(WarmupRun.id)
        .where(
            WarmupRun.warmup_plan_id == plan.id,
            WarmupRun.status.in_(["scheduled", "running"]),
        )
        .limit(1)
    )
    if not active.scalar_one_or_none():
        plan.status = "completed"
        plan.updated_at = now


async def _fail_remaining_run(session: AsyncSession, run: WarmupRun, account: TelegramAccount, message: str) -> None:
    now = datetime.now(UTC)
    run.status = "failed"
    run.completed_at = now
    run.stop_reason = message
    account.warmup_status = "failed"
    result = await session.execute(
        select(WarmupStep).where(WarmupStep.warmup_run_id == run.id, WarmupStep.status.in_(["queued", "running"]))
    )
    for step in result.scalars().all():
        step.status = "skipped"
        step.error_message = message


async def _plan_accounts(session: AsyncSession, workspace_id: str, plan: WarmupPlan) -> list[TelegramAccount]:
    zero_rpc = _warmup_plan_zero_rpc(plan)
    excluded_statuses = (
        ["deleted", "archived"]
        if zero_rpc
        else [
            "deleted",
            "archived",
            "frozen",
            "quarantined",
            "blocked",
            "spam_blocked",
        ]
    )
    raw_ids = list((plan.config or {}).get("account_ids", []))
    ids = [item for item in raw_ids if isinstance(item, str) and item]
    if not ids:
        if any(item is None for item in raw_ids):
            result = await session.execute(
                select(TelegramAccount)
                .where(
                    TelegramAccount.workspace_id == workspace_id,
                    TelegramAccount.status.notin_(excluded_statuses),
                    or_(zero_rpc, TelegramAccount.proxy_required.is_(False), TelegramAccount.proxy_id.is_not(None)),
                )
                .order_by(TelegramAccount.created_at.asc())
            )
            return list(result.scalars().all())
        return []
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id.in_(ids),
            TelegramAccount.status.notin_(excluded_statuses),
            or_(zero_rpc, TelegramAccount.proxy_required.is_(False), TelegramAccount.proxy_id.is_not(None)),
        )
    )
    by_id = {account.id: account for account in result.scalars().all()}
    return [by_id[item] for item in ids if item in by_id]


async def _replace_queued_steps(
    session: AsyncSession,
    plan: WarmupPlan,
    run: WarmupRun,
    account_id: str,
    start_at: datetime,
    account_index: int,
    account_count: int,
) -> None:
    await session.execute(
        delete(WarmupStep).where(
            WarmupStep.warmup_run_id == run.id,
            WarmupStep.status == "queued",
        )
    )
    await session.flush()
    session.add_all(
        _build_steps(
            plan,
            run,
            account_id,
            start_at,
            account_index=account_index,
            account_count=account_count,
        )
    )


def _build_steps(
    plan: WarmupPlan,
    run: WarmupRun,
    account_id: str,
    start_at: datetime,
    *,
    account_index: int = 0,
    account_count: int = 1,
) -> list[WarmupStep]:
    config = _effective_warmup_config(plan)
    if (plan.policy or {}).get("mode") == "manual_drafts_only" or config.get("scenario") == "safe_manual_intro":
        return _build_safe_manual_steps(run, account_id, start_at)
    if isinstance(config.get("sequence_sessions"), list):
        return _build_sequence_session_steps(
            plan,
            run,
            account_id,
            start_at,
            account_index=account_index,
            account_count=account_count,
            config=config,
        )
    if isinstance(config.get("stage_sessions"), list):
        return _build_stage_session_steps(
            plan,
            run,
            account_id,
            start_at,
            account_index=account_index,
            account_count=account_count,
            config=config,
        )

    activities = [item for item in config.get("activities", DEFAULT_ACTIVITIES) if isinstance(item, str)] or [
        "random_pauses"
    ]
    force_profile_check = config.get("force_profile_check") is not False and not _warmup_plan_zero_rpc(plan)
    if force_profile_check and "profile_check" not in activities:
        activities = ["profile_check", *activities]
    daily_limit = _int_between(config.get("daily_action_limit"), 1, 500, 25)
    duration_hours = _int_between(config.get("duration_hours"), 0, 23, 0)
    total_hours = max(1, int(plan.duration_days or 1) * 24 + duration_hours)
    duration_days = max(1, int(plan.duration_days or 1))
    rnd = random.Random(f"{plan.id}:{run.id}:{account_id}:{run.created_at or start_at.isoformat()}")
    max_steps_total = _int_between(config.get("max_steps_total"), 1, 240, 240)
    daily_limits = _daily_limits_for_config(config, duration_days, daily_limit)
    min_interval = _int_between(config.get("min_interval_minutes"), 0, 24 * 60, 0) or _min_interval_minutes(
        config.get("intensity")
    )
    active_from = _minutes_from_time(config.get("active_hours_from"), 9 * 60)
    active_to = _minutes_from_time(config.get("active_hours_to"), 21 * 60)
    active_window = active_to - active_from
    if active_window <= 0:
        active_window += 24 * 60
    min_gap = max(10, min_interval + rnd.randint(-min(20, min_interval // 4), min(25, max(1, min_interval // 3))))
    first_rest_minutes = max(0, _int_between(config.get("first_rest_hours"), 0, 24 * duration_days, 0) * 60)
    batch_spread = _int_between(config.get("batch_start_spread_minutes"), 0, 180, 0)
    account_start_offset = account_index * batch_spread + rnd.randint(0, max(15, batch_spread or 15))
    steps: list[WarmupStep] = []

    total_minutes = max(total_hours * 60, max_steps_total * min_gap)
    planned_offsets: list[tuple[int, int]] = []
    for day in range(duration_days):
        cap = min(daily_limits[day], max_steps_total - len(planned_offsets))
        if cap <= 0:
            break
        day_count = _humanized_day_action_count(rnd, cap, day)
        day_offsets: list[int] = []
        attempts = 0
        while len(day_offsets) < day_count and attempts < day_count * 120:
            attempts += 1
            minute_in_window = _weighted_minute_in_window(rnd, active_window)
            offset = day * 24 * 60 + active_from + minute_in_window + account_start_offset
            if offset < first_rest_minutes:
                continue
            if offset > total_minutes + 24 * 60:
                continue
            if all(abs(offset - current) >= min_gap for current in day_offsets):
                day_offsets.append(offset)
        planned_offsets.extend((day + 1, offset) for offset in day_offsets)
    planned_offsets.sort(key=lambda item: item[1])

    if not planned_offsets:
        planned_offsets.append((1, first_rest_minutes + account_start_offset + rnd.randint(5, 40)))
    should_force_peer_step = (
        account_count > 1
        and "peer_message_exchange" in activities
        and len(planned_offsets) < 2
        and max_steps_total >= 2
    )
    if should_force_peer_step:
        peer_day = next(
            (
                day
                for day in range(1, duration_days + 1)
                if "peer_message_exchange" in _action_pool_for_day(activities, config, day)
            ),
            1,
        )
        previous_offset = planned_offsets[-1][1]
        peer_offset = max(
            previous_offset + min_gap,
            (peer_day - 1) * 24 * 60 + active_from + account_start_offset + rnd.randint(45, max(60, active_window - 1)),
        )
        planned_offsets.append((peer_day, peer_offset))
        planned_offsets.sort(key=lambda item: item[1])

    first_offset = max(0, int((start_at - (run.started_at or start_at)).total_seconds() // 60))
    for index, (day_number, offset) in enumerate(planned_offsets[:max_steps_total]):
        action_pool = _action_pool_for_day(activities, config, day_number)
        if index == 0 and "profile_check" in activities:
            action = "profile_check"
        elif index == 1 and account_count > 1 and "peer_message_exchange" in action_pool:
            action = "peer_message_exchange"
        else:
            action = _weighted_action_choice(rnd, action_pool, config)
        action = _cap_planned_action(action, steps, config)
        if index == 0:
            planned_at = start_at + timedelta(minutes=max(2, min(offset, first_rest_minutes + account_start_offset)))
        else:
            planned_at = start_at + timedelta(minutes=max(first_offset, offset))
        if planned_at < start_at:
            planned_at = start_at + timedelta(minutes=rnd.randint(2, 25))
        steps.append(
            WarmupStep(
                warmup_run_id=run.id,
                telegram_account_id=account_id,
                action_type=action,
                status="queued",
                planned_at=planned_at,
                result={
                    "label": ACTION_LABELS.get(action, action),
                    "planned_index": index + 1,
                    "day": day_number,
                    "day_limit": daily_limits[min(day_number - 1, len(daily_limits) - 1)],
                    "preset": config.get("preset"),
                    "randomized": True,
                },
            )
        )
    return steps


def _build_sequence_session_steps(
    plan: WarmupPlan,
    run: WarmupRun,
    account_id: str,
    start_at: datetime,
    *,
    account_index: int,
    account_count: int,
    config: dict[str, Any],
) -> list[WarmupStep]:
    sequence_sessions = [item for item in config.get("sequence_sessions", []) if isinstance(item, dict)]
    rnd = random.Random(f"sequence:{plan.id}:{run.id}:{account_id}:{run.created_at or start_at.isoformat()}")
    first_rest_minutes = max(0, _int_between(config.get("first_rest_hours"), 0, 30 * 24, 0) * 60)
    batch_spread = _int_between(config.get("batch_start_spread_minutes"), 0, 180, 0)
    account_start_offset = account_index * batch_spread + rnd.randint(0, max(8, batch_spread or 8))
    max_steps_total = _int_between(config.get("max_steps_total"), 1, 240, 60)
    active_from = _minutes_from_time(config.get("active_hours_from"), 9 * 60)
    active_to = _minutes_from_time(config.get("active_hours_to"), 21 * 60)
    timezone = _timezone_from_config(config.get("timezone"))
    steps: list[WarmupStep] = []
    previous_action_session_start_at: datetime | None = None
    previous_session_end_at: datetime | None = None
    for session_index, session_config in enumerate(sequence_sessions, start=1):
        if len(steps) >= max_steps_total:
            break
        stage_name = str(session_config.get("stage") or f"session_{session_index}")
        start_after = first_rest_minutes + _int_between(session_config.get("start_after_minutes"), 0, 30 * 24 * 60, 0)
        duration = _int_between(session_config.get("duration_minutes"), 1, 24 * 60, 30)
        action_limit = _int_between(session_config.get("action_limit"), 1, 60, 1)
        actions_per_hour = _int_between(session_config.get("actions_per_hour"), 1, 60, action_limit)
        actions = [
            action
            for action in session_config.get("actions", ["random_pauses"])
            if isinstance(action, str) and action in ACTION_LABELS
        ] or ["random_pauses"]
        session_action_count = min(
            action_limit,
            max(1, int((duration / 60) * actions_per_hour)),
            max_steps_total - len(steps),
        )
        is_rest_session = "rest" in stage_name
        session_jitter = rnd.randint(0, max(2, min(9, duration)))
        session_offset_minutes = start_after + account_start_offset + session_jitter
        session_start_at = start_at + timedelta(minutes=session_offset_minutes)
        if previous_session_end_at is not None:
            session_start_at = max(session_start_at, previous_session_end_at)
        if not is_rest_session:
            session_start_at = _align_to_active_window(
                session_start_at,
                0,
                active_from=active_from,
                active_to=active_to,
                timezone=timezone,
            )
            session_start_at = _fit_session_to_active_window(
                session_start_at,
                duration,
                active_from=active_from,
                active_to=active_to,
                timezone=timezone,
            )
            if previous_action_session_start_at is not None:
                minimum_next_session_at = previous_action_session_start_at + timedelta(minutes=max(60, duration))
                if session_start_at <= minimum_next_session_at:
                    session_start_at = _align_to_active_window(
                        minimum_next_session_at,
                        0,
                        active_from=active_from,
                        active_to=active_to,
                        timezone=timezone,
                    )
                    session_start_at = _fit_session_to_active_window(
                        session_start_at,
                        duration,
                        active_from=active_from,
                        active_to=active_to,
                        timezone=timezone,
                    )
            previous_action_session_start_at = session_start_at
        session_first_step_index = len(steps)
        for action_index in range(session_action_count):
            action = actions[action_index % len(actions)]
            action = _cap_planned_action(action, steps, config)
            if is_rest_session:
                action = "random_pauses"
            spacing = 0 if session_action_count == 1 else int((duration / max(1, session_action_count)) * action_index)
            jitter = rnd.randint(0, max(2, min(9, duration)))
            if is_rest_session:
                planned_at = session_start_at + timedelta(minutes=duration + spacing + jitter)
            else:
                planned_at = session_start_at + timedelta(minutes=spacing + jitter)
                planned_at = _align_to_active_window(
                    planned_at,
                    0,
                    active_from=active_from,
                    active_to=active_to,
                    timezone=timezone,
                )
            steps.append(
                WarmupStep(
                    warmup_run_id=run.id,
                    telegram_account_id=account_id,
                    action_type=action,
                    status="queued",
                    planned_at=planned_at,
                    result={
                        "label": ACTION_LABELS.get(action, action),
                        "planned_index": len(steps) + 1,
                        "stage": stage_name,
                        "session_index": session_index,
                        "session_duration_minutes": duration,
                        "actions_per_hour": actions_per_hour,
                        "preset": config.get("preset"),
                        "sequence_based": True,
                        "randomized": True,
                    },
                )
            )
        previous_session_end_at = max(
            session_start_at + timedelta(minutes=duration),
            max(step.planned_at for step in steps[session_first_step_index:]) + timedelta(minutes=1),
        )
    return steps


def _build_stage_session_steps(
    plan: WarmupPlan,
    run: WarmupRun,
    account_id: str,
    start_at: datetime,
    *,
    account_index: int,
    account_count: int,
    config: dict[str, Any],
) -> list[WarmupStep]:
    duration_days = max(1, int(plan.duration_days or 1))
    stage_sessions = [item for item in config.get("stage_sessions", []) if isinstance(item, dict)]
    if not stage_sessions:
        return []
    rnd = random.Random(f"stage:{plan.id}:{run.id}:{account_id}:{run.created_at or start_at.isoformat()}")
    active_from = _minutes_from_time(config.get("active_hours_from"), 11 * 60)
    active_to = _minutes_from_time(config.get("active_hours_to"), 21 * 60)
    active_window = active_to - active_from
    if active_window <= 0:
        active_window += 24 * 60
    batch_spread = _int_between(config.get("batch_start_spread_minutes"), 0, 180, 0)
    account_start_offset = account_index * batch_spread + rnd.randint(0, max(15, batch_spread or 15))
    max_steps_total = _int_between(config.get("max_steps_total"), 1, 240, 60)
    steps: list[WarmupStep] = []

    for stage_index, stage_config in enumerate(stage_sessions, start=1):
        stage_name = str(stage_config.get("stage") or f"stage_{stage_index}")
        start_day = _int_between(stage_config.get("start_day"), 1, duration_days, 1)
        end_day = _int_between(stage_config.get("end_day"), start_day, duration_days, start_day)
        daily_limit = _int_between(stage_config.get("daily_action_limit"), 1, 6, 1)
        actions_per_session = _int_between(stage_config.get("actions_per_session"), 1, 4, daily_limit)
        session_duration = _int_between(stage_config.get("session_duration_minutes"), 15, 180, 45)
        min_interval = _int_between(stage_config.get("min_interval_minutes"), 60, 24 * 60, 720)
        actions = [
            action
            for action in stage_config.get("actions", ["random_pauses"])
            if isinstance(action, str) and action in ACTION_LABELS
        ] or ["random_pauses"]

        for day_number in range(start_day, end_day + 1):
            if len(steps) >= max_steps_total:
                return steps
            day_action_count = min(daily_limit, actions_per_session, max_steps_total - len(steps))
            session_anchor = _weighted_minute_in_window(rnd, active_window)
            session_start = (day_number - 1) * 24 * 60 + active_from + session_anchor + account_start_offset
            for action_index in range(day_action_count):
                if len(steps) >= max_steps_total:
                    return steps
                action = actions[action_index % len(actions)]
                action = _cap_planned_action(action, steps, config)
                if stage_name == "rest":
                    action = "random_pauses"
                spacing = 0 if day_action_count == 1 else min(session_duration, action_index * min_interval)
                jitter = rnd.randint(0, max(5, min(25, session_duration)))
                planned_at = start_at + timedelta(minutes=max(2, session_start + spacing + jitter))
                steps.append(
                    WarmupStep(
                        warmup_run_id=run.id,
                        telegram_account_id=account_id,
                        action_type=action,
                        status="queued",
                        planned_at=planned_at,
                        result={
                            "label": ACTION_LABELS.get(action, action),
                            "planned_index": len(steps) + 1,
                            "day": day_number,
                            "stage": stage_name,
                            "stage_index": stage_index,
                            "session_id": f"{stage_name}:day-{day_number}",
                            "session_duration_minutes": session_duration,
                            "actions_per_session": actions_per_session,
                            "min_interval_minutes": min_interval,
                            "preset": config.get("preset"),
                            "stage_based": True,
                            "randomized": True,
                        },
                    )
                )
    return steps


def _effective_warmup_config(plan: WarmupPlan) -> dict[str, Any]:
    config = dict(plan.config or {})
    preset_id = str(config.get("preset") or "").strip()
    preset = WARMUP_PRESETS.get(preset_id)
    preset_config = dict((preset or {}).get("config") or {})
    preset_config.update(config)
    if preset_id:
        preset_config["preset"] = preset_id
    policy = {**dict((preset or {}).get("policy") or {}), **dict(plan.policy or {})}
    if policy.get("rest_only") or policy.get("zero_telegram_actions"):
        preset_config["activities"] = ["random_pauses"]
        preset_config["force_profile_check"] = False
        for key in ("sequence_sessions", "stage_sessions"):
            if isinstance(preset_config.get(key), list):
                preset_config[key] = [
                    {**item, "actions": ["random_pauses"]} for item in preset_config[key] if isinstance(item, dict)
                ]
    return preset_config


def _warmup_plan_zero_rpc(plan: WarmupPlan) -> bool:
    config = _effective_warmup_config(plan)
    for key in ("sequence_sessions", "stage_sessions"):
        if isinstance(config.get(key), list):
            sessions = [item for item in config[key] if isinstance(item, dict)]
            return bool(sessions) and all(
                isinstance(item.get("actions", []), list)
                and all(action == "random_pauses" for action in item.get("actions", []))
                for item in sessions
            )
    activities = config.get("activities")
    return (
        isinstance(activities, list)
        and all(action == "random_pauses" for action in activities)
        and config.get("force_profile_check") is not True
    )


def _hold_unverified_warmup_account(account: TelegramAccount, now: datetime) -> None:
    data = dict(account.daily_limits or {})
    restriction = data.get("restriction_status")
    checked_at = _warmup_deadline(restriction.get("checked_at")) if isinstance(restriction, dict) else None
    verified = (
        account.restriction_status == "clear"
        and checked_at is not None
        and now - timedelta(hours=24) <= checked_at <= now
        and not data.get("flood_wait_unknown")
    )
    if not verified and account.status in {"new", "warming", "active"}:
        account.status = "quarantined"


def _warmup_module_for_plan(plan: WarmupPlan) -> str:
    if _warmup_plan_zero_rpc(plan):
        return "warmup_rest"
    preset = str((plan.config or {}).get("preset") or "")
    if preset == "new_account_validation":
        return "warmup_validation"
    if preset in {"primary_warmup", "new_account_safe", "incubator_safe_v2"}:
        return "warmup_safe"
    if preset in {"trusted_account_growth", "aggressive_old_account"}:
        return "warmup_growth"
    return "warmup_balanced"


def _completed_warmup_status(plan: WarmupPlan, run: WarmupRun) -> str:
    if str((plan.config or {}).get("preset") or "") == "new_account_validation":
        return "validated"
    return "completed"


def _mark_incubator_account_for_plan(account: TelegramAccount, plan: WarmupPlan, now: datetime) -> None:
    if str((plan.config or {}).get("preset") or "") != "incubator_safe_v2":
        return
    limits = dict(account.daily_limits or {})
    metadata = dict(limits.get("account_metadata") or {})
    metadata["incubator_mode"] = True
    metadata["incubator_until_at"] = (now + timedelta(days=max(1, int(plan.duration_days or 14)))).date().isoformat()
    metadata.setdefault("origin", account.origin)
    limits["account_metadata"] = metadata
    account.daily_limits = limits


def _warmup_preset_violation(account: TelegramAccount, plan: WarmupPlan, now: datetime) -> str | None:
    if _warmup_plan_zero_rpc(plan):
        return None
    preset = str((plan.config or {}).get("preset") or "")
    if preset == "aggressive_old_account":
        return "Агрессивный автоматический прогрев отключен: выбери щадящий или ручной сценарий."
    account_age_days = max(0, (now - (account.created_at or now)).days)
    if preset == "trusted_account_growth" and account_age_days < 7:
        return "Пресет роста разрешен только аккаунтам старше 7 дней в системе."
    if preset == "trusted_account_growth" and account.restriction_status != "clear":
        return "Пресет роста разрешен только аккаунтам без runtime-ограничений."
    if preset == "trusted_account_growth" and account.warmup_score < 80:
        return "Пресет роста разрешен только аккаунтам с warmup score от 80."
    if preset == "balanced_human" and not account.proxy_id:
        return "Сбалансированный прогрев требует назначенный прокси."
    if preset == "primary_warmup" and not account.proxy_id:
        return "Первичный прогрев требует отдельный чистый прокси."
    if preset == "incubator_safe_v2" and not account.proxy_id:
        return "Инкубатор требует отдельный чистый прокси."
    return None


def _daily_limits_for_config(config: dict[str, Any], duration_days: int, daily_limit: int) -> list[int]:
    raw_limits = config.get("daily_action_limits")
    limits: list[int] = []
    if isinstance(raw_limits, list):
        limits = [_int_between(item, 1, 500, daily_limit) for item in raw_limits]
    if not limits:
        limits = [daily_limit for _ in range(duration_days)]
    while len(limits) < duration_days:
        limits.append(limits[-1])
    return limits[:duration_days]


def _humanized_day_action_count(rnd: random.Random, cap: int, day_index: int) -> int:
    if cap <= 1:
        return cap
    floor = 1 if day_index == 0 else max(1, cap - max(1, cap // 3))
    return rnd.randint(floor, cap)


def _weighted_minute_in_window(rnd: random.Random, active_window: int) -> int:
    if active_window <= 90:
        return rnd.randrange(max(1, active_window))
    # People cluster around late morning, afternoon and evening rather than uniformly every minute.
    anchors = [0.18, 0.42, 0.72, 0.88]
    anchor = rnd.choice(anchors)
    center = int(active_window * anchor)
    spread = max(30, active_window // 10)
    return max(0, min(active_window - 1, int(rnd.gauss(center, spread))))


def _timezone_from_config(value: Any) -> ZoneInfo:
    if isinstance(value, str) and value.strip():
        try:
            return ZoneInfo(value.strip())
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")
    return ZoneInfo("UTC")


def _align_to_active_window(
    start_at: datetime,
    offset_minutes: int,
    *,
    active_from: int,
    active_to: int,
    timezone: ZoneInfo,
) -> datetime:
    candidate = start_at + timedelta(minutes=max(0, offset_minutes))
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    local_candidate = candidate.astimezone(timezone)
    local_date = local_candidate.date()
    for day_offset in range(370):
        window_date = local_date + timedelta(days=day_offset)
        window_start = datetime.combine(window_date, datetime.min.time(), tzinfo=timezone) + timedelta(
            minutes=active_from
        )
        window_end = datetime.combine(window_date, datetime.min.time(), tzinfo=timezone) + timedelta(minutes=active_to)
        if window_end <= window_start:
            window_end += timedelta(days=1)
        if local_candidate < window_start:
            return window_start.astimezone(UTC)
        if window_start <= local_candidate < window_end:
            return local_candidate.astimezone(UTC)
    return candidate.astimezone(UTC)


def _fit_session_to_active_window(
    session_start_at: datetime,
    duration_minutes: int,
    *,
    active_from: int,
    active_to: int,
    timezone: ZoneInfo,
) -> datetime:
    if session_start_at.tzinfo is None:
        session_start_at = session_start_at.replace(tzinfo=UTC)
    local_start = session_start_at.astimezone(timezone)
    duration = timedelta(minutes=max(1, duration_minutes))
    for day_offset in range(370):
        window_date = local_start.date() + timedelta(days=day_offset)
        window_start = datetime.combine(window_date, datetime.min.time(), tzinfo=timezone) + timedelta(
            minutes=active_from
        )
        window_end = datetime.combine(window_date, datetime.min.time(), tzinfo=timezone) + timedelta(minutes=active_to)
        if window_end <= window_start:
            window_end += timedelta(days=1)
        candidate = max(local_start, window_start) if day_offset == 0 else window_start
        if candidate + duration <= window_end:
            return candidate.astimezone(UTC)
    return session_start_at.astimezone(UTC)


def _action_pool_for_day(activities: list[str], config: dict[str, Any], day_number: int) -> list[str]:
    disabled = config.get("disabled_actions_before_day")
    disabled_map = disabled if isinstance(disabled, dict) else {}
    pool = []
    for action in activities:
        if action == "profile_check":
            continue
        min_day = _int_between(disabled_map.get(action), 1, 30, 1)
        if day_number >= min_day:
            pool.append(action)
    return pool or ["random_pauses"]


def _weighted_action_choice(rnd: random.Random, action_pool: list[str], config: dict[str, Any]) -> str:
    weights = config.get("action_weights")
    if not isinstance(weights, dict):
        return rnd.choice(action_pool)
    expanded: list[str] = []
    for action in action_pool:
        expanded.extend([action] * _int_between(weights.get(action), 1, 20, 1))
    return rnd.choice(expanded or action_pool)


def _cap_planned_action(action: str, planned_steps: list[WarmupStep], config: dict[str, Any]) -> str:
    max_counts = config.get("max_action_counts")
    if not isinstance(max_counts, dict):
        return action
    max_count = _int_between(max_counts.get(action), 0, 500, 500)
    if max_count >= 500:
        return action
    current_count = sum(1 for step in planned_steps if step.action_type == action)
    if current_count >= max_count:
        return "random_pauses"
    return action


def _build_safe_manual_steps(run: WarmupRun, account_id: str, start_at: datetime) -> list[WarmupStep]:
    actions = [
        ("profile_checklist", start_at + timedelta(minutes=5)),
        ("manual_reading_window", start_at + timedelta(minutes=20)),
        ("manual_draft_message", start_at + timedelta(minutes=45)),
        ("rest_window", start_at + timedelta(minutes=75)),
    ]
    return [
        WarmupStep(
            warmup_run_id=run.id,
            telegram_account_id=account_id,
            action_type=action,
            status="manual",
            planned_at=planned_at,
            result={
                "label": ACTION_LABELS[action],
                "safe_manual": True,
                "requires_operator": True,
            },
        )
        for action, planned_at in actions
    ]


def _min_interval_minutes(intensity: Any) -> int:
    if intensity == "rest":
        return 720
    if intensity in {"validation", "new_safe"}:
        return 150
    if intensity == "aggressive":
        return 20
    if intensity == "growth":
        return 60
    if intensity == "incubator_v2":
        return 360
    if intensity == "cautious":
        return 90
    return 45


def _score_increment(plan: WarmupPlan) -> int:
    intensity = _effective_warmup_config(plan).get("intensity")
    if intensity == "incubator_v2":
        return 4
    if intensity in {"validation", "new_safe"}:
        return 1
    if intensity == "aggressive":
        return 3
    if intensity == "cautious":
        return 1
    return 2


def _reaction_for_plan(plan: WarmupPlan) -> str:
    reactions = (plan.config or {}).get("reactions")
    if isinstance(reactions, list):
        clean = [str(item) for item in reactions if str(item).strip()]
        if clean:
            return clean[0]
    return "👍"


def _int_between(value: Any, min_value: int, max_value: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, number))


def _minutes_from_time(value: Any, default: int) -> int:
    if not isinstance(value, str):
        return default
    try:
        hours, minutes = value.split(":", 1)
        return max(0, min(23, int(hours))) * 60 + max(0, min(59, int(minutes)))
    except (TypeError, ValueError):
        return default


def _warmup_error_message(exc: TelegramGatewayError) -> str:
    text = f"{exc.code} {exc}".lower()
    if "flood" in text or "wait of" in text:
        return "Telegram попросил паузу. Следующие действия будут выполнены позже."
    if "frozen" in text:
        return "Аккаунт заморожен Telegram, прогрев остановлен для этого аккаунта."
    if "private" in text or "forbidden" in text:
        return "Источник недоступен аккаунту, шаг пропущен."
    if "reaction" in text:
        return "Telegram не принял реакцию для этого поста, шаг пропущен."
    return "Telegram временно не принял действие, шаг записан как ошибка."


def _apply_warmup_gateway_error_risk(account: TelegramAccount, exc: TelegramGatewayError, now: datetime) -> None:
    now = datetime.now(UTC)
    data = dict(account.daily_limits or {})
    text = f"{exc.code} {exc}".lower()
    if exc.code == "TELEGRAM_CONNECTION_FAILED":
        account.health_score = max(0, min(account.health_score, 60) - 10)
        data["last_telegram_connection_failed_at"] = now.isoformat()
        data["telegram_connection_failed_count"] = (
            _int_between(data.get("telegram_connection_failed_count"), 0, 10_000, 0) + 1
        )
    elif "reactionstoomany" in text or "reactions too many" in text:
        account.health_score = max(0, min(account.health_score, 60) - 15)
        data["last_reactions_too_many_at"] = now.isoformat()
    elif "flood" in text or "wait of" in text:
        account.health_score = max(0, min(account.health_score, 70) - 10)
        data["last_flood_wait_at"] = now.isoformat()
        seconds = _warmup_flood_wait_seconds(exc)
        data["last_flood_wait_seconds"] = seconds
        if seconds is None:
            if account.status not in {"frozen", "spam_blocked", "blocked"}:
                account.status = "quarantined"
            data["flood_wait_unknown"] = True
        else:
            deadline = now + timedelta(seconds=seconds + 15)
            for key in ("auto_join_wait_until", "source_fetch_wait_until"):
                previous = _warmup_deadline(data.get(key))
                data[key] = max(previous or now, deadline).isoformat()
    account.daily_limits = data


def _warmup_flood_wait_seconds(exc: TelegramGatewayError) -> int | None:
    raw = exc.raw if isinstance(exc.raw, dict) else {}
    seconds = raw.get("seconds")
    if type(seconds) is int and seconds >= 0:
        return seconds
    match = re.search(r"(?:wait of\s+|flood[ _-]*wait[\s:_]*)(\d+)\b(?!\.\d)", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _warmup_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _warmup_flood_wait_active(account: TelegramAccount, now: datetime) -> bool:
    data = dict(account.daily_limits or {})
    if data.get("flood_wait_unknown"):
        return True
    return any(
        deadline and now < deadline
        for key in ("source_fetch_wait_until", "auto_join_wait_until")
        if (deadline := _warmup_deadline(data.get(key)))
    )


async def _warmup_attempt_budget_exhausted(
    session: AsyncSession,
    plan: WarmupPlan,
    account: TelegramAccount,
    now: datetime,
) -> bool:
    config = _effective_warmup_config(plan)
    if "daily_action_limit" not in config:
        return False
    limit = _int_between(config.get("daily_action_limit"), 0, 500, 0)
    count = await session.scalar(
        select(func.count(WarmupStep.id)).where(
            WarmupStep.telegram_account_id == account.id,
            WarmupStep.action_type != "random_pauses",
            WarmupStep.executed_at > now - timedelta(hours=24),
            WarmupStep.status.in_(["running", "succeeded", "failed"]),
        )
    )
    return (count or 0) >= limit
