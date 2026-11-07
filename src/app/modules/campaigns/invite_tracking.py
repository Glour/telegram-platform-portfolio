from datetime import UTC, datetime

from app.modules.telegram_gateway.service import TelegramGatewayError, get_invite_link_stats
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.campaigns.models import Campaign, CampaignInviteLinkMetric
from app.modules.campaigns.schemas import CampaignInviteTrackingConfig, CampaignInviteTrackingResponse
from app.modules.telegram_accounts.models import TelegramAccount

SUPPORTED_INVITE_TRACKING_CAMPAIGN_TYPES = {
    "comment_posts",
    "neuro_chat",
    "neuro_dialog",
    "mass_reactions",
}


def invite_tracking_config_dict(value: CampaignInviteTrackingConfig | dict | None) -> dict | None:
    if value is None:
        return None
    config = value if isinstance(value, CampaignInviteTrackingConfig) else CampaignInviteTrackingConfig(**value)
    return config.model_dump(mode="json", exclude_none=True)


async def upsert_campaign_invite_tracking_metric(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
    config: CampaignInviteTrackingConfig | dict | None,
) -> CampaignInviteLinkMetric | None:
    config_dict = invite_tracking_config_dict(config)
    if not config_dict or campaign.type not in SUPPORTED_INVITE_TRACKING_CAMPAIGN_TYPES:
        if campaign.id:
            await session.execute(
                delete(CampaignInviteLinkMetric).where(
                    CampaignInviteLinkMetric.workspace_id == workspace_id,
                    CampaignInviteLinkMetric.campaign_id == campaign.id,
                )
            )
        return None

    result = await session.execute(
        select(CampaignInviteLinkMetric).where(
            CampaignInviteLinkMetric.workspace_id == workspace_id,
            CampaignInviteLinkMetric.campaign_id == campaign.id,
        )
    )
    metric = result.scalar_one_or_none()
    if metric is None:
        metric = CampaignInviteLinkMetric(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            source_ref=config_dict["source_ref"],
            invite_link=config_dict["invite_link"],
            telegram_account_id=config_dict.get("account_id"),
            raw={},
        )
        session.add(metric)
    else:
        link_changed = (
            metric.source_ref != config_dict["source_ref"]
            or metric.invite_link != config_dict["invite_link"]
            or metric.telegram_account_id != config_dict.get("account_id")
        )
        metric.source_ref = config_dict["source_ref"]
        metric.invite_link = config_dict["invite_link"]
        metric.telegram_account_id = config_dict.get("account_id")
        if link_changed:
            metric.baseline_usage_count = None
            metric.usage_count = None
            metric.requested_count = None
            metric.last_checked_at = None
            metric.last_error_code = None
            metric.last_error_message = None
            metric.raw = {}
    return metric


async def get_campaign_invite_tracking_metric(
    session: AsyncSession,
    workspace_id: str,
    campaign_id: str,
) -> CampaignInviteLinkMetric | None:
    result = await session.execute(
        select(CampaignInviteLinkMetric).where(
            CampaignInviteLinkMetric.workspace_id == workspace_id,
            CampaignInviteLinkMetric.campaign_id == campaign_id,
        )
    )
    return result.scalar_one_or_none()


async def refresh_campaign_invite_tracking_metric(
    session: AsyncSession,
    workspace_id: str,
    campaign: Campaign,
) -> CampaignInviteLinkMetric | None:
    config = invite_tracking_config_dict((campaign.config or {}).get("invite_tracking"))
    metric = await upsert_campaign_invite_tracking_metric(session, workspace_id, campaign, config)
    if metric is None:
        return None

    account_id = metric.telegram_account_id or _first_campaign_account_id(campaign)
    account = await session.get(TelegramAccount, account_id) if account_id else None
    if not account or account.workspace_id != workspace_id:
        _record_metric_error(
            metric,
            "INVITE_TRACKING_ACCOUNT_NOT_FOUND",
            "Telegram account for invite tracking was not found.",
        )
        return metric

    try:
        stats = await get_invite_link_stats(
            session,
            account,
            source_ref=metric.source_ref,
            invite_link=metric.invite_link,
        )
    except TelegramGatewayError as exc:
        _record_metric_error(metric, exc.code, str(exc))
        return metric

    now = datetime.now(UTC)
    metric.telegram_account_id = account.id
    metric.invite_link = stats.invite_link
    metric.title = stats.title
    metric.usage_count = stats.usage_count
    metric.requested_count = stats.requested_count
    if metric.baseline_usage_count is None and stats.usage_count is not None:
        metric.baseline_usage_count = stats.usage_count
    metric.revoked = stats.revoked
    metric.expire_date = stats.expire_date
    metric.usage_limit = stats.usage_limit
    metric.last_checked_at = now
    metric.last_error_code = None
    metric.last_error_message = None
    metric.raw = stats.raw
    return metric


async def refresh_due_campaign_invite_tracking_metrics(
    session: AsyncSession,
    workspace_id: str | None = None,
    *,
    limit: int = 20,
) -> dict:
    query = (
        select(Campaign, CampaignInviteLinkMetric)
        .join(CampaignInviteLinkMetric, CampaignInviteLinkMetric.campaign_id == Campaign.id)
        .where(
            Campaign.status.in_(["pending", "running"]),
            Campaign.type.in_(SUPPORTED_INVITE_TRACKING_CAMPAIGN_TYPES),
        )
        .order_by(CampaignInviteLinkMetric.last_checked_at.asc().nullsfirst())
        .limit(limit)
    )
    if workspace_id:
        query = query.where(Campaign.workspace_id == workspace_id)

    rows = list((await session.execute(query)).all())
    refreshed = 0
    failed = 0
    for campaign, _metric in rows:
        metric = await refresh_campaign_invite_tracking_metric(session, campaign.workspace_id, campaign)
        refreshed += 1
        if metric and metric.last_error_code:
            failed += 1
    return {"refreshed": refreshed, "failed": failed}


def invite_tracking_response(metric: CampaignInviteLinkMetric | None) -> CampaignInviteTrackingResponse | None:
    if metric is None:
        return None
    joined_delta = None
    if metric.usage_count is not None and metric.baseline_usage_count is not None:
        joined_delta = max(0, metric.usage_count - metric.baseline_usage_count)
    return CampaignInviteTrackingResponse.model_validate(metric).model_copy(
        update={"joined_delta": joined_delta},
    )


def invite_tracking_summary(metric: CampaignInviteLinkMetric | None) -> dict | None:
    response = invite_tracking_response(metric)
    return response.model_dump(mode="json") if response else None


def _record_metric_error(metric: CampaignInviteLinkMetric, code: str, message: str) -> None:
    metric.last_checked_at = datetime.now(UTC)
    metric.last_error_code = code
    metric.last_error_message = message[:2000]


def _first_campaign_account_id(campaign: Campaign) -> str | None:
    account_ids = (campaign.config or {}).get("account_ids")
    if not isinstance(account_ids, list):
        return None
    for account_id in account_ids:
        if isinstance(account_id, str) and account_id.strip():
            return account_id
    return None
