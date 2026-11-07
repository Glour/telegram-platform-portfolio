from fastapi import APIRouter

from app.api.entitlements import require_entitlement
from app.modules.account_market.api import router as account_market_router
from app.modules.account_warmup.api import router as warmup_router
from app.modules.ai.api import router as ai_router
from app.modules.assistant.api import router as assistant_router
from app.modules.auth.api import router as auth_router
from app.modules.billing.api import router as billing_router
from app.modules.campaigns.api import router as campaigns_router
from app.modules.channel_map.api import router as channel_map_router
from app.modules.comments.api import router as comments_router
from app.modules.contact.api import router as contact_router
from app.modules.contact_broadcasts.api import router as contact_broadcasts_router
from app.modules.content_factory.api import router as content_factory_router
from app.modules.datasets.api import router as datasets_router
from app.modules.link_tracking.api import router as link_tracking_router
from app.modules.operations.api import router as operations_router
from app.modules.parsing.api import router as parsing_router
from app.modules.plans.api import router as plans_router
from app.modules.proxies.api import router as proxies_router
from app.modules.sources.api import router as sources_router
from app.modules.telegram_accounts.api import router as accounts_router
from app.modules.telegram_manual.api import router as telegram_manual_router
from app.modules.telegram_safety.api import router as telegram_safety_router
from app.modules.workspaces.api import router as workspaces_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(contact_router, prefix="/contact", tags=["contact"])
api_router.include_router(
    contact_broadcasts_router,
    prefix="/contact-broadcasts",
    tags=["saved contact broadcasts"],
    dependencies=[require_entitlement("module.message_broadcast")],
)
api_router.include_router(assistant_router, prefix="/assistant", tags=["assistant"])
api_router.include_router(billing_router, prefix="/billing", tags=["billing"])
api_router.include_router(workspaces_router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(plans_router, prefix="/plans", tags=["plans"])
api_router.include_router(proxies_router, prefix="/proxies", tags=["proxies"])
api_router.include_router(accounts_router, prefix="/telegram-accounts", tags=["telegram accounts"])
api_router.include_router(telegram_safety_router, prefix="/telegram-safety", tags=["telegram safety"])
api_router.include_router(account_market_router, prefix="/account-market", tags=["account market"])
api_router.include_router(
    warmup_router,
    prefix="/warmup",
    tags=["account warmup"],
    dependencies=[require_entitlement("module.warmup")],
)
api_router.include_router(sources_router, prefix="/sources", tags=["sources"])
api_router.include_router(channel_map_router, prefix="/channel-map", tags=["channel map"])
api_router.include_router(parsing_router, prefix="/parsing", tags=["parsing"])
api_router.include_router(ai_router, prefix="/ai", tags=["ai"], dependencies=[require_entitlement("module.ai")])
api_router.include_router(
    campaigns_router,
    prefix="/campaigns",
    tags=["campaigns"],
    dependencies=[require_entitlement("module.neurocommenting")],
)
api_router.include_router(comments_router, prefix="/comments", tags=["comments"])
api_router.include_router(
    content_factory_router,
    prefix="/content-factories",
    tags=["content factories"],
    dependencies=[require_entitlement("module.content_factory")],
)
api_router.include_router(operations_router, prefix="/operations", tags=["operations"])
api_router.include_router(
    datasets_router,
    prefix="/datasets",
    tags=["datasets"],
    dependencies=[require_entitlement("module.datasets")],
)

api_router.include_router(link_tracking_router, prefix="/tracking-links", tags=["tracking links"])

api_router.include_router(telegram_manual_router, prefix="/telegram-manual", tags=["manual telegram"])
