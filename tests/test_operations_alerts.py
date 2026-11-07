from app.modules.operations.alerts import _format_payload


def test_telegram_alert_payload_prefers_human_names() -> None:
    text = _format_payload(
        {
            "campaign_name": "Недвижимость 6",
            "account_name": "commenter",
            "source_title": "Target channel",
            "source_ref": "@target",
            "error_code": "SESSION_NOT_FOUND",
            "campaign_id": "campaign-uuid",
            "run_id": "run-uuid",
            "account_id": "account-uuid",
            "source_id": "source-uuid",
        }
    )

    assert "Кампания: Недвижимость 6" in text
    assert "Аккаунт: commenter" in text
    assert "Источник: Target channel" in text
    assert "Ссылка: @target" in text
    assert "Код: SESSION_NOT_FOUND" in text
    assert "campaign_id: campaign-uuid" in text
