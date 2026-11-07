"""Explicit reconciliation of interrupted username lookups, never message writes."""

from datetime import UTC, datetime, timedelta


def validate_interrupted_lookup(operation, now):
    if (
        operation.method != "ResolveUsernameRequest"
        or operation.outbound
        or operation.outcome != "pending"
        or operation.finished_at is not None
    ):
        raise ValueError("Only an unfinished, read-only username lookup can be reconciled.")
    created = operation.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if now - created < timedelta(minutes=15):
        raise ValueError("The lookup is too recent to be treated as interrupted.")


async def reconcile_interrupted_lookup(account_id, operation_id, actor_id, evidence):
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal
    from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount
    from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
    from app.modules.workspaces.models import Workspace

    if not evidence.strip():
        raise ValueError("Interruption evidence is required.")
    async with AsyncSessionLocal.begin() as db:
        account = await db.get(TelegramAccount, account_id)
        workspace = await db.get(Workspace, account.workspace_id) if account else None
        state = await db.get(TelegramSafetyState, account_id)
        if not workspace or workspace.owner_user_id != actor_id or not state or not state.fingerprint:
            raise ValueError("Owner and registered session evidence are required.")
        lock_id = int.from_bytes(bytes.fromhex(state.fingerprint)[:8], "big", signed=True)
        if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}):
            raise ValueError("The Telegram session is still in use; reconciliation cancelled.")
        operation = await db.get(TelegramOperation, operation_id, with_for_update=True)
        if not operation or operation.account_id != account_id:
            raise ValueError("Operation does not belong to the account.")
        now = datetime.now(UTC)
        validate_interrupted_lookup(operation, now)
        # The lookup result is unknown; this does not assert Telegram delivery or success.
        operation.outcome = "read_interrupted"
        operation.finished_at = now
        operation.error_code = "INTERRUPTED_READ_RECONCILED"
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.reconcile_interrupted_read",
                outbound=False,
                target=operation_id,
                outcome="read_interrupted",
                created_at=now,
                finished_at=now,
                idempotency_key=f"reconcile-interrupted-read:{operation_id}",
            )
        )
        db.add(
            AccountHealthEvent(
                account_id=account_id,
                event_type="interrupted_read_reconciled",
                severity="info",
                message="Interrupted username lookup reconciled without Telegram requests.",
                raw_error={"operation_id": operation_id, "actor_id": actor_id, "evidence": evidence.strip()},
            )
        )
        return {"operation_id": operation_id, "outcome": operation.outcome}
