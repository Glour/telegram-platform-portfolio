"""Workspace permission overview. Reads persisted evidence; performs no Telegram RPC."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_safety.admission import ATTESTABLE_REASONS
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
from app.modules.workspaces.models import Workspace, WorkspaceMember

HARD_STATUSES = {"blocked", "banned", "frozen", "spam_blocked", "quarantined", "invalid", "deleted", "archived"}
STATUS_LABELS = {
    "invalid": "Сессия невалидна",
    "quarantined": "Карантин: требуется проверка",
    "spam_blocked": "Спам-блок",
    "blocked": "Аккаунт заблокирован",
    "banned": "Аккаунт заблокирован",
    "frozen": "Аккаунт заморожен",
}


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def account_overview(account, state, proxy, *, pending=False, proxy_count=0, is_owner=False, can_manage=False):
    from app.modules.telegram_safety.service import _owner_admission_valid

    now = datetime.now(UTC)
    restriction = (state.restriction_status if state else None) or account.restriction_status or "unknown"
    reason = state.reason if state else "admission_required"
    grant_problem = None
    if account.status in HARD_STATUSES:
        grant_problem = STATUS_LABELS.get(account.status, "Статус аккаунта не допускает работу")
    elif restriction in {"limited", "blocked", "frozen", "spam_blocked"} or account.restriction_status in {
        "limited",
        "blocked",
        "frozen",
        "spam_blocked",
    }:
        grant_problem = "Подтверждено ограничение Telegram"
    elif state and state.wait_until and aware(state.wait_until) > now:
        grant_problem = "Ожидание Telegram до " + aware(state.wait_until).isoformat()
    elif pending:
        grant_problem = "Нужно уточнить результат незавершённой операции"
    elif reason not in ATTESTABLE_REASONS:
        grant_problem = "Требуется разбор причины остановки — откройте Подробнее"
    owner_valid = bool(state and _owner_admission_valid(state, account))
    clear_valid = False
    try:
        checked = account._restriction_metadata().get("checked_at")
        clear_valid = bool(
            state
            and state.restriction_status == "clear"
            and state.restriction_checked_at
            and checked
            and now - timedelta(hours=24) <= aware(state.restriction_checked_at) <= now
            and now - timedelta(hours=24) <= aware(checked) <= now
        )
    except (ValueError, TypeError):
        pass
    admitted = bool(state and not state.manual_hold and state.operator_approved_at and (owner_valid or clear_valid))
    permission = (
        "allowed"
        if admitted
        else "paused"
        if state and state.manual_hold and state.operator_approved_at
        else "required"
    )
    audit = (account.daily_limits or {}).get("session_audit", {})
    session_status = audit.get("online_session", "not_checked")
    if account.status == "invalid":
        session_status = "invalid"
    proxy_ok = bool(
        proxy
        and proxy.status == "active"
        and proxy.workspace_id == account.workspace_id
        and proxy_count <= (proxy.max_accounts or 1)
        and proxy.telegram_connectivity_status not in {"failed", "blocked", "error"}
    )
    problem = grant_problem
    if not problem and account.proxy_required and not proxy_ok:
        problem = "Нужен рабочий прокси" if not proxy else "Проверьте состояние или загрузку прокси"
    if not problem and not admitted:
        problem = "Работа приостановлена оператором" if reason == "operator_paused" else "Нужен допуск владельца"
    return {
        "id": account.id,
        "phone": account.phone,
        "username": account.username,
        "display_name": account.display_name,
        "account_status": account.status,
        "session_status": session_status,
        "proxy_assigned": bool(proxy),
        "proxy_ready": proxy_ok,
        "proxy_status": proxy.status if proxy else None,
        "permission": permission,
        "admission_basis": "owner_attestation" if owner_valid else "operator_admission" if admitted else None,
        "restriction_status": restriction,
        "restriction_checked_at": state.restriction_checked_at if state else None,
        "manual_hold": state.manual_hold if state else True,
        "reason": reason,
        "ready": bool(admitted and not problem),
        "problem": problem,
        "can_attest": bool(is_owner and not grant_problem),
        "attest_problem": grant_problem
        or (None if is_owner else "Допуск доверенного аккаунта подтверждает владелец workspace"),
        "can_pause": bool(can_manage and state and not state.manual_hold),
        "operator_approved_at": state.operator_approved_at if state else None,
    }


async def workspace_overview(db, workspace_id, actor_id):
    workspace = await db.get(Workspace, workspace_id)
    is_owner = bool(workspace and workspace.owner_user_id == actor_id)
    member = (
        await db.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == actor_id,
                WorkspaceMember.status == "active",
            )
        )
    ).first()
    can_manage = is_owner or bool(member and member.role in {"owner", "admin"})
    accounts = list(
        (
            await db.scalars(
                select(TelegramAccount)
                .where(
                    TelegramAccount.workspace_id == workspace_id, TelegramAccount.status.notin_(["deleted", "archived"])
                )
                .order_by(TelegramAccount.sort_order, TelegramAccount.created_at)
            )
        ).all()
    )
    ids = [a.id for a in accounts]
    states = {
        s.account_id: s
        for s in (await db.scalars(select(TelegramSafetyState).where(TelegramSafetyState.account_id.in_(ids)))).all()
    }
    proxies = {p.id: p for p in (await db.scalars(select(Proxy).where(Proxy.workspace_id == workspace_id))).all()}
    # Include archived assignments when assessing proxy occupancy.
    occupancy = Counter(
        (
            await db.scalars(
                select(TelegramAccount.proxy_id).where(
                    TelegramAccount.workspace_id == workspace_id, TelegramAccount.proxy_id.is_not(None)
                )
            )
        ).all()
    )
    pending = set(
        (
            await db.scalars(
                select(TelegramOperation.account_id).where(
                    TelegramOperation.account_id.in_(ids),
                    TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                )
            )
        ).all()
    )
    rows = [
        account_overview(
            a,
            states.get(a.id),
            proxies.get(a.proxy_id),
            pending=a.id in pending,
            proxy_count=occupancy[a.proxy_id],
            is_owner=is_owner,
            can_manage=can_manage,
        )
        for a in accounts
    ]
    return {"accounts": rows, "can_manage": can_manage, "is_owner": is_owner}
