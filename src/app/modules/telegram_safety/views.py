"""Read-only account-card projection of the persistent stop; no parent-row writes."""

from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState


async def apply_safety_projection(db, account, payload: dict) -> dict:
    state = await db.get(TelegramSafetyState, account.id)
    payload = {
        **payload,
        "telegram_manual_hold": state.manual_hold if state else None,
        "telegram_safety_reason": state.reason if state else None,
    }
    if not state or not state.manual_hold:
        return payload
    if state.restriction_status in {"limited", "blocked"}:
        return {
            **payload,
            "status": "spam_blocked",
            "restriction_status": state.restriction_status,
            "restriction_label": "Спам-блок подтверждён",
        }
    operation = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
    if operation and operation.error_code == "UserBannedInChannelError":
        return {
            **payload,
            "status": "quarantined",
            "restriction_label": "Подозрение на спам-блок / запрет записи — аккаунт остановлен",
        }
    return payload
