from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings


async def send_telegram_alert(
    *,
    severity: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    settings = get_settings()
    if not (settings.telegram_alerts_enabled and settings.telegram_alert_bot_token and settings.telegram_alert_chat_id):
        return False

    details = _format_payload(payload or {})
    text = f"{_severity_label(severity)} {title}\n\n{message}"
    if details:
        text = f"{text}\n\n{details}"

    url = f"https://api.telegram.org/bot{settings.telegram_alert_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            url,
            json={
                "chat_id": settings.telegram_alert_chat_id,
                "text": text[:3900],
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
    return True


def _severity_label(severity: str) -> str:
    return {
        "critical": "[CRITICAL]",
        "error": "[ERROR]",
        "warning": "[WARNING]",
        "info": "[INFO]",
    }.get(severity, "[EVENT]")


def _format_payload(payload: dict[str, Any]) -> str:
    rows: list[str] = []
    for key, label in [
        ("campaign_name", "Кампания"),
        ("account_name", "Аккаунт"),
        ("source_title", "Источник"),
        ("source_ref", "Ссылка"),
        ("error_code", "Код"),
        ("campaign_id", "campaign_id"),
        ("run_id", "run_id"),
        ("account_id", "account_id"),
        ("source_id", "source_id"),
    ]:
        value = payload.get(key)
        if value is not None and str(value).strip():
            rows.append(f"{label}: {value}")
    return "\n".join(rows)
