from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import app.db.import_models  # noqa: F401
from app.api.deps import get_workspace_id
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.modules.auth.models import User
from app.modules.link_tracking import api
from app.modules.link_tracking.models import TrackingClick
from app.modules.link_tracking.schemas import LinkInput
from app.modules.link_tracking.service import classify_request, visitor_hash
from app.modules.workspaces.models import Workspace


def request(ua="Mozilla/5.0", method="GET", headers=None):
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [(b"user-agent", ua.encode()), *(headers or [])],
            "client": ("192.0.2.1", 443),
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/a",
        "javascript:alert(1)",
        "https://t.me.evil.test/a",
        "https://t.me@evil.test/a",
        "http://t.me/a",
        "https://t.me:443/a",
        "https://t.me/",
        "https://t.me/a\r\nx:y",
    ],
)
def test_destination_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        LinkInput(title="test", destination_url=url)


@pytest.mark.parametrize("url", ["https://t.me/channel", "https://t.me/+invite", "https://t.me/bot?start=source"])
def test_destination_preserves_telegram_links(url):
    assert LinkInput(title="test", destination_url=url).destination_url == url


def test_filter_and_privacy():
    assert classify_request(request()) == "transition"
    assert classify_request(request("TelegramBot (like TwitterBot)")) == "technical"
    assert classify_request(request(method="HEAD")) == "head"
    assert classify_request(request(headers=[(b"sec-purpose", b"prefetch")])) == "prefetch"
    assert visitor_hash(request(), "2026-10-12") == visitor_hash(request(), "2026-10-12")
    assert visitor_hash(request(), "2026-10-12") != visitor_hash(request(), "2026-10-13")
    assert "192.0.2.1" not in visitor_hash(request(), "2026-10-12")


@pytest.fixture
async def rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(email="links@example.com", password_hash="x", status="active")
        s.add(user)
        await s.flush()
        ws = Workspace(owner_user_id=user.id, name="Links", slug="links")
        s.add(ws)
        await s.commit()
        ws_id = ws.id

    async def session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_workspace_id] = lambda: ws_id
    monkeypatch.setattr(api, "AsyncSessionLocal", factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost") as client:
        yield client, factory, ws_id
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_full_path_metrics_edit_disable_and_isolation(rig):
    client, factory, _ = rig
    response = await client.post(
        "/api/v1/tracking-links", json={"title": "=TEST", "destination_url": "https://t.me/first"}
    )
    assert response.status_code == 201, response.text
    link = response.json()
    path = f"/r/{link['code']}"
    browser = {"user-agent": "Mozilla/5.0 Safari/605.1"}
    for _ in range(2):
        r = await client.get(path, headers=browser)
        assert r.status_code == 302 and r.headers["location"] == "https://t.me/first"
        assert "no-store" in r.headers["cache-control"]
    await client.get(path, headers={"user-agent": "TelegramBot"})
    await client.head(path, headers=browser)
    await client.get(path, headers={**browser, "purpose": "prefetch"})
    links = (await client.get("/api/v1/tracking-links?days=1")).json()
    assert links[0]["requests"] == 5 and links[0]["transitions"] == 2 and links[0]["unique_daily"] == 1
    # A second browser behind the same IP/UA must not collapse into the first visitor.
    client.cookies.clear()
    await client.get(path, headers=browser)
    links = (await client.get("/api/v1/tracking-links?days=1")).json()
    assert links[0]["transitions"] == 3 and links[0]["unique_daily"] == 2
    csv = await client.get("/api/v1/tracking-links/export.csv")
    assert "'=TEST" in csv.text
    report = (await client.get("/api/v1/tracking-links/report")).json()
    assert report["timezone"] == "Europe/Moscow" and report["rows"][0]["transitions"] == 3
    body = {"title": "test", "destination_url": "https://t.me/second", "enabled": True}
    updated = (await client.put(f"/api/v1/tracking-links/{link['id']}", json=body)).json()
    assert updated["tracking_url"] == link["tracking_url"]
    assert (await client.get(path)).headers["location"] == body["destination_url"]
    body["enabled"] = False
    await client.put(f"/api/v1/tracking-links/{link['id']}", json=body)
    assert (await client.get(path)).status_code == 404
    assert (await client.get("/r/missing")).status_code == 404
    app.dependency_overrides[get_workspace_id] = lambda: "different-workspace"
    assert (await client.get("/api/v1/tracking-links")).json() == []
    assert (await client.put(f"/api/v1/tracking-links/{link['id']}", json=body)).status_code == 404
    assert (await client.get("/api/v1/tracking-links/report")).json()["rows"] == []


async def test_storage_failure_still_redirects(rig, monkeypatch):
    client, _, _ = rig
    link = (
        await client.post("/api/v1/tracking-links", json={"title": "test", "destination_url": "https://t.me/first"})
    ).json()

    async def fail(*args):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(api, "persist_event", fail)
    response = await client.get(f"/r/{link['code']}")
    assert response.status_code == 302 and response.headers["location"] == "https://t.me/first"


async def test_other_workspace_references_rejected(rig):
    client, _, _ = rig
    result = await client.post(
        "/api/v1/tracking-links",
        json={"title": "test", "destination_url": "https://t.me/first", "campaign_id": "foreign"},
    )
    assert result.status_code == 422


async def test_day_and_period_counts(rig):
    client, factory, _ = rig
    link = (
        await client.post("/api/v1/tracking-links", json={"title": "test", "destination_url": "https://t.me/first"})
    ).json()
    async with factory() as s:
        s.add(
            TrackingClick(
                link_id=link["id"],
                occurred_at=datetime.now(UTC) - timedelta(days=3),
                day="2026-10-09",
                visitor_hash="earlier",
                category="transition",
            )
        )
        await s.commit()
    assert (await client.get("/api/v1/tracking-links?days=1")).json()[0]["transitions"] == 0
    assert (await client.get("/api/v1/tracking-links?days=7")).json()[0]["transitions"] == 1
    async with factory() as s:
        event = await s.scalar(select(TrackingClick))
        assert event.visitor_hash == "earlier"
