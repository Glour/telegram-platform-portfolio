"""Unit tests for pilot broadcast authorization without a Telegram connection."""

from types import SimpleNamespace

from app.core.text import utf16_length
from app.modules.contact_broadcasts import service


def test_utf16_length_counts_supplementary_characters() -> None:
    assert utf16_length("A😀") == 3


def test_pilot_requires_configured_ids_and_exactly_one_recipient(monkeypatch) -> None:
    settings = SimpleNamespace(
        contact_broadcast_bulk_enabled=False,
        contact_broadcast_bulk_workspace_ids=[],
        contact_broadcast_pilot_account_id="account",
        contact_broadcast_pilot_sender_tg_id="sender",
        contact_broadcast_pilot_recipient_tg_id="recipient",
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    account = SimpleNamespace(id="account", workspace_id="workspace")
    payload = {
        "sender_id": "sender",
        "pilot_recipient_id": "recipient",
        "recipients": [{"user_id": "recipient"}],
    }
    assert service.pilot_allowed(account, payload)
    assert not service.pilot_allowed(account, {**payload, "sender_id": "other"})
    assert not service.pilot_allowed(account, {**payload, "recipients": []})
    assert not service.pilot_allowed(account, {**payload, "recipients": [{"user_id": "other"}]})
    assert not service.pilot_allowed(account, {**payload, "recipients": payload["recipients"] * 2})
    settings.contact_broadcast_pilot_sender_tg_id = ""
    assert not service.pilot_allowed(account, {**payload, "sender_id": ""})
    settings.contact_broadcast_pilot_sender_tg_id = "sender"
    settings.contact_broadcast_pilot_recipient_tg_id = ""
    assert not service.pilot_allowed(account, payload)
