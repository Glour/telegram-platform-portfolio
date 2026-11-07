from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy import update as sqlalchemy_update

from app.api.deps import SessionDep, WorkspaceIdDep
from app.core.errors import not_found
from app.modules.campaigns.models import Campaign
from app.modules.campaigns.schemas import CampaignResponse
from app.modules.proxies.models import Proxy, ProxyCheck
from app.modules.proxies.schemas import (
    ProxyAssignAccount,
    ProxyAssignCampaign,
    ProxyBulkActionRequest,
    ProxyBulkActionResponse,
    ProxyBulkCheckRequest,
    ProxyBulkCheckResponse,
    ProxyCheckResponse,
    ProxyCreate,
    ProxyResponse,
    ProxyUpdate,
    ProxyUsageResponse,
)
from app.modules.proxies.service import (
    bulk_check_proxies,
    create_proxy,
    create_proxy_check,
    ensure_proxy_account_capacity,
    update_proxy,
)
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_accounts.schemas import TelegramAccountResponse

router = APIRouter()


@router.get("", response_model=list[ProxyResponse])
async def list_proxies(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[dict[str, object]]:
    result = await session.execute(
        select(Proxy)
        .where(Proxy.workspace_id == workspace_id, Proxy.status != "deleted")
        .order_by(Proxy.created_at.desc())
    )
    proxies = list(result.scalars().all())
    if not proxies:
        return []

    proxy_ids = [proxy.id for proxy in proxies]
    counts_result = await session.execute(
        select(TelegramAccount.proxy_id, func.count(TelegramAccount.id))
        .where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.proxy_id.in_(proxy_ids),
            TelegramAccount.status.notin_(["archived", "deleted"]),
        )
        .group_by(TelegramAccount.proxy_id)
    )
    account_counts = {proxy_id: int(account_count) for proxy_id, account_count in counts_result.all() if proxy_id}

    checks_result = await session.execute(
        select(ProxyCheck)
        .where(
            ProxyCheck.workspace_id == workspace_id,
            ProxyCheck.proxy_id.in_(proxy_ids),
        )
        .order_by(ProxyCheck.created_at.desc())
    )
    latest_checks: dict[str, ProxyCheck] = {}
    for check in checks_result.scalars().all():
        latest_checks.setdefault(check.proxy_id, check)

    return [
        {
            "id": proxy.id,
            "workspace_id": proxy.workspace_id,
            "type": proxy.type,
            "host": proxy.host,
            "port": proxy.port,
            "status": proxy.status,
            "country": proxy.country,
            "provider": proxy.provider,
            "city_state": proxy.city_state,
            "proxy_class": proxy.proxy_class,
            "exclusivity": proxy.exclusivity,
            "sticky_until": proxy.sticky_until,
            "last_ip_detected": proxy.last_ip_detected,
            "last_country_detected": proxy.last_country_detected,
            "telegram_connectivity_status": proxy.telegram_connectivity_status,
            "max_accounts": proxy.max_accounts,
            "note": proxy.note,
            "account_count": account_counts.get(proxy.id, 0),
            "latest_latency_ms": latest_checks[proxy.id].latency_ms if proxy.id in latest_checks else None,
            "latest_telegram_latency_ms": latest_checks[proxy.id].telegram_latency_ms
            if proxy.id in latest_checks
            else None,
            "latest_checked_at": latest_checks[proxy.id].created_at if proxy.id in latest_checks else None,
        }
        for proxy in proxies
    ]


@router.post("", response_model=ProxyResponse, status_code=201)
async def create(payload: ProxyCreate, session: SessionDep, workspace_id: WorkspaceIdDep) -> Proxy:
    return await create_proxy(session, workspace_id, payload)


@router.post("/bulk-check", response_model=ProxyBulkCheckResponse)
async def bulk_check(
    payload: ProxyBulkCheckRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ProxyBulkCheckResponse:
    return await bulk_check_proxies(session, workspace_id, payload)


@router.post("/bulk/delete", response_model=ProxyBulkActionResponse)
async def bulk_delete(
    payload: ProxyBulkActionRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ProxyBulkActionResponse:
    proxy_ids = list(dict.fromkeys(payload.proxy_ids))
    result = await session.execute(
        sqlalchemy_update(Proxy)
        .where(
            Proxy.workspace_id == workspace_id,
            Proxy.id.in_(proxy_ids),
            Proxy.status != "deleted",
        )
        .values(status="deleted")
    )
    await session.commit()
    return ProxyBulkActionResponse(
        status="accepted",
        action="delete",
        proxy_count=len(proxy_ids),
        target_count=int(result.rowcount or 0),
    )


@router.patch("/{proxy_id}", response_model=ProxyResponse)
async def update(proxy_id: str, payload: ProxyUpdate, session: SessionDep, workspace_id: WorkspaceIdDep) -> Proxy:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    return await update_proxy(session, proxy, payload)


@router.delete("/{proxy_id}", status_code=204)
async def delete(proxy_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> Response:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    proxy.status = "deleted"
    await session.commit()
    return Response(status_code=204)


@router.post("/{proxy_id}/check", response_model=ProxyCheckResponse)
async def check(proxy_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> object:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    return await create_proxy_check(session, workspace_id, proxy)


@router.get("/{proxy_id}/usage", response_model=ProxyUsageResponse)
async def usage(proxy_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> dict[str, object]:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    accounts_result = await session.execute(
        select(TelegramAccount)
        .where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.proxy_id == proxy.id,
            TelegramAccount.status.notin_(["archived", "deleted"]),
        )
        .order_by(TelegramAccount.created_at.desc())
    )
    accounts = list(accounts_result.scalars().all())

    campaigns_result = await session.execute(
        select(Campaign)
        .where(Campaign.workspace_id == workspace_id, Campaign.status != "deleted")
        .order_by(Campaign.created_at.desc())
    )
    campaigns = [
        campaign
        for campaign in campaigns_result.scalars().all()
        if isinstance(campaign.policy, dict) and campaign.policy.get("proxy_id") == proxy.id
    ]

    return {
        "proxy_id": proxy.id,
        "accounts": [
            {
                "id": account.id,
                "username": account.username,
                "display_name": account.display_name,
                "status": account.status,
            }
            for account in accounts
        ],
        "campaigns": [
            {
                "id": campaign.id,
                "name": campaign.name,
                "status": campaign.status,
            }
            for campaign in campaigns
        ],
        "account_count": len(accounts),
        "campaign_count": len(campaigns),
    }


@router.post("/{proxy_id}/assign-account", response_model=TelegramAccountResponse)
async def assign_account(
    proxy_id: str,
    payload: ProxyAssignAccount,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> TelegramAccount:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    account = await session.get(TelegramAccount, payload.telegram_account_id)
    if not account or account.workspace_id != workspace_id:
        raise not_found("Telegram account not found")

    await ensure_proxy_account_capacity(session, workspace_id, proxy, exclude_account_id=account.id)
    account.proxy_id = proxy.id
    account.proxy_required = True
    await session.commit()
    await session.refresh(account)
    return account


@router.post("/{proxy_id}/assign-campaign", response_model=CampaignResponse)
async def assign_campaign(
    proxy_id: str,
    payload: ProxyAssignCampaign,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> Campaign:
    proxy = await _get_workspace_proxy(session, workspace_id, proxy_id)
    campaign = await session.get(Campaign, payload.campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise not_found("Campaign not found")

    campaign.policy = {**(campaign.policy or {}), "proxy_id": proxy.id}
    await session.commit()
    await session.refresh(campaign)
    return campaign


async def _get_workspace_proxy(session: SessionDep, workspace_id: str, proxy_id: str) -> Proxy:
    proxy = await session.get(Proxy, proxy_id)
    if not proxy or proxy.workspace_id != workspace_id:
        raise not_found("Proxy not found")
    return proxy
