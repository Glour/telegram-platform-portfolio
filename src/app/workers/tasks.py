import asyncio
import random
from typing import Any

from app.modules.telegram_gateway.service import ensure_source_access_with_account
from sqlalchemy import select

# Import model modules so SQLAlchemy can resolve cross-module foreign keys in Celery workers.
import app.modules.ai.models  # noqa: F401,E402
import app.modules.assistant.models  # noqa: F401,E402
import app.modules.auth.models  # noqa: F401,E402
import app.modules.comments.models  # noqa: F401,E402
import app.modules.operations.models  # noqa: F401,E402
import app.modules.proxies.models  # noqa: F401,E402
import app.modules.sources.models  # noqa: F401,E402
import app.modules.telegram_accounts.models  # noqa: F401,E402
import app.modules.workspaces.models  # noqa: F401,E402
from app.db.session import AsyncSessionLocal, engine
from app.modules.account_warmup.service import tick_active_warmup_runs as tick_active_warmup_runs_service
from app.modules.assistant.ingestion import reindex_knowledge as reindex_assistant_knowledge_service
from app.modules.auth.service import expire_due_workspace_access_periods as expire_due_workspace_access_periods_service
from app.modules.billing.service import reconcile_pending_payment_orders as reconcile_pending_payment_orders_service
from app.modules.campaigns.invite_tracking import (
    refresh_due_campaign_invite_tracking_metrics as refresh_due_campaign_invite_tracking_metrics_service,
)
from app.modules.campaigns.runs import (
    tick_active_campaign_runs as tick_active_campaign_runs_service,
)
from app.modules.content_factory.service import tick_due_content_factories as tick_due_content_factories_service
from app.modules.operations.service import run_operations_monitor as run_operations_monitor_service
from app.modules.parsing.service import discover_account_channels_into_sources
from app.modules.telegram_accounts.bulk_ai_profiles import complete_bulk_ai_profiles
from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility
from app.modules.telegram_accounts.schemas import TelegramAccountCloneProfileRequest, TelegramAccountProfilePatch
from app.modules.telegram_accounts.service import (
    check_account as check_account_service,
)
from app.modules.telegram_accounts.service import (
    clone_account_profile as clone_account_profile_service,
)
from app.modules.telegram_accounts.service import (
    update_account_profile as update_account_profile_service,
)
from app.workers.celery_app import celery_app


@celery_app.task(name="assistant.reindex_knowledge")
def reindex_assistant_knowledge(source: str = "all") -> dict:
    return asyncio.run(_reindex_assistant_knowledge(source))


async def _reindex_assistant_knowledge(source: str = "all") -> dict:
    try:
        async with AsyncSessionLocal() as session:
            sources, chunks = await reindex_assistant_knowledge_service(session, source)
            await session.commit()
            return {"status": "ok", "sources": sources, "chunks": chunks}
    finally:
        await engine.dispose()


@celery_app.task
def check_telegram_account(account_id: str) -> dict:
    return asyncio.run(_check_telegram_account(account_id))


async def _check_telegram_account(account_id: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            account = await session.get(TelegramAccount, account_id)
            if not account:
                return {"account_id": account_id, "status": "not_found"}
            await check_account_service(session, account, force_restriction_check=True)
            await session.refresh(account)
            return {
                "account_id": account.id,
                "status": account.status,
                "health_score": account.health_score,
                "restriction_until_at": _restriction_metadata(account).get("until_at"),
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.refresh_telegram_account_statuses")
def refresh_telegram_account_statuses(workspace_id: str | None = None, limit: int | None = None) -> dict:
    return asyncio.run(_refresh_telegram_account_statuses(workspace_id=workspace_id, limit=limit))


async def _refresh_telegram_account_statuses(workspace_id: str | None = None, limit: int | None = None) -> dict:
    checked_ids: list[str] = []
    failed: list[dict[str, str]] = []

    try:
        async with AsyncSessionLocal() as session:
            query = (
                select(TelegramAccount)
                .where(TelegramAccount.status.notin_(["archived", "deleted"]))
                .order_by(TelegramAccount.updated_at.asc(), TelegramAccount.created_at.asc())
            )
            if workspace_id:
                query = query.where(TelegramAccount.workspace_id == workspace_id)
            if limit:
                query = query.limit(limit)

            accounts = list((await session.execute(query)).scalars().all())
            for account in accounts:
                try:
                    await check_account_service(session, account, check_restrictions=False)
                    await session.refresh(account)
                    checked_ids.append(account.id)
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    failed.append({"account_id": account.id, "error": str(exc)[:500]})

            return {
                "status": "completed",
                "mode": "workspace_filter" if workspace_id else "hourly_account_status_refresh",
                "checked": len(checked_ids),
                "failed": len(failed),
                "account_ids": checked_ids,
                "failures": failed,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.bulk_join_chats")
def bulk_join_chats(
    account_ids: list[str],
    target_refs: list[str],
    delay_seconds_min: int = 60,
    delay_seconds_max: int = 1800,
    split_folder_links: bool = True,
    request_id: str | None = None,
) -> dict:
    return asyncio.run(
        _bulk_join_chats(
            account_ids=account_ids,
            target_refs=target_refs,
            delay_seconds_min=delay_seconds_min,
            delay_seconds_max=delay_seconds_max,
            split_folder_links=split_folder_links,
            request_id=request_id,
        )
    )


async def _bulk_join_chats(
    *,
    account_ids: list[str],
    target_refs: list[str],
    delay_seconds_min: int,
    delay_seconds_max: int,
    split_folder_links: bool,
    request_id: str | None,
) -> dict:
    completed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    delay_min = max(30, int(delay_seconds_min or 60))
    delay_max = max(delay_min, int(delay_seconds_max or delay_min))
    targets = _clean_join_targets(target_refs, split_folder_links=split_folder_links)

    try:
        async with AsyncSessionLocal() as session:
            for account_id in list(dict.fromkeys(account_ids)):
                account = await session.get(TelegramAccount, account_id)
                if not account:
                    failed.append({"account_id": account_id, "error": "Аккаунт не найден"})
                    continue
                if account.status in {"archived", "deleted", "quarantined", "frozen"}:
                    skipped.append({"account_id": account.id, "reason": account.status})
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_join_chats_skipped",
                            severity="warning",
                            message=f"Вступление в чаты пропущено: статус аккаунта {account.status}.",
                            raw_error={"bulk_request_id": request_id, "status": account.status},
                        )
                    )
                    await session.commit()
                    continue
                eligibility = get_module_eligibility(account, "bulk_join_chats")
                if eligibility.status == "blocked":
                    skipped.append({"account_id": account.id, "reason": eligibility.reason or "blocked"})
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_join_chats_skipped",
                            severity="warning",
                            message=eligibility.message or "Вступление в чаты запрещено защитой аккаунта.",
                            raw_error={
                                "bulk_request_id": request_id,
                                "reason": eligibility.reason,
                                "module": "bulk_join_chats",
                            },
                        )
                    )
                    await session.commit()
                    continue

                session.add(
                    AccountHealthEvent(
                        account_id=account.id,
                        event_type="bulk_join_chats_started",
                        severity="info",
                        message=f"Запущено вступление в чаты: целей {len(targets)}, пауза {delay_min}-{delay_max} сек.",
                        raw_error={
                            "bulk_request_id": request_id,
                            "target_count": len(targets),
                            "delay_seconds_min": delay_min,
                            "delay_seconds_max": delay_max,
                        },
                    )
                )
                await session.commit()

                for index, target_ref in enumerate(targets):
                    if index > 0:
                        delay_seconds = random.randint(delay_min, delay_max)
                        session.add(
                            AccountHealthEvent(
                                account_id=account.id,
                                event_type="bulk_join_chats_delay",
                                severity="info",
                                message=f"Пауза перед следующим вступлением: {delay_seconds} сек.",
                                raw_error={
                                    "bulk_request_id": request_id,
                                    "target_ref": target_ref,
                                    "delay_seconds": delay_seconds,
                                    "position": index + 1,
                                    "target_count": len(targets),
                                },
                            )
                        )
                        await session.commit()
                        await asyncio.sleep(delay_seconds)

                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_join_chat_attempt",
                            severity="info",
                            message=f"Пробую вступить: {target_ref}",
                            raw_error={
                                "bulk_request_id": request_id,
                                "target_ref": target_ref,
                                "position": index + 1,
                                "target_count": len(targets),
                            },
                        )
                    )
                    await session.commit()
                    try:
                        access = await ensure_source_access_with_account(
                            session,
                            account,
                            target_ref,
                            join_if_needed=True,
                            join_discussion=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - keep the bulk job moving.
                        await session.rollback()
                        error_text = f"{type(exc).__name__}: {exc}"
                        failed.append({"account_id": account.id, "target_ref": target_ref, "error": error_text})
                        session.add(
                            AccountHealthEvent(
                                account_id=account.id,
                                event_type="bulk_join_chat_failed",
                                severity="error",
                                message=f"Не удалось вступить: {target_ref}",
                                raw_error={
                                    "bulk_request_id": request_id,
                                    "target_ref": target_ref,
                                    "error": error_text,
                                    "captcha_note": (
                                        "Если Telegram запросил нестандартную проверку, нужно ручное подтверждение."
                                    ),
                                },
                            )
                        )
                        await session.commit()
                        continue

                    completed.append({"account_id": account.id, "target_ref": target_ref})
                    captcha = access.metadata.get("join_captcha") if isinstance(access.metadata, dict) else None
                    captcha_solved = int(captcha.get("solved", 0)) if isinstance(captcha, dict) else 0
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_join_chat_completed",
                            severity="info",
                            message=(
                                f"Вступление выполнено: {target_ref}"
                                if captcha_solved <= 0
                                else f"Вступление выполнено: {target_ref}; простая проверка обработана."
                            ),
                            raw_error={
                                "bulk_request_id": request_id,
                                "target_ref": target_ref,
                                "joined": access.joined,
                                "join_attempted": access.join_attempted,
                                "captcha": captcha,
                            },
                        )
                    )
                    await session.commit()

                session.add(
                    AccountHealthEvent(
                        account_id=account.id,
                        event_type="bulk_join_chats_completed",
                        severity="info",
                        message="Фоновая задача вступления в чаты завершена для аккаунта.",
                        raw_error={"bulk_request_id": request_id},
                    )
                )
                await session.commit()

            return {
                "status": "completed",
                "request_id": request_id,
                "requested_accounts": len(set(account_ids)),
                "targets": len(targets),
                "completed": len(completed),
                "skipped": len(skipped),
                "failed": len(failed),
                "completed_items": completed,
                "skipped_items": skipped,
                "failed_items": failed,
            }
    finally:
        await engine.dispose()


def _clean_join_targets(target_refs: list[str], *, split_folder_links: bool) -> list[str]:
    targets: list[str] = []
    for raw_ref in target_refs:
        value = str(raw_ref or "").strip()
        if not value:
            continue
        if split_folder_links and ("t.me/addlist/" in value or "telegram.me/addlist/" in value):
            targets.append(value)
            continue
        targets.append(value)
    return list(dict.fromkeys(targets))


@celery_app.task(name="app.workers.tasks.refresh_campaign_invite_tracking_metrics")
def refresh_campaign_invite_tracking_metrics(workspace_id: str | None = None, limit: int = 20) -> dict:
    return asyncio.run(_refresh_campaign_invite_tracking_metrics(workspace_id=workspace_id, limit=limit))


async def _refresh_campaign_invite_tracking_metrics(workspace_id: str | None = None, limit: int = 20) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            result = await refresh_due_campaign_invite_tracking_metrics_service(
                session,
                workspace_id=workspace_id,
                limit=limit,
            )
            await session.commit()
            return {"status": "completed", **result}
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.update_account_profile")
def update_account_profile(
    account_id: str,
    payload_data: dict[str, Any],
    request_id: str | None = None,
) -> dict:
    return asyncio.run(
        _update_account_profile(
            account_id=account_id,
            payload_data=payload_data,
            request_id=request_id,
        )
    )


async def _update_account_profile(
    *,
    account_id: str,
    payload_data: dict[str, Any],
    request_id: str | None,
) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            account = await session.get(TelegramAccount, account_id)
            if not account:
                return {"account_id": account_id, "status": "not_found"}

            try:
                profile = await update_account_profile_service(
                    session,
                    account,
                    TelegramAccountProfilePatch(**payload_data),
                )
            except Exception as exc:  # noqa: BLE001 - worker must record the account-level failure.
                await session.rollback()
                session.add(
                    AccountHealthEvent(
                        account_id=account.id,
                        event_type="account_profile_update_failed",
                        severity="error",
                        message="Не удалось обновить профиль Telegram в фоновой задаче.",
                        raw_error={
                            "request_id": request_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
                await session.commit()
                return {"account_id": account.id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

            session.add(
                AccountHealthEvent(
                    account_id=account.id,
                    event_type="account_profile_update_completed",
                    severity="info",
                    message="Профиль Telegram обновлен в фоновой задаче.",
                    raw_error={
                        "request_id": request_id,
                        "display_name": profile.display_name,
                    },
                )
            )
            await session.commit()
            return {"account_id": account.id, "status": "completed", "display_name": profile.display_name}
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.bulk_clone_profile")
def bulk_clone_profile(
    account_ids: list[str],
    source_ref: str | None = None,
    source_account_id: str | None = None,
    copy_name: bool = True,
    copy_bio: bool = True,
    copy_photo: bool = True,
    copy_stories: bool = False,
    pin_stories: bool = False,
    delete_old_stories: bool = False,
    story_limit: int = 3,
) -> dict:
    return asyncio.run(
        _bulk_clone_profile(
            account_ids=account_ids,
            source_ref=source_ref,
            source_account_id=source_account_id,
            copy_name=copy_name,
            copy_bio=copy_bio,
            copy_photo=copy_photo,
            copy_stories=copy_stories,
            pin_stories=pin_stories,
            delete_old_stories=delete_old_stories,
            story_limit=story_limit,
        )
    )


async def _bulk_clone_profile(
    *,
    account_ids: list[str],
    source_ref: str | None,
    source_account_id: str | None,
    copy_name: bool,
    copy_bio: bool,
    copy_photo: bool,
    copy_stories: bool,
    pin_stories: bool,
    delete_old_stories: bool,
    story_limit: int,
) -> dict:
    completed: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    try:
        async with AsyncSessionLocal() as session:
            source_account = await session.get(TelegramAccount, source_account_id) if source_account_id else None
            limit = max(1, min(int(story_limit or 3), 20))
            for account_id in list(dict.fromkeys(account_ids)):
                account = await session.get(TelegramAccount, account_id)
                if not account:
                    failed.append({"account_id": account_id, "error": "Аккаунт не найден"})
                    continue
                if source_account and source_account.id == account.id:
                    skipped.append({"account_id": account.id, "reason": "Источник и цель совпадают"})
                    continue

                payload = TelegramAccountCloneProfileRequest(
                    source_ref=source_ref,
                    source_account_id=source_account_id,
                    copy_name=copy_name,
                    copy_bio=copy_bio,
                    copy_photo=copy_photo,
                    copy_stories=copy_stories,
                    pin_stories=pin_stories,
                    delete_old_stories=delete_old_stories,
                    story_limit=limit,
                )
                try:
                    result = await clone_account_profile_service(
                        session,
                        account,
                        payload,
                        source_account=source_account,
                    )
                except Exception as exc:  # noqa: BLE001 - keep worker alive for the next account.
                    await session.rollback()
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_clone_profile_failed",
                            severity="error",
                            message="Не удалось скопировать профиль.",
                            raw_error={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
                    await session.commit()
                    failed.append({"account_id": account.id, "error": f"{type(exc).__name__}: {exc}"})
                    continue

                if result.copied_fields:
                    completed.append(account.id)
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_clone_profile_completed",
                            severity="info",
                            message="Профиль скопирован в фоновой задаче.",
                            raw_error={
                                "copied_fields": result.copied_fields,
                                "skipped_fields": result.skipped_fields,
                                "warnings": result.warnings,
                            },
                        )
                    )
                    await session.commit()
                else:
                    reason_parts = result.warnings or result.skipped_fields or ["Нечего копировать"]
                    skipped.append({"account_id": account.id, "reason": "; ".join(reason_parts)})

            return {
                "status": "completed",
                "requested": len(set(account_ids)),
                "completed": len(completed),
                "skipped": len(skipped),
                "failed": len(failed),
                "completed_account_ids": completed,
                "skipped_accounts": skipped,
                "failed_accounts": failed,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.bulk_copy_stories")
def bulk_copy_stories(
    account_ids: list[str],
    source_ref: str | None = None,
    source_account_id: str | None = None,
    story_limit: int = 10,
    pin_on_top: bool = False,
    delete_old_stories: bool = False,
) -> dict:
    return asyncio.run(
        _bulk_copy_stories(
            account_ids=account_ids,
            source_ref=source_ref,
            source_account_id=source_account_id,
            story_limit=story_limit,
            pin_on_top=pin_on_top,
            delete_old_stories=delete_old_stories,
        )
    )


async def _bulk_copy_stories(
    *,
    account_ids: list[str],
    source_ref: str | None,
    source_account_id: str | None,
    story_limit: int,
    pin_on_top: bool,
    delete_old_stories: bool,
) -> dict:
    completed: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    try:
        async with AsyncSessionLocal() as session:
            source_account = await session.get(TelegramAccount, source_account_id) if source_account_id else None
            limit = max(1, min(int(story_limit or 10), 20))
            for account_id in list(dict.fromkeys(account_ids)):
                account = await session.get(TelegramAccount, account_id)
                if not account:
                    failed.append({"account_id": account_id, "error": "Аккаунт не найден"})
                    continue
                if source_account and source_account.id == account.id:
                    skipped.append({"account_id": account.id, "reason": "Источник и цель совпадают"})
                    continue

                payload = TelegramAccountCloneProfileRequest(
                    source_ref=source_ref,
                    source_account_id=source_account_id,
                    copy_name=False,
                    copy_bio=False,
                    copy_photo=False,
                    copy_stories=True,
                    pin_stories=pin_on_top,
                    delete_old_stories=delete_old_stories,
                    story_limit=limit,
                )
                try:
                    result = await clone_account_profile_service(
                        session,
                        account,
                        payload,
                        source_account=source_account,
                    )
                except Exception as exc:  # noqa: BLE001 - keep worker alive for the next account.
                    await session.rollback()
                    session.add(
                        AccountHealthEvent(
                            account_id=account.id,
                            event_type="bulk_copy_stories_failed",
                            severity="error",
                            message="Не удалось скопировать сторис.",
                            raw_error={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
                    await session.commit()
                    failed.append({"account_id": account.id, "error": f"{type(exc).__name__}: {exc}"})
                    continue

                if "stories" in result.copied_fields:
                    completed.append(account.id)
                else:
                    reason_parts = result.warnings or result.skipped_fields or ["Сторис не найдены"]
                    skipped.append({"account_id": account.id, "reason": "; ".join(reason_parts)})

            return {
                "status": "completed",
                "requested": len(set(account_ids)),
                "completed": len(completed),
                "skipped": len(skipped),
                "failed": len(failed),
                "completed_account_ids": completed,
                "skipped_accounts": skipped,
                "failed_accounts": failed,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.bulk_ai_profiles")
def bulk_ai_profiles(
    workspace_id: str,
    account_ids: list[str],
    request_id: str,
    payload_data: dict,
) -> dict:
    return asyncio.run(
        _bulk_ai_profiles(
            workspace_id=workspace_id,
            account_ids=account_ids,
            request_id=request_id,
            payload_data=payload_data,
        )
    )


async def _bulk_ai_profiles(
    *,
    workspace_id: str,
    account_ids: list[str],
    request_id: str,
    payload_data: dict,
) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            return await complete_bulk_ai_profiles(
                session,
                workspace_id=workspace_id,
                account_ids=account_ids,
                request_id=request_id,
                payload_data=payload_data,
            )
    finally:
        await engine.dispose()


@celery_app.task
def recheck_due_spam_blocked_accounts(account_id: str | None = None, limit: int = 20) -> dict:
    return {
        "status": "disabled",
        "reason": "active_restriction_probe_disabled",
        "account_id": account_id,
        "limit": limit,
    }


async def _recheck_due_spam_blocked_accounts(account_id: str | None = None, limit: int = 20) -> dict:
    return {
        "status": "disabled",
        "reason": "active_restriction_probe_disabled",
        "account_id": account_id,
        "limit": limit,
    }


def _restriction_metadata(account: TelegramAccount) -> dict[str, Any]:
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    metadata = data.get("restriction_status")
    return metadata if isinstance(metadata, dict) else {}


@celery_app.task
def run_warmup_step(step_id: str) -> dict:
    return {"step_id": step_id, "status": "queued"}


@celery_app.task
def tick_active_warmup_runs(workspace_id: str | None = None, limit: int = 20) -> dict:
    return asyncio.run(_tick_active_warmup_runs(workspace_id, limit))


async def _tick_active_warmup_runs(workspace_id: str | None, limit: int) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            runs = await tick_active_warmup_runs_service(session, workspace_id, limit=limit)
            await session.commit()
            return {
                "status": "completed",
                "mode": "workspace_filter" if workspace_id else "scheduled_safety_scan",
                "runs_ticked": len(runs),
                "run_ids": [run.id for run in runs],
            }
    finally:
        await engine.dispose()


@celery_app.task
def parse_source(source_id: str) -> dict:
    return {"source_id": source_id, "status": "queued"}


@celery_app.task(name="app.workers.tasks.discover_account_channels")
def discover_account_channels(account_id: str) -> dict:
    return asyncio.run(_discover_account_channels(account_id))


async def _discover_account_channels(account_id: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            account = await session.get(TelegramAccount, account_id)
            if not account:
                return {"account_id": account_id, "status": "not_found"}
            if account.status in {"archived", "deleted"}:
                return {"account_id": account.id, "status": "skipped", "reason": account.status}

            try:
                result = await discover_account_channels_into_sources(session, account)
            except Exception as exc:  # noqa: BLE001 - discovery should not break account import.
                await session.rollback()
                session.add(
                    AccountHealthEvent(
                        account_id=account.id,
                        event_type="account_channel_discovery_failed",
                        severity="warning",
                        message="Не удалось автоматически собрать каналы из Telegram аккаунта.",
                        raw_error={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
                await session.commit()
                return {"account_id": account.id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

            session.add(
                AccountHealthEvent(
                    account_id=account.id,
                    event_type="account_channel_discovery_completed",
                    severity="info",
                    message="Каналы из Telegram аккаунта автоматически добавлены в базу источников.",
                    raw_error=result,
                )
            )
            await session.commit()
            return result
    finally:
        await engine.dispose()


@celery_app.task
def generate_ai_comment(generation_id: str) -> dict:
    return {"generation_id": generation_id, "status": "queued"}


@celery_app.task
def run_comment_action(action_id: str) -> dict:
    return {"action_id": action_id, "status": "queued"}


@celery_app.task
def tick_active_campaign_runs(workspace_id: str | None = None, limit: int = 20) -> dict:
    return asyncio.run(_tick_active_campaign_runs(workspace_id, limit))


async def _tick_active_campaign_runs(workspace_id: str | None, limit: int) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            runs = await tick_active_campaign_runs_service(session, workspace_id, limit=limit)
            from app.modules.contact_broadcasts.scheduling import queue_next_tick

            for run in runs:
                if (run.report or {}).get("recipient_mode") == "saved_contacts":
                    await queue_next_tick(session, run)
            return {
                "status": "completed",
                "mode": "workspace_filter" if workspace_id else "scheduled_safety_scan",
                "runs_ticked": len(runs),
                "run_ids": [run.id for run in runs],
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.tick_due_content_factories")
def tick_due_content_factories(workspace_id: str | None = None, limit: int = 10) -> dict:
    return asyncio.run(_tick_due_content_factories(workspace_id, limit))


async def _tick_due_content_factories(workspace_id: str | None, limit: int) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            runs = await tick_due_content_factories_service(session, workspace_id=workspace_id, limit=limit)
            return {
                "status": "completed",
                "mode": "workspace_filter" if workspace_id else "scheduled_content_factory_tick",
                "runs_started": len(runs),
                "run_ids": [run.id for run in runs],
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.run_operations_monitor", soft_time_limit=60, time_limit=90)
def run_operations_monitor() -> dict:
    return asyncio.run(_run_operations_monitor())


async def _run_operations_monitor() -> dict:
    try:
        async with AsyncSessionLocal() as session:
            return await run_operations_monitor_service(session)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.expire_due_workspace_access_periods")
def expire_due_workspace_access_periods(limit: int = 500) -> dict:
    return asyncio.run(_expire_due_workspace_access_periods(limit))


async def _expire_due_workspace_access_periods(limit: int) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            workspace_ids = await expire_due_workspace_access_periods_service(session, limit=limit)
            await session.commit()
            return {
                "status": "completed",
                "expired": len(workspace_ids),
                "workspace_ids": workspace_ids,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.reconcile_pending_payment_orders")
def reconcile_pending_payment_orders(limit: int = 100) -> dict:
    return asyncio.run(_reconcile_pending_payment_orders(limit))


async def _reconcile_pending_payment_orders(limit: int) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            result = await reconcile_pending_payment_orders_service(session, limit=limit)
            await session.commit()
            return {"status": "completed", **result}
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.tick_contact_broadcast")
def tick_contact_broadcast(run_id: str, token: str) -> dict:
    return asyncio.run(_tick_contact_broadcast(run_id, token))


async def _tick_contact_broadcast(run_id: str, token: str) -> dict:
    from app.modules.campaigns.models import CampaignRun
    from app.modules.contact_broadcasts.runner import tick
    from app.modules.contact_broadcasts.scheduling import queue_next_tick

    try:
        async with AsyncSessionLocal() as session:
            run = (
                await session.execute(select(CampaignRun).where(CampaignRun.id == run_id).with_for_update())
            ).scalar_one_or_none()
            if not run or run.status not in {"pending", "running"} or (run.progress or {}).get("tick_token") != token:
                return {"status": "inactive_or_stale"}
            run.progress = {**(run.progress or {}), "tick_token": None, "tick_scheduled_for": None}
            await session.commit()
            await tick(session, run.workspace_id, run)
            await queue_next_tick(session, run)
            return {"status": run.status}
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.tasks.tick_campaign_account_replacements")
def tick_campaign_account_replacements(limit: int = 20) -> dict:
    return asyncio.run(_tick_campaign_account_replacements(limit))


async def _tick_campaign_account_replacements(limit: int) -> dict:
    from app.modules.telegram_gateway.service import real_telegram_enabled

    from app.core.config import get_settings
    from app.modules.campaigns.account_replacement import replacement_pending, tick_replacement
    from app.modules.campaigns.models import Campaign

    if not real_telegram_enabled() or not get_settings().telegram_operations_enabled:
        return {"processed": 0, "reason": "telegram_operations_disabled"}
    async with AsyncSessionLocal() as session:
        campaigns = list(
            (
                await session.scalars(
                    select(Campaign)
                    .where(
                        Campaign.type == "comment_posts",
                        Campaign.status == "paused",
                    )
                    .order_by(Campaign.updated_at)
                )
            ).all()
        )
        ids = [c.id for c in campaigns if replacement_pending(c)][:limit]
        for campaign_id in ids:
            await tick_replacement(session, campaign_id)
        return {"processed": len(ids)}
