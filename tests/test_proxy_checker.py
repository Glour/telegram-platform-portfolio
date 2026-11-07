from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.proxies import service as proxy_service
from app.modules.proxies.models import Proxy, ProxyCheck
from app.modules.proxies.schemas import ProxyBulkCheckRequest
from app.modules.proxies.service import bulk_check_proxies, parse_proxy_line
from app.modules.workspaces.models import Workspace


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _workspace(session: AsyncSession) -> Workspace:
    workspace = Workspace(owner_user_id="owner", name="Test", slug="test")
    session.add(workspace)
    await session.flush()
    return workspace


def test_parse_proxy_line_supports_webshare_and_url_formats() -> None:
    webshare = parse_proxy_line("192.0.2.10:5803:user:pass")
    url = parse_proxy_line("socks5://login:secret@198.51.100.20:6370")

    assert webshare.type == "socks5"
    assert webshare.host == "192.0.2.10"
    assert webshare.port == 5803
    assert webshare.username == "user"
    assert webshare.password == "pass"
    assert url.host == "198.51.100.20"
    assert url.username == "login"
    assert url.password == "secret"


@pytest.mark.asyncio
async def test_bulk_proxy_check_can_save_valid_items(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session)

    async def fake_check_proxy_values(**kwargs: object) -> dict[str, object]:
        return {
            "status": "succeeded",
            "latency_ms": 123,
            "ip_detected": kwargs["host"],
            "telegram_ok": True,
            "telegram_latency_ms": 234,
            "geo_country": "US",
            "error_code": None,
            "error_message": None,
        }

    monkeypatch.setattr("app.modules.proxies.service.check_proxy_values", fake_check_proxy_values)

    result = await bulk_check_proxies(
        session,
        workspace.id,
        ProxyBulkCheckRequest(
            raw_text="192.0.2.10:5803:user:pass\nbad-row",
            provider="Webshare",
            proxy_class="static_residential",
            exclusivity="dedicated",
            save_valid=True,
        ),
    )
    proxies = list((await session.execute(select(Proxy))).scalars().all())

    assert result.total == 2
    assert result.parsed == 1
    assert result.valid == 1
    assert result.saved == 1
    assert result.items[0].telegram_ok is True
    assert result.items[1].status == "invalid"
    assert proxies[0].country == "US"
    assert proxies[0].provider == "Webshare"
    assert proxies[0].proxy_class == "static_residential"
    assert proxies[0].exclusivity == "dedicated"
    assert proxies[0].last_ip_detected == "192.0.2.10"
    assert proxies[0].last_country_detected == "US"
    assert proxies[0].telegram_connectivity_status == "ok"
    proxy_check = (await session.execute(select(ProxyCheck))).scalar_one()
    assert proxy_check.telegram_latency_ms == 234
    assert proxy_check.geo_country == "US"


def test_telegram_proxy_check_fails_on_partial_dc_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def settimeout(self, timeout: int) -> None:
            self.timeout = timeout

        def set_proxy(self, *args: object, **kwargs: object) -> None:
            self.proxy = (args, kwargs)

        def connect(self, target: tuple[str, int]) -> None:
            if target[0] == "dc-bad":
                raise TimeoutError("timed out")

        def close(self) -> None:
            pass

    monkeypatch.setattr(proxy_service, "TELEGRAM_CONNECT_TARGETS", (("dc-good", 443), ("dc-bad", 443)))
    monkeypatch.setattr(proxy_service.socks, "socksocket", FakeSocket)

    result = proxy_service._check_telegram_connect_sync("socks5", "proxy.local", 1080, "user", "pass")

    assert result["ok"] is False
    assert result["latency_ms"] is not None
    assert result["error_code"] == "TELEGRAM_PARTIAL_CONNECT_FAILED"
    assert "dc-bad:443" in str(result["error_message"])


@pytest.mark.asyncio
@pytest.mark.parametrize("measured_ip", [None, "203.0.113.20"])
async def test_proxy_check_reports_only_measured_egress_ip(monkeypatch, measured_ip):
    monkeypatch.setattr(
        proxy_service,
        "_check_tcp",
        AsyncMock(
            return_value={
                "status": "succeeded",
                "latency_ms": 10,
                "ip_detected": None,
                "error_code": None,
                "error_message": None,
            }
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "_proxy_geo_country_through_proxy",
        AsyncMock(
            return_value={
                "ip": measured_ip,
                "country": None,
            }
        ),
    )
    monkeypatch.setattr(proxy_service, "_proxy_geo_country", AsyncMock(return_value="US"))
    telegram_check = AsyncMock(side_effect=AssertionError("Telegram check is disabled"))
    monkeypatch.setattr(proxy_service, "_check_telegram_connect", telegram_check)

    result = await proxy_service.check_proxy_values(
        proxy_type="socks5",
        host="192.0.2.10",
        port=1080,
        check_telegram=False,
    )

    assert result["status"] == "succeeded"
    assert result["geo_country"] == "US"
    assert result["ip_detected"] == measured_ip
    assert result["ip_detected"] != "192.0.2.10"
    telegram_check.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_code",
    [
        (None, None),
        (TimeoutError("timed out"), "PROXY_TIMEOUT"),
        (OSError("connection refused"), "PROXY_CONNECT_FAILED"),
    ],
)
async def test_tcp_connection_never_reports_an_egress_ip(monkeypatch, error, expected_code):
    sock = Mock()
    connect = Mock(return_value=sock, side_effect=error)
    monkeypatch.setattr(proxy_service.socket, "create_connection", connect)

    result = await proxy_service._check_tcp("192.0.2.10", 1080)

    assert result["ip_detected"] is None
    assert result["error_code"] == expected_code
    assert result["status"] == ("succeeded" if error is None else "failed")
    if error is None:
        sock.close.assert_called_once()


@pytest.mark.asyncio
async def test_bulk_proxy_check_without_measured_ip_preserves_unknown_class(monkeypatch, session):
    workspace = await _workspace(session)
    monkeypatch.setattr(
        proxy_service,
        "_check_tcp",
        AsyncMock(
            return_value={
                "status": "succeeded",
                "latency_ms": 10,
                "ip_detected": None,
                "error_code": None,
                "error_message": None,
            }
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "_proxy_geo_country_through_proxy",
        AsyncMock(
            return_value={
                "ip": None,
                "country": None,
            }
        ),
    )
    monkeypatch.setattr(proxy_service, "_proxy_geo_country", AsyncMock(return_value=None))
    monkeypatch.setattr(
        proxy_service,
        "_check_telegram_connect",
        AsyncMock(
            return_value={
                "ok": True,
                "latency_ms": 20,
                "error_code": None,
                "error_message": None,
            }
        ),
    )

    result = await bulk_check_proxies(
        session,
        workspace.id,
        ProxyBulkCheckRequest(raw_text="192.0.2.10:1080:user:pass", save_valid=True),
    )

    proxy = (await session.execute(select(Proxy))).scalar_one()
    check = (await session.execute(select(ProxyCheck))).scalar_one()
    assert result.saved == 1
    assert proxy.proxy_class == "unknown"
    assert proxy.last_ip_detected is None
    assert check.ip_detected is None
