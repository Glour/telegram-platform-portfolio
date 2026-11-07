"""Durable, operator-requested source preparation when a comment account changes.

No network in the API. Each tick performs at most one channel/discussion membership
operation. An interrupted attempt is never replayed automatically.
"""

from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from app.modules.telegram_gateway.service import (
    _normalize_source_ref,
    real_telegram_enabled,
    telegram_client_for_account,
)
from fastapi import HTTPException
from sqlalchemy import func, select, text
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest

from app.core.config import get_settings
from app.modules.campaigns.models import Campaign, CampaignAction, CampaignRun
from app.modules.proxies.models import Proxy
from app.modules.sources.models import TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.risk import get_module_eligibility
from app.modules.telegram_safety.admission import replacement_pacing_scope
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
from app.modules.workspaces.models import Workspace

ACTIVE = {"queued", "joining", "waiting_account", "needs_review"}
HARD_STATUSES = {"invalid", "banned", "blocked", "frozen", "spam_blocked", "quarantined", "deleted", "archived"}


def now():
    return datetime.now(UTC)


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def replacement_pending(campaign):
    return (campaign.config or {}).get("account_replacement", {}).get("status") in ACTIVE


def replacement_blocks_launch(campaign):
    data = (campaign.config or {}).get("account_replacement", {})
    return replacement_pending(campaign) or (
        data.get("status") == "cancelled" and any(j.get("status") != "joined" for j in data.get("jobs", []))
    )


def _save(campaign, replacement):
    campaign.config = {**campaign.config, "account_replacement": deepcopy(replacement)}


def admission_wait_reason(account, state):
    from app.modules.telegram_safety.service import _owner_admission_valid

    if not state or state.manual_hold:
        return "Ожидает допуска: " + (state.reason or "ручная проверка" if state else "ручная проверка")
    if not state.operator_approved_at:
        return "Нужен операторский допуск к Telegram."
    if _owner_admission_valid(state, account):
        return None
    if state.restriction_status != "clear":
        return "Нет действующего допуска к Telegram."
    checked = (account.daily_limits or {}).get("restriction_status", {}).get("checked_at")
    if (
        not state.restriction_checked_at
        or aware(state.restriction_checked_at) < now() - timedelta(hours=24)
        or not checked
        or not now() - timedelta(hours=24) <= aware(checked) <= now()
    ):
        return "Последний допуск устарел; требуется обновление статуса аккаунта."
    return None


async def replacement_accounts(session, campaign):
    """All workspace accounts, including reasons instead of silently hiding rows."""
    accounts = list(
        (
            await session.scalars(
                select(TelegramAccount)
                .where(
                    TelegramAccount.workspace_id == campaign.workspace_id,
                    TelegramAccount.status.notin_(["deleted", "archived"]),
                )
                .order_by(TelegramAccount.created_at, TelegramAccount.id)
            )
        ).all()
    )
    others = list(
        (
            await session.scalars(
                select(Campaign).where(
                    Campaign.workspace_id == campaign.workspace_id,
                    Campaign.type == "comment_posts",
                    Campaign.id != campaign.id,
                    Campaign.status.notin_(["deleted", "archived"]),
                )
            )
        ).all()
    )
    states = {
        state.account_id: state
        for state in (
            await session.scalars(
                select(TelegramSafetyState).where(TelegramSafetyState.account_id.in_([a.id for a in accounts]))
            )
        ).all()
    }
    proxies = {
        proxy.id: proxy
        for proxy in (
            await session.scalars(
                select(Proxy).where(
                    Proxy.workspace_id == campaign.workspace_id,
                    Proxy.id.in_([a.proxy_id for a in accounts if a.proxy_id]),
                )
            )
        ).all()
    }
    proxy_counts = Counter(a.proxy_id for a in accounts if a.proxy_id)
    result = []
    for account in accounts:
        current = account.id in campaign.config.get("account_ids", [])
        used = [c.name for c in others if account.id in c.config.get("account_ids", [])]
        hard = account.status in HARD_STATUSES or account.restriction_status in {"limited", "blocked", "frozen"}
        eligibility = get_module_eligibility(
            account,
            "auto_join",
            proxy=proxies.get(account.proxy_id),
            proxy_account_count=proxy_counts.get(account.proxy_id, 0),
        )
        comment_eligibility = get_module_eligibility(
            account,
            "neurocommenting",
            proxy=proxies.get(account.proxy_id),
            proxy_account_count=proxy_counts.get(account.proxy_id, 0),
        )
        state = states.get(account.id)
        admission_reason = admission_wait_reason(account, state)
        reason = (
            "Текущий аккаунт кампании"
            if current
            else "Закреплён за кампанией: " + ", ".join(used)
            if used
            else "Аккаунт недоступен: " + (account.restriction_label or account.status)
            if hard
            else "Нужен рабочий прокси"
            if not account.proxy_id or account.proxy_id not in proxies
            else admission_reason
            if admission_reason
            else "Ожидает допуска: " + (eligibility.message or eligibility.reason or "проверка")
            if eligibility.status == "blocked"
            else "Ожидает допуска к комментариям: "
            + (comment_eligibility.message or comment_eligibility.reason or "проверка")
            if comment_eligibility.status == "blocked"
            else "Ожидает допуска к Telegram"
            if not state
            else "Готов к подготовке источников"
        )
        result.append(
            {
                "id": account.id,
                "phone": account.phone,
                "username": account.username,
                "display_name": account.display_name,
                "status": account.status,
                "can_select": not (current or used or hard),
                "ready": not (
                    current
                    or used
                    or hard
                    or not account.proxy_id
                    or account.proxy_id not in proxies
                    or not state
                    or admission_reason
                    or eligibility.status == "blocked"
                    or comment_eligibility.status == "blocked"
                ),
                "reason": reason,
            }
        )
    return result


async def request_replacement(session, campaign, payload, actor_id):
    workspace = await session.get(Workspace, campaign.workspace_id)
    if not workspace or workspace.owner_user_id != actor_id:
        raise HTTPException(403, "Замена аккаунта доступна владельцу workspace.")
    if campaign.status in {"archived", "deleted"}:
        raise HTTPException(409, "Архивная кампания не может менять аккаунты.")
    if campaign.type != "comment_posts":
        raise HTTPException(400, "Замена с подготовкой источников доступна в нейрокомментинге.")
    # Serialize against the isolated controller, without waiting on a Telegram RPC.
    if session.bind.dialect.name == "postgresql":
        acquired = await session.scalar(text("SELECT pg_try_advisory_xact_lock(604102026)"))
        if not acquired:
            raise HTTPException(409, "Сейчас завершается действие кампании. Повторите замену после его завершения.")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int.from_bytes(sha256(campaign.workspace_id.encode()).digest()[:7], "big")},
        )
    runs = list(
        (
            await session.scalars(
                select(CampaignRun)
                .where(
                    CampaignRun.campaign_id == campaign.id,
                )
                .order_by(CampaignRun.created_at.desc())
                .with_for_update()
            )
        ).all()
    )
    await session.refresh(campaign, with_for_update=True)
    previous = deepcopy(campaign.config.get("account_replacement", {}))
    if previous.get("request_id") == payload.request_id:
        if (
            previous.get("old_account_id") != payload.old_account_id
            or previous.get("new_account_id") != payload.new_account_id
        ):
            raise HTTPException(409, "Этот идентификатор замены уже использован для другой пары аккаунтов.")
        return campaign
    if previous.get("status") in ACTIVE and previous.get("status") != "needs_review":
        raise HTTPException(409, "Замена уже выполняется. Дождитесь результата или отмените подготовку.")
    if any(j.get("status") == "attempting" for j in previous.get("jobs", [])):
        raise HTTPException(409, "Исход предыдущего вступления неизвестен; требуется сверка результата.")
    old_ids = list(campaign.config.get("account_ids", []))
    if payload.old_account_id not in old_ids or payload.new_account_id in old_ids:
        raise HTTPException(409, "Состав аккаунтов изменился. Откройте кампанию заново.")
    candidates = await replacement_accounts(session, campaign)
    candidate = next((a for a in candidates if a["id"] == payload.new_account_id), None)
    if not candidate or not candidate["can_select"]:
        raise HTTPException(400, candidate["reason"] if candidate else "Аккаунт не найден в этом workspace.")
    # Wait for normal runners' transaction locks before freezing their progress.
    account_ids = [payload.old_account_id, payload.new_account_id]
    inflight = await session.scalar(
        select(TelegramOperation.id)
        .where(
            TelegramOperation.account_id.in_(account_ids),
            TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
        )
        .limit(1)
    )
    if inflight:
        raise HTTPException(
            409, "У аккаунта есть незавершённая операция Telegram. Сначала нужно получить её результат."
        )
    source_ids = list(campaign.config.get("source_ids", []))
    assignment = deepcopy(campaign.config.get("source_assignment", {}))
    mode = campaign.config.get("source_assignment_mode", "standard")
    assigned_sources = list(assignment.get(payload.old_account_id, [])) if mode != "standard" else source_ids
    assigned_sources = list(dict.fromkeys(s for s in assigned_sources if s in source_ids))
    sources = list(
        (
            await session.scalars(
                select(TelegramSource)
                .where(
                    TelegramSource.workspace_id == campaign.workspace_id,
                    TelegramSource.id.in_(assigned_sources),
                )
                .with_for_update()
            )
        ).all()
    )
    if len(sources) != len(assigned_sources):
        raise HTTPException(409, "Не все закреплённые источники найдены в workspace.")
    at = now()
    for run in runs:
        if run.status in {"pending", "running", "stopping"}:
            run.status = "cancelled"
            run.finished_at = at
            run.requested_stop_at = at
            run.report = {**(run.report or {}), "status": "cancelled", "stop_reason": "account_replacement"}
    # Old drafts/actions must never be silently sent with a different identity.
    queued = list(
        (
            await session.scalars(
                select(CampaignAction).where(
                    CampaignAction.campaign_id == campaign.id,
                    CampaignAction.telegram_account_id == payload.old_account_id,
                    CampaignAction.status == "queued",
                )
            )
        ).all()
    )
    for action in queued:
        action.status = "cancelled"
        action.error_code = "ACCOUNT_REPLACED"
    new_ids = [payload.new_account_id if a == payload.old_account_id else a for a in old_ids]
    if payload.old_account_id in assignment:
        assignment[payload.new_account_id] = assignment.pop(payload.old_account_id)
    jobs = [{"id": str(uuid4()), "source_id": sid, "kind": "channel", "status": "queued"} for sid in assigned_sources]
    history = deepcopy(campaign.config.get("account_replacement_history", []))
    if previous:
        history.append(previous)
    replacement = {
        "id": str(uuid4()),
        "request_id": payload.request_id,
        "workspace_id": campaign.workspace_id,
        "campaign_id": campaign.id,
        "actor_id": actor_id,
        "old_account_id": payload.old_account_id,
        "new_account_id": payload.new_account_id,
        "source_ids": assigned_sources,
        "all_source_ids": source_ids,
        "account_ids": new_ids,
        "status": "queued" if candidate["ready"] else "waiting_account",
        "wait_reason": None if candidate["ready"] else candidate["reason"],
        "join_interval_seconds": payload.join_interval_seconds,
        "resume_when_ready": payload.resume_when_ready,
        "requested_at": at.isoformat(),
        "jobs": jobs,
        "next_join_at": at.isoformat(),
        "previous_run_progress": deepcopy(runs[0].progress if runs else {}),
        "lineage_account_ids": list(
            dict.fromkeys(previous.get("lineage_account_ids", []) + [payload.old_account_id, payload.new_account_id])
        ),
    }
    predecessor = await session.get(TelegramAccount, payload.old_account_id)
    old_attempts = (predecessor.daily_limits or {}).get("comment_send_attempts_24h", []) if predecessor else []
    receipts = list(
        (
            await session.scalars(
                select(TelegramOperation.created_at)
                .where(
                    TelegramOperation.account_id == payload.old_account_id,
                    TelegramOperation.method.in_(
                        ["SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest", "ForwardMessagesRequest"]
                    ),
                    TelegramOperation.created_at > at - timedelta(hours=24),
                )
                .order_by(TelegramOperation.created_at)
            )
        ).all()
    )
    predecessor_times = [
        value for value in old_attempts if isinstance(value, str) and aware(value) > at - timedelta(hours=24)
    ]
    if len(receipts) > len(predecessor_times):
        predecessor_times = [aware(value).isoformat() for value in receipts]
    inherited = deepcopy(campaign.config.get("replacement_comment_attempts", []))
    inherited = [
        entry for entry in inherited if not isinstance(entry, dict) or entry.get("account_id") != payload.old_account_id
    ]
    inherited += [{"account_id": payload.old_account_id, "at": value} for value in predecessor_times]
    last_receipt = await session.scalar(
        select(func.max(TelegramOperation.created_at)).where(
            TelegramOperation.account_id == payload.old_account_id,
            TelegramOperation.method.in_(["SendMessageRequest", "SendMediaRequest"]),
            TelegramOperation.outcome == "accepted",
        )
    )
    last_send = campaign.config.get("replacement_last_send_at")
    if last_receipt and (not last_send or aware(last_receipt) > aware(last_send)):
        last_send = aware(last_receipt).isoformat()
    config = {
        **campaign.config,
        "replacement_comment_attempts": inherited,
        "replacement_last_send_at": last_send,
        "account_ids": new_ids,
        "source_assignment": assignment,
        "account_replacement": replacement,
        "account_replacement_history": history,
    }
    if "controlled_launch" in config:
        config["controlled_launch"] = {**config["controlled_launch"], "status": "account_replacement"}
    campaign.config = config
    campaign.status = "paused"
    for source in sources:
        metadata = deepcopy(source.source_metadata or {})
        bindings = dict(metadata.get("campaign_account_assignments", {}))
        bindings[campaign.id] = {
            "account_id": payload.new_account_id,
            "replacement_id": replacement["id"],
            "status": "preparing",
        }
        metadata["campaign_account_assignments"] = bindings
        for key, binding in metadata.get("demo_launch_assignments", {}).items():
            if binding.get("campaign_id") == campaign.id:
                metadata["demo_launch_assignments"][key] = {
                    **binding,
                    "account_id": payload.new_account_id,
                    "channel_membership": "pending",
                    "send_ready": False,
                    "replacement_id": replacement["id"],
                }
        source.source_metadata = metadata
    await session.commit()
    return campaign


@asynccontextmanager
async def queue_lock(session, campaign_id):
    """Session-level lease survives the commit before the external RPC."""
    if session.bind.dialect.name != "postgresql":
        yield True
        return
    async with session.bind.connect() as connection:
        key = int.from_bytes(sha256(("replacement:" + campaign_id).encode()).digest()[:7], "big")
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.commit()


async def local_budget_retry_at(session, account_id, code, at):
    """Wait for the actual rolling budget reset, without repeatedly opening Telegram."""
    state = await session.get(TelegramSafetyState, account_id, populate_existing=True)
    query = select(TelegramOperation.created_at).where(
        TelegramOperation.account_id == account_id,
        TelegramOperation.created_at >= at - timedelta(hours=24),
    )
    if code == "SAFETY_ATTEMPT_BUDGET":
        query = query.where(TelegramOperation.outbound.is_(True))
        limit = state.outbound_attempt_limit
    elif code == "SAFETY_RPC_BUDGET":
        query = query.where(TelegramOperation.method.not_like("operator.%"))
        limit = state.rpc_attempt_limit
    else:
        return at + timedelta(seconds=60)
    oldest = await session.scalar(
        query.order_by(TelegramOperation.created_at.desc()).offset(max(0, limit - 1)).limit(1)
    )
    return (
        max(at + timedelta(seconds=1), aware(oldest) + timedelta(hours=24, seconds=1))
        if oldest
        else at + timedelta(seconds=1)
    )


async def tick_replacement(session, campaign_id):
    if not real_telegram_enabled() or not get_settings().telegram_operations_enabled:
        return
    async with queue_lock(session, campaign_id) as acquired:
        if not acquired:
            return
        campaign = await session.get(Campaign, campaign_id, populate_existing=True)
        if not campaign or campaign.status != "paused" or not replacement_pending(campaign):
            return
        replacement = deepcopy(campaign.config["account_replacement"])
        if replacement["status"] == "needs_review":
            return
        if campaign.config.get("account_ids") != replacement["account_ids"] or set(
            campaign.config.get("source_ids", [])
        ) != (set(replacement["all_source_ids"]) - set(campaign.config.get("blocked_sources", {}))):
            replacement.update(status="needs_review", wait_reason="Состав кампании изменился во время подготовки.")
            _save(campaign, replacement)
            await session.commit()
            return
        if any(j["status"] == "attempting" for j in replacement["jobs"]):
            replacement.update(
                status="needs_review",
                wait_reason="Вступление было прервано. Исход требует сверки; повтор не выполняется.",
            )
            _save(campaign, replacement)
            await session.commit()
            return
        # 'current' is expected now; inspect actual admission independently.
        account = await session.get(TelegramAccount, replacement["new_account_id"], populate_existing=True)
        state = await session.get(TelegramSafetyState, account.id) if account else None
        reason = None
        if not account or account.workspace_id != campaign.workspace_id:
            reason = "Аккаунт не найден в workspace."
        elif not account.proxy_id:
            reason = "Нужен прокси."
        elif admission_wait_reason(account, state):
            reason = admission_wait_reason(account, state)
        else:
            proxy = await session.get(Proxy, account.proxy_id)
            proxy_count = await session.scalar(
                select(func.count())
                .select_from(TelegramAccount)
                .where(
                    TelegramAccount.workspace_id == campaign.workspace_id,
                    TelegramAccount.proxy_id == account.proxy_id,
                    TelegramAccount.status.notin_(["archived", "deleted"]),
                )
            )
            if not proxy or proxy.workspace_id != campaign.workspace_id:
                reason = "Назначенный прокси не найден в workspace."
            for module in ("auto_join", "neurocommenting"):
                eligibility = get_module_eligibility(account, module, proxy=proxy, proxy_account_count=proxy_count)
                if eligibility.status == "blocked":
                    reason = eligibility.message or eligibility.reason
                    break
        if reason:
            replacement.update(status="waiting_account", wait_reason=reason)
            _save(campaign, replacement)
            await session.commit()
            return
        if replacement.get("status") == "waiting_account" and replacement.get("wait_reason", "").startswith(
            "Ожидание доступного бюджета"
        ):
            if now() < aware(replacement["next_join_at"]):
                return
        replacement.update(status="joining", wait_reason=None)
        job = next((j for j in replacement["jobs"] if j["status"] == "queued"), None)
        if job is None:
            await _finish(session, campaign, replacement)
            return
        at = now()
        operations = list(
            (
                await session.scalars(
                    select(TelegramOperation)
                    .where(
                        TelegramOperation.account_id == account.id,
                        TelegramOperation.method == "JoinChannelRequest",
                        TelegramOperation.created_at > at - timedelta(hours=1),
                    )
                    .order_by(TelegramOperation.created_at)
                )
            ).all()
        )
        interval = replacement["join_interval_seconds"]
        deadline = aware(replacement["next_join_at"])
        if operations:
            deadline = max(deadline, aware(operations[-1].created_at) + timedelta(seconds=interval))
        hour_limit = max(1, (3600 + interval - 1) // interval)
        if len(operations) >= hour_limit:
            deadline = max(deadline, aware(operations[-hour_limit].created_at) + timedelta(hours=1, seconds=1))
        if at < deadline or state.wait_until and aware(state.wait_until) > at:
            replacement["next_join_at"] = max(
                deadline, aware(state.wait_until) if state.wait_until else deadline
            ).isoformat()
            _save(campaign, replacement)
            await session.commit()
            return
        # This scoped pacing is used only inside this queue's transport context.
        pacing = {
            "replacement_id": replacement["id"],
            "campaign_id": campaign.id,
            "account_id": account.id,
            "workspace_id": campaign.workspace_id,
            "actor_id": replacement["actor_id"],
            "min_interval_seconds": interval,
            "approved_at": at.isoformat(),
            "expires_at": (at + timedelta(hours=1)).isoformat(),
        }
        account.daily_limits = {**(account.daily_limits or {}), "replacement_join_pacing": pacing}
        job.update(status="attempting", attempted_at=at.isoformat())
        _save(campaign, replacement)
        await session.commit()
        source = await session.get(TelegramSource, job["source_id"])
        try:
            if not source or source.workspace_id != campaign.workspace_id:
                raise ValueError("SOURCE_NOT_FOUND")
            with replacement_pacing_scope(pacing):
                async with telegram_client_for_account(session, account) as client:
                    client._safety_intent = f"campaign-replacement:{replacement['id']}:{job['id']}"
                    entity = await client.get_entity(
                        _normalize_source_ref(source.username or source.url or source.tg_id)
                    )
                    if job["kind"] == "discussion":
                        parent = await client(GetFullChannelRequest(entity))
                        linked_id = getattr(parent.full_chat, "linked_chat_id", None)
                        entity = next((c for c in parent.chats if c.id == linked_id), None)
                        if entity is None or str(linked_id) != job["tg_id"]:
                            raise ValueError("DISCUSSION_CHANGED")
                    if not (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
                        raise ValueError("SOURCE_NOT_CHANNEL")
                    if getattr(entity, "join_request", False):
                        raise ValueError("JOIN_APPROVAL_REQUIRED")
                    already_joined = not bool(getattr(entity, "left", True))
                    if not already_joined:
                        await client(JoinChannelRequest(entity))
                    if job["kind"] == "channel":
                        full = await client(GetFullChannelRequest(entity))
                        linked_id = getattr(full.full_chat, "linked_chat_id", None)
                        linked = next((c for c in full.chats if c.id == linked_id), None)
                        if not linked_id or linked is None:
                            raise ValueError("COMMENTS_CLOSED_OR_DISCUSSION_UNRESOLVED")
                        discussion_joined = not bool(getattr(linked, "left", True))
                        requires_join = bool(getattr(linked, "join_to_send", False))
                        if requires_join and getattr(linked, "join_request", False):
                            raise ValueError("DISCUSSION_APPROVAL_REQUIRED")
                        if requires_join and not discussion_joined:
                            replacement["jobs"].insert(
                                replacement["jobs"].index(job) + 1,
                                {
                                    "id": str(uuid4()),
                                    "source_id": source.id,
                                    "kind": "discussion",
                                    "tg_id": str(linked_id),
                                    "status": "queued",
                                },
                            )
            metadata = deepcopy(source.source_metadata or {})
            access = dict(metadata.get("account_access", {}))
            entry = {**access.get(account.id, {}), "replacement_id": replacement["id"], "checked_at": now().isoformat()}
            if job["kind"] == "channel":
                source.tg_id = str(entity.id)
                source.comments_enabled = True
                source.linked_discussion_tg_id = str(linked_id)
                source.linked_discussion_username = getattr(linked, "username", None)
                source.access_status = "available"
                entry.update(
                    access_checked=True,
                    joined=True,
                    discussion_joined=discussion_joined,
                    discussion_requires_join=requires_join,
                )
            else:
                entry.update(discussion_joined=True, discussion_checked_at=now().isoformat())
            access[account.id] = entry
            metadata["account_access"] = access
            ready = entry.get("joined") and (
                entry.get("discussion_joined") or not entry.get("discussion_requires_join")
            )
            metadata["campaign_account_assignments"][campaign.id]["status"] = "ready" if ready else "preparing"
            for key, binding in metadata.get("demo_launch_assignments", {}).items():
                if binding.get("campaign_id") == campaign.id:
                    metadata["demo_launch_assignments"][key] = {
                        **binding,
                        "channel_membership": "joined",
                        "send_ready": bool(ready),
                    }
            source.source_metadata = metadata
            job.update(
                status="joined", already_joined=already_joined, finished_at=now().isoformat(), tg_id=str(entity.id)
            )
            await session.refresh(campaign)
            if campaign.status != "paused":
                replacement.update(
                    status="cancelled", resume_when_ready=False, wait_reason="Кампания остановлена во время подготовки."
                )
            replacement["next_join_at"] = (now() + timedelta(seconds=interval if not already_joined else 0)).isoformat()
            _save(campaign, replacement)
            await session.commit()
            if (
                campaign.status == "paused"
                and replacement["status"] == "joining"
                and all(j["status"] == "joined" for j in replacement["jobs"])
            ):
                await _finish(session, campaign, replacement)
        except Exception as exc:
            await session.rollback()
            campaign = await session.get(Campaign, campaign_id, populate_existing=True)
            replacement = deepcopy(campaign.config["account_replacement"])
            saved = next(j for j in replacement["jobs"] if j["id"] == job["id"])
            code = getattr(exc, "code", None)
            if code in {"SAFETY_ATTEMPT_BUDGET", "SAFETY_RPC_BUDGET", "SAFETY_JOIN_RATE"}:
                # Local guard refused an RPC, not a failed/unknown Telegram write.
                # Preserve the diagnostic and re-check admission/membership on a later tick.
                saved.setdefault("local_wait_history", []).append(
                    {
                        "code": code,
                        "attempted_at": saved.get("attempted_at"),
                        "at": now().isoformat(),
                    }
                )
                saved.update(status="queued", last_wait_code=code)
                saved.pop("attempted_at", None)
                saved.pop("error_code", None)
                replacement.update(
                    status="waiting_account",
                    wait_reason="Ожидание доступного бюджета операций: " + code,
                    next_join_at=(
                        await local_budget_retry_at(session, replacement["new_account_id"], code, now())
                    ).isoformat(),
                )
                _save(campaign, replacement)
                await session.commit()
                return
            local_code = getattr(exc, "code", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            if local_code in {
                "SOURCE_ACCESS_DENIED",
                "SOURCE_READ_DENIED",
                "JOIN_APPROVAL_PENDING",
                "JOIN_APPROVAL_REQUIRED",
                "DISCUSSION_APPROVAL_REQUIRED",
                "COMMENTS_CLOSED_OR_DISCUSSION_UNRESOLVED",
            }:
                from app.modules.campaigns.engine import _exclude_comment_account_source_pair

                source = await session.get(TelegramSource, job["source_id"])
                account = await session.get(TelegramAccount, replacement["new_account_id"])
                _exclude_comment_account_source_pair(campaign, source, account, now(), local_code)
                rejected = [j for j in replacement["jobs"] if j["source_id"] == source.id]
                replacement.setdefault("rejected_jobs", []).extend(
                    [
                        {**j, "status": "blocked", "error_code": local_code, "finished_at": now().isoformat()}
                        for j in rejected
                    ]
                )
                replacement["jobs"] = [j for j in replacement["jobs"] if j["source_id"] != source.id]
                replacement.update(
                    status="joining" if replacement["jobs"] else "needs_review",
                    wait_reason=None
                    if replacement["jobs"]
                    else "Нет доступных источников после исключения закрытых обсуждений.",
                    next_join_at=(now() + timedelta(seconds=replacement["join_interval_seconds"])).isoformat(),
                )
                _save(campaign, replacement)
                await session.commit()
                if replacement["jobs"] and all(j["status"] == "joined" for j in replacement["jobs"]):
                    await _finish(session, campaign, replacement)
                return
            saved.update(
                status="failed",
                error_code=getattr(exc, "code", str(exc) if isinstance(exc, ValueError) else type(exc).__name__),
                finished_at=now().isoformat(),
            )
            replacement.update(status="needs_review", wait_reason="Подготовка остановлена: " + saved["error_code"])
            _save(campaign, replacement)
            await session.commit()


async def _finish(session, campaign, replacement):
    replacement.update(status="ready", wait_reason=None, finished_at=now().isoformat())
    _save(campaign, replacement)
    control = deepcopy(campaign.config.get("controlled_launch"))
    if control:
        control.update(
            status="running" if replacement["resume_when_ready"] else "prepared",
            activate_not_before=now().isoformat(),
            jobs=[
                {**j, "not_before": replacement["requested_at"], "comments_enabled": True} for j in replacement["jobs"]
            ],
        )
        campaign.config = {**campaign.config, "controlled_launch": control}
    if replacement["resume_when_ready"]:
        campaign.status = "pending"
        session.add(
            CampaignRun(
                workspace_id=campaign.workspace_id,
                campaign_id=campaign.id,
                status="running",
                started_at=now(),
                heartbeat_at=now(),
                progress=replacement["previous_run_progress"],
                stop_conditions=campaign.config.get("stop_conditions", {}),
                report={"status": "running", "reason": "account_replacement_ready"},
            )
        )
    else:
        campaign.status = "paused"
    await session.commit()


async def cancel_replacement(session, campaign, actor_id):
    workspace = await session.get(Workspace, campaign.workspace_id)
    if not workspace or workspace.owner_user_id != actor_id:
        raise HTTPException(403, "Отмена замены доступна владельцу workspace.")
    if session.bind.dialect.name == "postgresql":
        if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(604102026)")):
            raise HTTPException(409, "Сейчас завершается действие кампании. Повторите после его завершения.")
    async with queue_lock(session, campaign.id) as acquired:
        if not acquired:
            raise HTTPException(409, "Сейчас выполняется вступление. Дождитесь его результата.")
        await session.refresh(campaign, with_for_update=True)
        replacement = deepcopy(campaign.config.get("account_replacement", {}))
        if not replacement_pending(campaign):
            return campaign
        if any(j["status"] == "attempting" for j in replacement.get("jobs", [])):
            raise HTTPException(409, "Исход вступления неизвестен. Сначала нужна сверка результата.")
        replacement.update(status="cancelled", cancelled_at=now().isoformat(), wait_reason=None)
        _save(campaign, replacement)
        campaign.status = "paused"
        await session.commit()
        return campaign
