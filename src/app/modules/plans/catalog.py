from app.modules.plans.models import Entitlement, Plan

FREE_MODULE_ENTITLEMENTS = {
    "module.telegram_accounts": 1,
    "module.proxies": 1,
    "module.sources": 1,
}

PAID_MODULE_ENTITLEMENTS = {
    "module.neurocommenting": 1,
    "module.message_broadcast": 1,
    "module.mass_reactions": 1,
    "module.channel_base": 1,
    "module.neurochatting": 1,
    "module.neurodialogs": 1,
    "module.parsing.dialogs": 1,
    "module.parsing.channels": 1,
    "module.parsing.groups": 1,
    "module.parsing.posts": 1,
    "module.parsing.users": 1,
    "module.parsing.comments": 1,
    "module.warmup": 1,
    "module.ai": 1,
    "module.datasets": 1,
    "module.content_factory": 1,
}

MODULE_ENTITLEMENTS = {**FREE_MODULE_ENTITLEMENTS, **PAID_MODULE_ENTITLEMENTS}


READY_PAID_MODULE_CODES = [
    "module.neurocommenting",
    "module.message_broadcast",
    "module.mass_reactions",
    "module.channel_base",
    "module.neurochatting",
    "module.neurodialogs",
    "module.parsing.dialogs",
    "module.parsing.channels",
    "module.parsing.groups",
    "module.parsing.posts",
    "module.parsing.users",
    "module.parsing.comments",
    "module.warmup",
    "module.ai",
    "module.datasets",
    "module.content_factory",
]

FREE_PLAN_CODE = "free"
TRIAL_PLAN_CODE = "trial_7d"
FULL_ACCESS_PLAN_CODE = "all_modules"

MODULE_PRODUCTS = [
    {
        "code": "module.neurocommenting",
        "name": "AI-комментарии",
        "description": "AI-комментарии к новым и историческим постам по промптам, тонам и правилам безопасности.",
        "monthly_price": 3490,
        "yearly_price": 34900,
        "status": "available",
    },
    {
        "code": "module.message_broadcast",
        "name": "Рассылки сообщений",
        "description": "Аккуратная отправка заданного текста по выбранным диалогам через живые Telegram-аккаунты.",
        "monthly_price": 2190,
        "yearly_price": 21900,
        "status": "available",
    },
    {
        "code": "module.mass_reactions",
        "name": "AI реакции",
        "description": "Постановка AI реакций на посты в каналах и группах с лимитами и задержками.",
        "monthly_price": 2190,
        "yearly_price": 21900,
        "status": "available",
    },
    {
        "code": "module.channel_base",
        "name": "База каналов",
        "description": "Каталог Telegram-каналов по тематикам для быстрого подбора источников.",
        "monthly_price": 0,
        "yearly_price": 0,
        "status": "available",
    },
    {
        "code": "module.neurochatting",
        "name": "AI-чат",
        "description": "AI-ответы в группах и обсуждениях для мягкого старта диалогов.",
        "monthly_price": 3490,
        "yearly_price": 34900,
        "status": "available",
    },
    {
        "code": "module.neurodialogs",
        "name": "AI диалоги",
        "description": "AI-общение в личных диалогах по заданным сценариям и ограничениям.",
        "monthly_price": 2990,
        "yearly_price": 29900,
        "status": "available",
    },
    {
        "code": "module.parsing.dialogs",
        "name": "Парсинг диалогов, каналов и групп",
        "description": "Сбор списка диалогов аккаунта, каналов и групп с базовыми метаданными.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.parsing.channels",
        "name": "Парсинг каналов",
        "description": "Сбор Telegram-каналов с базовыми метаданными и рабочей структурой.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.parsing.groups",
        "name": "Парсинг групп",
        "description": "Сбор групп и чатов аккаунта с сохранением в рабочую базу.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.parsing.posts",
        "name": "Парсинг постов",
        "description": "Сбор постов из Telegram-источников с сохранением в рабочую базу.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.parsing.users",
        "name": "Парсинг пользователей",
        "description": "Сбор участников и пользователей из доступных источников.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.parsing.comments",
        "name": "Парсинг комментариев",
        "description": "Сбор комментариев под постами каналов и групп.",
        "monthly_price": 490,
        "yearly_price": 4900,
        "status": "available",
    },
    {
        "code": "module.warmup",
        "name": "Прогрев аккаунтов",
        "description": "Планы прогрева, проверки и чеклисты здоровья аккаунтов перед кампаниями.",
        "monthly_price": 2490,
        "yearly_price": 24900,
        "status": "available",
    },
    {
        "code": "module.content_factory",
        "name": "Контент-завод",
        "description": (
            "Полный цикл Telegram-контента: источники, знания о продукте, рубрики, "
            "генерация постов, картинки, согласование и публикация."
        ),
        "monthly_price": 6990,
        "yearly_price": 69900,
        "status": "available",
    },
]

FULL_ACCESS_PRODUCT = {
    "code": FULL_ACCESS_PLAN_CODE,
    "name": "Все модули",
    "description": "Self-serve доступ ко всем модулям платформы.",
    "monthly_price": 9900,
    "yearly_price": 99000,
    "module_codes": READY_PAID_MODULE_CODES,
}

BUSINESS_MODEL = {
    "currency": "RUB",
    "public_offer": {
        "primary_product_code": "core",
        "primary_period_days": 30,
        "implementation_pilot_price": 59900,
        "implementation_pilot_period_days": 30,
        "automatic_renewal": False,
    },
    "free_plan": {
        "code": FREE_PLAN_CODE,
        "name": "Free",
        "monthly_price": 0,
        "description": "Кабинет, аккаунты, прокси, источники и настройки без платных рабочих модулей.",
    },
    "trial_plan": {
        "code": TRIAL_PLAN_CODE,
        "name": "Пробный доступ",
        "duration_days": 7,
        "monthly_price": 0,
        "description": "7 дней доступа ко всем рабочим модулям с ограниченными лимитами для первого теста.",
    },
    "base_platform_fee": 4900,
    "self_serve_plans": [
        {
            "code": "start",
            "name": "Start",
            "monthly_price": 4990,
            "yearly_price": 49900,
            "description": "AI-комментарии, рассылки сообщений, база каналов и прогрев для первого рабочего контура.",
        },
        {
            "code": "core",
            "name": "Core",
            "monthly_price": 9900,
            "yearly_price": 99000,
            "description": (
                "Все доступные модули платформы, включая контент-завод, и повышенные лимиты для регулярной работы."
            ),
        },
        {
            "code": "agency",
            "name": "Agency",
            "monthly_price": 19900,
            "yearly_price": 199000,
            "description": "Командный тариф с расширенными лимитами и несколькими клиентскими контурами.",
        },
    ],
    "recommended_bundle": {
        "name": "Core",
        "monthly_price": 9900,
        "yearly_price": 99000,
        "modules": READY_PAID_MODULE_CODES,
    },
    "agency_bundle": {
        "name": "Agency",
        "monthly_price": 19900,
        "yearly_price": 199000,
        "modules": READY_PAID_MODULE_CODES,
    },
    "add_ons": [
        {"code": "module.neurocommenting", "name": "AI-комментарии", "monthly_price": 3490, "yearly_price": 34900},
        {
            "code": "module.message_broadcast",
            "name": "Рассылки сообщений",
            "monthly_price": 2190,
            "yearly_price": 21900,
        },
        {"code": "module.warmup", "name": "Прогрев аккаунтов", "monthly_price": 2490, "yearly_price": 24900},
        {"code": "module.content_factory", "name": "Контент-завод", "monthly_price": 6990, "yearly_price": 69900},
    ],
    "compliance_notes": [
        "Без обещаний обхода банов: используем язык здоровья аккаунтов, лимитов и безопасных сценариев.",
        "Все рабочие модули открыты в кабинете; оплата управляет коммерческими условиями, а не видимостью разделов.",
    ],
}


def free_plan_limits() -> dict[str, int]:
    return {
        **FREE_MODULE_ENTITLEMENTS,
        "telegram_accounts": 10,
        "active_campaigns": 0,
        "sources": 100,
    }


def trial_plan_limits() -> dict[str, int]:
    return {
        **MODULE_ENTITLEMENTS,
        "telegram_accounts": 2,
        "active_campaigns": 2,
        "sources": 200,
        "workspace_members": 1,
    }


INTERNAL_UNLIMITED_LIMIT = 1_000_000

BASE_LIMITS = {
    "telegram_accounts": 3,
    "active_campaigns": 3,
    "sources": 200,
}

INTERNAL_UNLIMITED_LIMITS = {
    "telegram_accounts": INTERNAL_UNLIMITED_LIMIT,
    "active_campaigns": INTERNAL_UNLIMITED_LIMIT,
    "sources": INTERNAL_UNLIMITED_LIMIT,
    "comments_monthly": INTERNAL_UNLIMITED_LIMIT,
    "workspace_members": INTERNAL_UNLIMITED_LIMIT,
    "module.reports": 1,
    "module.parsing": 1,
}


def internal_mvp_limits() -> dict[str, int]:
    return {**INTERNAL_UNLIMITED_LIMITS, **MODULE_ENTITLEMENTS}


def module_feature_map() -> dict[str, str]:
    return {
        "telegram_accounts": "module.telegram_accounts",
        "proxies": "module.proxies",
        "sources": "module.sources",
        "channel_map": "module.channel_base",
        "warmup": "module.warmup",
        "parsing": "module.parsing.dialogs",
        "ai": "module.ai",
        "campaigns": "module.neurocommenting",
        "comments": "module.neurocommenting",
        "message_broadcast": "module.message_broadcast",
        "mass_reactions": "module.mass_reactions",
        "content_factory": "module.content_factory",
        "datasets": "module.datasets",
    }


def apply_plan_entitlements(workspace_id: str, plan: Plan) -> list[Entitlement]:
    return [
        Entitlement(
            workspace_id=workspace_id,
            feature_code=feature_code,
            limit_value=int(limit_value),
            reset_period="month",
        )
        for feature_code, limit_value in (plan.limits or {}).items()
    ]
