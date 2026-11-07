import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.link_tracking.models import TrackingClick, TrackingLink

MOSCOW = ZoneInfo("Europe/Moscow")
# These requests are retained separately; a browser-looking GET is not proof of a human.
BOT = re.compile(
    r"bot|crawler|spider|preview|slack|discord|facebookexternalhit|whatsapp|curl|wget|python|httpx|headless", re.I
)


def classify_request(request: Request) -> str:
    if request.method == "HEAD":
        return "head"
    if any("prefetch" in request.headers.get(h, "").lower() for h in ("purpose", "sec-purpose", "x-purpose")):
        return "prefetch"
    ua = request.headers.get("user-agent", "")
    return "technical" if not ua or BOT.search(ua) else "transition"


def browser_token(request: Request) -> str:
    value = request.cookies.get("wn_tracking", "")
    token, _, signature = value.partition(".")
    key = get_settings().secret_key.encode()
    expected = hmac.new(key, ("tracking-cookie|" + token).encode(), hashlib.sha256).hexdigest()
    if len(token) == 32 and hmac.compare_digest(signature, expected):
        return value
    token = secrets.token_hex(16)
    signature = hmac.new(key, ("tracking-cookie|" + token).encode(), hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def visitor_hash(request: Request, day: str) -> str:
    # Do not persist IP addresses, raw user agents, referrers or query strings.
    ip = request.client.host if request.client else "unknown"
    browser = getattr(request.state, "tracking_browser", None)
    identity = browser or f"{ip}|{request.headers.get('user-agent', '')[:512]}"
    value = f"tracking-v1|{day}|{identity}"
    return hmac.new(get_settings().secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


async def record_click(session: AsyncSession, link_id: str, request: Request) -> None:
    now = datetime.now(UTC)
    day = now.astimezone(MOSCOW).date().isoformat()
    session.add(
        TrackingClick(
            link_id=link_id,
            occurred_at=now,
            day=day,
            visitor_hash=visitor_hash(request, day),
            category=classify_request(request),
        )
    )
    await session.commit()


async def metrics(session: AsyncSession, workspace_id: str, since: datetime, until: datetime, daily: bool = False):
    groups = [TrackingClick.link_id]
    if daily:
        groups.append(TrackingClick.day)
    result = await session.execute(
        select(
            *groups,
            func.count().label("requests"),
            func.sum(case((TrackingClick.category == "transition", 1), else_=0)).label("transitions"),
            func.count(distinct(case((TrackingClick.category == "transition", TrackingClick.visitor_hash)))).label(
                "unique_daily"
            ),
        )
        .join(TrackingLink, TrackingLink.id == TrackingClick.link_id)
        .where(
            TrackingLink.workspace_id == workspace_id,
            TrackingClick.occurred_at >= since,
            TrackingClick.occurred_at < until,
        )
        .group_by(*groups)
    )
    return [dict(row._mapping) for row in result]
