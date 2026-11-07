from types import SimpleNamespace

import pytest

from app.modules.account_market import api


@pytest.mark.asyncio
async def test_market_management_uses_configured_allowlist(monkeypatch) -> None:
    settings = SimpleNamespace(market_admin_emails=" Admin@Example.com , other@example.com ")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    assert await api._can_manage_market(None, "workspace", SimpleNamespace(email="admin@example.com"))
    assert not await api._can_manage_market(None, "workspace", SimpleNamespace(email="user@example.com"))
    settings.market_admin_emails = ""
    assert not await api._can_manage_market(None, "workspace", SimpleNamespace(email="admin@example.com"))
