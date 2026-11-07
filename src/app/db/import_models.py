"""Import all SQLAlchemy models so Alembic can discover metadata."""

from app.modules.account_market.models import AccountMarketOffer, AccountMarketRequest  # noqa: F401
from app.modules.account_trust.models import AccountRiskFactor, AccountTrustSnapshot  # noqa: F401
from app.modules.account_warmup.models import WarmupPlan, WarmupRun, WarmupStep  # noqa: F401
from app.modules.ai.models import AiGeneration, AiProfile, CommentPromptTemplate  # noqa: F401
from app.modules.assistant.models import (  # noqa: F401
    AssistantConversation,
    AssistantFeedback,
    AssistantKnowledgeChunk,
    AssistantKnowledgeSource,
    AssistantMessage,
)
from app.modules.audit.models import AuditLog  # noqa: F401
from app.modules.auth.models import ApiKey, AuthToken, OAuthAccount, OAuthState, User  # noqa: F401
from app.modules.billing.models import BillingCart, PaymentOrder  # noqa: F401
from app.modules.campaigns.models import (  # noqa: F401
    Campaign,
    CampaignAction,
    CampaignEvent,
    CampaignInviteLinkMetric,
    CampaignRun,
    CampaignTarget,
)
from app.modules.comments.models import TelegramComment  # noqa: F401
from app.modules.contact_broadcasts.models import ContactBroadcastMedia, ContactBroadcastSnapshot  # noqa: F401
from app.modules.content_factory.models import (  # noqa: F401
    ContentFactory,
    ContentFactoryEvent,
    ContentFactoryKnowledgeItem,
    ContentFactoryRubric,
    ContentFactoryRun,
    ContentFactorySource,
    ContentMemoryItem,
    ContentPost,
)
from app.modules.datasets.models import CommentDatasetItem  # noqa: F401
from app.modules.link_tracking.models import TrackingClick, TrackingLink  # noqa: F401
from app.modules.operations.models import OperationalEvent  # noqa: F401
from app.modules.parsing.models import ParsedTelegramComment, ParsingJob, ParsingJobLog, ParsingResult  # noqa: F401
from app.modules.plans.models import Entitlement, Plan, Subscription, UsageEvent  # noqa: F401
from app.modules.proxies.models import Proxy, ProxyCheck  # noqa: F401
from app.modules.sources.models import (  # noqa: F401
    TelegramMessage,
    TelegramSource,
    TelegramSourceBlacklistEntry,
    TelegramSourceFolder,
    TelegramUser,
)
from app.modules.telegram_accounts.models import (  # noqa: F401
    AccountHealthEvent,
    AccountQualityCheck,
    RiskDecision,
    TelegramAccount,
    TelegramSession,
)
from app.modules.telegram_safety.models import TelegramLoginSafety, TelegramOperation, TelegramSafetyState  # noqa: F401
from app.modules.workspaces.models import Workspace, WorkspaceMember  # noqa: F401
