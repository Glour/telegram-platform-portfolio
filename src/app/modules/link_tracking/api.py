import asyncio
import csv
import io
import logging
import secrets
from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.modules.campaigns.models import Campaign
from app.modules.link_tracking.models import TrackingLink
from app.modules.link_tracking.schemas import LinkInput
from app.modules.link_tracking.service import MOSCOW, browser_token, classify_request, metrics, record_click
from app.modules.telegram_accounts.models import TelegramAccount

router = APIRouter()
public_router = APIRouter()
log = logging.getLogger(__name__)


def period(days: int):
    end = datetime.now(UTC)
    start = datetime.combine(end.astimezone(MOSCOW).date() - timedelta(days=days - 1), time(), MOSCOW)
    return start, end


def serialize(link: TrackingLink, counts: dict | None = None) -> dict:
    settings = get_settings()
    base = settings.tracking_public_base_url.rstrip("/")
    return dict(
        id=link.id,
        code=link.code,
        title=link.title,
        destination_url=link.destination_url,
        placement=link.placement,
        campaign_id=link.campaign_id,
        account_id=link.account_id,
        enabled=link.enabled,
        created_at=link.created_at,
        updated_at=link.updated_at,
        tracking_url=f"{base}/r/{link.code}",
        requests=int((counts or {}).get("requests", 0)),
        transitions=int((counts or {}).get("transitions", 0)),
        unique_daily=int((counts or {}).get("unique_daily", 0)),
    )


async def scoped_link(session, workspace_id, link_id):
    link = await session.scalar(
        select(TrackingLink).where(TrackingLink.id == link_id, TrackingLink.workspace_id == workspace_id)
    )
    if link is None:
        raise HTTPException(404, "Ссылка не найдена")
    return link


async def validate_references(session, workspace_id, payload):
    for model, value in ((Campaign, payload.campaign_id), (TelegramAccount, payload.account_id)):
        if value and not await session.scalar(
            select(model.id).where(model.id == value, model.workspace_id == workspace_id)
        ):
            raise HTTPException(422, "Кампания или аккаунт не принадлежат текущему workspace")


@router.get("")
async def list_links(session: SessionDep, workspace_id: WorkspaceIdDep, days: int = Query(30, ge=1, le=366)):
    counts = {r["link_id"]: r for r in await metrics(session, workspace_id, *period(days))}
    links = (
        await session.scalars(
            select(TrackingLink)
            .where(TrackingLink.workspace_id == workspace_id)
            .order_by(TrackingLink.created_at.desc())
        )
    ).all()
    return [serialize(link, counts.get(link.id)) for link in links]


@router.post("", status_code=201)
async def create_link(payload: LinkInput, session: SessionDep, workspace_id: WorkspaceIdDep):
    await validate_references(session, workspace_id, payload)
    link = TrackingLink(workspace_id=workspace_id, code=secrets.token_urlsafe(12), **payload.model_dump())
    session.add(link)
    await session.commit()
    return serialize(link)


@router.put("/{link_id}")
async def update_link(link_id: str, payload: LinkInput, session: SessionDep, workspace_id: WorkspaceIdDep):
    link = await scoped_link(session, workspace_id, link_id)
    await validate_references(session, workspace_id, payload)
    for key, value in payload.model_dump().items():
        setattr(link, key, value)
    await session.commit()
    return serialize(link)


@router.get("/report")
async def report(session: SessionDep, workspace_id: WorkspaceIdDep, days: int = Query(30, ge=1, le=366)):
    return {
        "timezone": "Europe/Moscow",
        "days": days,
        "rows": await metrics(session, workspace_id, *period(days), daily=True),
    }


@router.get("/export.csv")
async def export(session: SessionDep, workspace_id: WorkspaceIdDep, days: int = Query(30, ge=1, le=366)):
    links = {
        link.id: link
        for link in (await session.scalars(select(TrackingLink).where(TrackingLink.workspace_id == workspace_id))).all()
    }
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Дата (МСК)",
            "Название",
            "Размещение",
            "Кампания ID",
            "Аккаунт ID",
            "URL",
            "Запросы",
            "Переходы",
            "Уникальные за день ≈",
        ]
    )
    for row in await metrics(session, workspace_id, *period(days), daily=True):
        link = links[row["link_id"]]

        def safe(value):
            value = str(value or "")
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

        writer.writerow(
            [
                row["day"],
                safe(link.title),
                safe(link.placement),
                link.campaign_id,
                link.account_id,
                link.destination_url,
                row["requests"],
                row["transitions"],
                row["unique_daily"],
            ]
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="link-transitions.csv"', "Cache-Control": "no-store"},
    )


async def persist_event(link_id: str, request: Request):
    async with AsyncSessionLocal() as session:
        await record_click(session, link_id, request)


@public_router.api_route("/r/{code}", methods=["GET", "HEAD"], include_in_schema=False)
async def redirect(code: str, request: Request, session: SessionDep):
    if len(code) > 32:
        raise HTTPException(404)
    async with asyncio.timeout(3):
        link = await session.scalar(select(TrackingLink).where(TrackingLink.code == code))
    if not link or not link.enabled:
        raise HTTPException(404, "Ссылка недоступна")
    # Release lookup connection before independent event transaction. No Telegram RPCs.
    destination, link_id = link.destination_url, link.id
    await session.rollback()
    if classify_request(request) == "transition":
        request.state.tracking_browser = browser_token(request)
    try:
        async with asyncio.timeout(1):
            await persist_event(link_id, request)
    except Exception:
        # Deliver destination even when analytics storage fails; never retry the user's request.
        log.warning("tracking_event_write_failed link_id=%s", link_id, exc_info=True)
    response = RedirectResponse(
        destination,
        status_code=302,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )

    if getattr(request.state, "tracking_browser", None):
        response.set_cookie(
            "wn_tracking",
            request.state.tracking_browser,
            max_age=30 * 86400,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/r",
        )
    return response
