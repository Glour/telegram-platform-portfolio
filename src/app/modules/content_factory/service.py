# ruff: noqa: E501
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from app.modules.telegram_gateway.service import (
    TelegramGatewayError,
    fetch_account_premium_emoji_catalog,
    fetch_posts_with_account,
    real_telegram_enabled,
    send_channel_post_with_account,
    source_ref_from_fields,
)
from fastapi import UploadFile
from sqlalchemy import func, nullsfirst, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.ai.humanizer import (
    HUMANIZER_PROMPT_VERSION,
    AuthorGender,
    author_gender_prompt_block,
    build_humanizer_system_prompt,
    humanizer_violations,
    normalize_author_gender,
)
from app.modules.ai.service import AiProviderError, generate_profile_avatar_image, generate_text_response
from app.modules.content_factory.models import (
    ContentFactory,
    ContentFactoryEvent,
    ContentFactoryKnowledgeItem,
    ContentFactoryRubric,
    ContentFactoryRun,
    ContentFactorySource,
    ContentMemoryItem,
    ContentPost,
)
from app.modules.content_factory.schemas import (
    ContentFactoryCreate,
    ContentFactoryKnowledgeCreate,
    ContentFactoryRubricCreate,
    ContentFactorySourceCreate,
    ContentFactoryUpdate,
    ContentPostUpdate,
)
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount

ACTIVE_RUN_STATUSES = {"queued", "researching", "writing", "reviewing", "publishing"}
CONTENT_FACTORY_WEEKDAY_VALUES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CONTENT_FACTORY_MAX_POSTS_PER_DAY = 10
CONTENT_FACTORY_WORKSPACE_DAILY_GENERATION_LIMIT = 10
CONTENT_FACTORY_PREMIUM_EMOJI_CATALOG_LIMIT = 96
SENSITIVE_KNOWLEDGE_IMAGE_TOKENS = {
    "admin",
    "adminka",
    "админ",
    "operations",
    "operation-events",
    "sentry",
    "telegram-alerts",
}
PRODUCT_MODULE_FOCUS_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "traffic_system",
        "name": "Telegram-трафик и лидогенерация",
        "aliases": (
            "telegram-трафик",
            "трафик",
            "лидогенерац",
            "лид",
            "воронк",
            "посев",
            "касание",
            "оффер",
            "аудитор",
            "канал",
        ),
        "features": "источники, аккаунты, комментарии, кампании, контент, статусы, логи и следующий шаг по лиду",
        "benefit": "помогает смотреть на Telegram как на управляемую систему трафика, а не набор отдельных действий.",
        "screenshot_tokens": ("landing", "home", "platform", "overview", "sources", "comments", "accounts", "campaign"),
    },
    {
        "id": "content_factory",
        "name": "Контент-завод",
        "aliases": (
            "контент-завод",
            "контент завод",
            "content factory",
            "черновик",
            "черновики",
            "drafts_only",
            "quality-check",
            "ручное согласование",
            "редактирование поста",
            "генерация поста",
            "расписание публикаций",
            "база знаний",
            "изображения для постов",
            "источник для поста",
        ),
        "features": "черновики, режим drafts_only, ручное согласование, редактирование текста и фото, расписание, база знаний, источники, quality-check",
        "benefit": "помогает вести Telegram-канал как редакционный процесс: от темы и материалов до готового поста.",
        "screenshot_tokens": (
            "content-factory",
            "content_factory",
            "контент",
            "factory",
            "post",
            "draft",
            "knowledge",
            "base",
        ),
    },
    {
        "id": "warmup",
        "name": "Прогрев аккаунтов",
        "aliases": ("прогрев", "warmup", "новый аккаунт", "активность аккаунт"),
        "features": "сценарии прогрева, статусы шагов, паузы, лимиты, история действий",
        "benefit": "помогает не смешивать свежий аккаунт с резкими активными действиями.",
        "screenshot_tokens": ("warmup", "прогрев", "account-warmup"),
    },
    {
        "id": "accounts",
        "name": "Аккаунты",
        "aliases": ("аккаунт", "статус аккаун", "сесс", "telegram account"),
        "features": "статусы аккаунтов, ограничения, профиль, прокси, готовность к работе",
        "benefit": "показывает, какие аккаунты готовы, какие требуют паузы или проверки.",
        "screenshot_tokens": ("accounts", "account", "аккаунт"),
    },
    {
        "id": "sources",
        "name": "Источники",
        "aliases": ("источник", "парсинг", "сырь", "канал-источник", "source"),
        "features": "подключение Telegram-каналов, сбор постов, сырьё, фактура для генерации",
        "benefit": "даёт контент-заводу фактуру, а не заставляет писать посты из воздуха.",
        "screenshot_tokens": ("sources", "source", "сырье", "raw"),
    },
    {
        "id": "comments",
        "name": "AI-комментарии",
        "aliases": ("комментар", "neurocomment", "ai-коммент"),
        "features": "генерация комментариев, ручная проверка, статусы, ограничения",
        "benefit": "помогает готовить комментарии аккуратно, без одинакового спама.",
        "screenshot_tokens": ("comments", "comment", "ai-comments"),
    },
    {
        "id": "proxies",
        "name": "Прокси",
        "aliases": ("прокси", "proxy"),
        "features": "привязка прокси, проверки доступности, связь с аккаунтами",
        "benefit": "помогает видеть техническую основу аккаунтов в одном кабинете.",
        "screenshot_tokens": ("proxies", "proxy", "прокси"),
    },
    {
        "id": "operations",
        "name": "Операции и логи",
        "aliases": ("лог", "событ", "операц", "ошиб", "алерт", "статус процесс"),
        "features": "журнал событий, ошибки, статусы фоновых процессов, проверки",
        "benefit": "показывает, где процесс завис и что требует внимания.",
        "screenshot_tokens": ("operations", "events", "logs", "status"),
    },
)
DEFAULT_CONTENT_PROMPT = (
    "Ты редактор Telegram-канала. Пиши полезные авторские посты для заданной аудитории, "
    "используй источники и базу знаний, не копируй чужие тексты дословно, отделяй факты от мнений "
    "и не обещай результатов, которых нельзя гарантировать."
)
DEFAULT_IMAGE_PROMPT = (
    "Создавай выразительную editorial-обложку под конкретный смысл поста, а не абстрактный интерфейс. "
    "Сначала пойми тему поста и выбери визуальную метафору: продуктовый сценарий, работа команды, источник -> черновик -> публикация, "
    "ошибка до запуска, расписание, проверка качества, аккуратная Telegram-коммуникация. "
    "Избегай однообразных синих голограмм, sci-fi dashboards, floating UI panels, generic network nodes, одинаковых digital control rooms и текста на изображении. "
    "Если пост разбирает конкретный экран продукта и в базе знаний есть подходящий публичный скриншот, можно использовать USE_KNOWLEDGE_IMAGE; "
    "иначе генерируй живую, премиальную, предметную сцену с сильным foreground subject, реалистичным светом и понятной связью с темой."
)
CONTENT_FACTORY_HUMANIZER_LAYER = """
Content factory humanizer layer.
This layer is mandatory for every Russian Telegram post generated by content factory.

Use the Humanizer RU rules as the default voice layer: concrete, alive, direct, non-bureaucratic, without AI gloss.
For Telegram posts, preserve useful channel formatting when it is requested by the factory: short paragraphs, Markdown links,
bold accents, footer links and meaningful emoji. Do not turn the post into a comment, a support reply or a landing page.

Style reference distilled from strong Telegram product channels:
- write like a founder or channel editor who really uses the product, not like a brand brochure;
- start from a live situation, update, user question, bug, cost, mistake, screen or small operational pain;
- explain what changed, where to click, what to check, what risk disappears or what gets easier;
- one post = one practical point; do not dissolve it into abstract "control", "process" or "order";
- it is okay to sound conversational: "на практике", "обычно ломается вот здесь", "мы сделали так", "если у вас...";
- keep the voice simple and confident, with occasional human imperfection, but without sloppy grammar;
- if the post mentions a module, name it and show the user scenario inside that module.

Hard content-factory rules:
- no em dash or en dash punctuation in final text;
- no Russian angle quotes or smart quotes in final text;
- no generic AI starters and endings;
- no "Таким образом", "Важно отметить", "Стоит понимать", "В заключение";
- no repeated brochure rhythm and no sterile expert-summary tone;
- no mechanical list if the topic works better as paragraphs;
- keep the requested Telegram length from the start, including footer and Markdown links.
- every post must be grounded in a concrete product module, feature, screen or workflow when the knowledge base contains product context.
- every product post needs at least one concrete user action or scenario: open a module, add a source, upload a file, approve a draft, pause an account, check logs, change a schedule, review a warning.
- do not write several posts in a row around the same vague idea; change the module, user situation, angle or proof.
""".strip()
EDITORIAL_STYLE_GUIDE = """
Референс формата Telegram-поста задаёт уровень живости, а не жёсткий шаблон.
Каждый пост должен быть самостоятельным: читатель с первой пары строк понимает, о каком продукте, модуле, сценарии или проблеме идёт речь.

Правила редактора:
- пиши как фаундер или редактор, который показывает продукт изнутри: проще, живее, честнее, без корпоративной гладкости;
- хороший заход выглядит как конкретная сцена: человек открыл модуль, добавил источник, получил черновик, увидел ошибку, не понял статус, упёрся в лимит, проверил расписание;
- после захода быстро отвечай на вопрос "и что с этим делать": какая функция помогает, какой экран нужен, какой следующий шаг у пользователя;
- формат похож на живой продуктовый Telegram-канал: сильный **заголовок**, короткое объяснение фичи/сценария, 1-2 полезных блока и понятная ссылка;
- обязательно вставляй хотя бы одну Markdown-ссылку на продукт, сайт, модуль, источник или целевую страницу, если ссылка есть в базе знаний или настройках завода;
- в конце поста нужен компактный footer/CTA с ссылкой на платформу или канал, а не обезличенный финал;
- если пост ссылается на источник или статью, ссылка должна быть встроена в текст рядом с тезисом, а не лежать отдельно без контекста;
- не повторяй структуру, заголовочный emoji, начало и финальный CTA из последних постов;
- emoji необязателен; если используешь, максимум один в заголовке и выбирай по смыслу, не по привычке;
- не начинай несколько постов подряд с одинаковой конструкции вроде «Когда…», «Вот как это работает», «Таким образом»;
- не злоупотребляй словами «Telegram-операционка», «хаос», «контроль», «логи», «статусы»; используй их только когда пост реально про эти сущности;
- каждый пост выбирает один конкретный фокус: модуль, экран, сценарий пользователя, ошибка, решение, кейс или обновление; не пиши общий пост «про порядок в Telegram» без нового угла;
- абзацы короткие, между смысловыми блоками обязательно пустая строка, в JSON это \\n\\n;
- Telegram Markdown используй заметно, но аккуратно: **жирный заголовок**, 1-4 выделения внутри текста, Markdown-ссылки вида [текст](https://...);
- можно использовать blockquote `> важный вывод` для сильной мысли, цены, предупреждения или наблюдения;
- пост не должен звучать как абстрактная методичка: называй конкретный экран, модуль, пользовательский сценарий, риск или решение;
- избегай канцелярских продуктовых слов без доказательства: «операционный контур», «наблюдаемость», «масштабирование», «управляемая система». Если используешь их, сразу привяжи к экрану или действию;
- не пиши «в demo-platform удобно», пока не сказал, что именно удобно сделать;
- нормальный пост может быть чуть разговорным: «обычно тут и начинается путаница», «на этом месте команда часто спешит», «я бы начинал с проверки статуса»;
- если фактов из базы знаний мало, честно пиши как наблюдение/мнение, а не как обещание;
- не обещай отсутствие банов, гарантированный рост, обход ограничений или агрессивную массовую активность.
""".strip()
EDITORIAL_FORMAT_VARIANTS = """
Выбирай один редакторский сценарий и адаптируй его под тему, не копируя формулировки:
1. Разбор модуля: «Сегодня разберём…» → что это за модуль → кому полезен → как использовать.
2. Пользовательская ситуация: короткая сцена/боль → почему возникает → как платформа помогает навести порядок.
3. Антипример: как обычно делают плохо → что ломается → более аккуратный подход.
4. Мини-гайд: 3-5 практических шагов без канцелярита.
5. Чеклист проверки: что посмотреть перед запуском/публикацией/кампанией.
6. До/после: как процесс выглядит без системы и как выглядит с платформой.
7. Вопрос-ответ: один понятный вопрос пользователя и развёрнутый ответ.
8. Наблюдение/мнение: конкретный тезис → аргументы → вывод без продажи в лоб.
9. Обновление/релиз: что добавили → зачем это понадобилось → как пользователь применит сегодня.
10. Микро-кейс: было неудобно → где именно ломалось → как теперь выглядит рабочий путь.

Важно: если последние посты уже были списком с 1️⃣ 2️⃣ 3️⃣ или блоком «Вот как это работает», выбери другой сценарий.
""".strip()
MAX_KNOWLEDGE_UPLOAD_FILES = 30
MAX_KNOWLEDGE_UPLOAD_BYTES = 20 * 1024 * 1024
IMAGE_KNOWLEDGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_KNOWLEDGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_SITE_CONTEXT_BYTES = 1_500_000
IMAGE_RETRY_MAX_ATTEMPTS = 6
IMAGE_RETRY_MAX_DELAY_SECONDS = 120.0
IMAGE_RETRY_MIN_DELAY_SECONDS = 2.0
IMAGE_RATE_LIMIT_PER_MINUTE_DELAY_SECONDS = 65.0
IMAGE_RETRY_MAX_TOTAL_WAIT_SECONDS = 300.0
TELEGRAM_MEDIA_CAPTION_LIMIT_DEFAULT = 1000
TELEGRAM_MEDIA_CAPTION_LIMIT_PREMIUM = 2000


@dataclass
class SiteContextPage:
    url: str
    title: str
    text: str
    links: list[str]


@dataclass(frozen=True)
class KnowledgeFilePayload:
    filename: str
    content_type: str
    data: bytes
    suffix: str


@dataclass(frozen=True)
class PremiumEmojiEntry:
    alias: str
    emoji: str
    custom_emoji_id: str


@dataclass(frozen=True)
class ContentFactorySlotBrief:
    trigger_type: str
    posts_per_day: int
    slot_index: int
    slot_count: int
    slot_local_time: str
    weekday_label: str
    day_theme: str
    subtopic: str
    angle: str

    def as_prompt_block(self) -> str:
        return "\n".join(
            [
                f"Тип запуска: {self.trigger_type}",
                f"Слот дня: {self.slot_index + 1} из {self.slot_count}",
                f"Локальное время слота: {self.slot_local_time}",
                f"День недели: {self.weekday_label}",
                f"Тема дня: {self.day_theme}",
                f"Подтема этого поста: {self.subtopic}",
                f"Редакторский угол: {self.angle}",
            ]
        )


class ContentFactoryError(RuntimeError):
    pass


async def normalize_content_config_for_telegram_limits(
    session: AsyncSession,
    *,
    workspace_id: str,
    publisher_account_id: str | None,
    content_config: dict,
    image_config: dict,
) -> dict:
    normalized = dict(content_config)
    if "author_gender" in normalized:
        normalized["author_gender"] = normalize_author_gender(normalized.get("author_gender"))
    if not image_config.get("enabled"):
        normalized.pop("telegram_caption_limit", None)
        normalized.pop("telegram_caption_limit_source", None)
        return normalized

    account = await _get_publisher_account_for_limits(session, workspace_id, publisher_account_id)
    caption_limit = telegram_media_caption_limit_for_account(account)
    requested_max_length = _int_config_value(normalized.get("max_length"), 1200)
    requested_min_length = _int_config_value(normalized.get("min_length"), min(700, caption_limit))
    normalized["max_length"] = min(requested_max_length, caption_limit)
    normalized["min_length"] = min(requested_min_length, int(normalized["max_length"]))
    normalized["telegram_caption_limit"] = caption_limit
    normalized["telegram_caption_limit_source"] = "premium" if account and account.is_premium else "default"
    return normalized


async def _get_publisher_account_for_limits(
    session: AsyncSession,
    workspace_id: str,
    publisher_account_id: str | None,
) -> TelegramAccount | None:
    if not publisher_account_id:
        return None
    account = await session.get(TelegramAccount, publisher_account_id)
    if not account or account.workspace_id != workspace_id:
        return None
    return account


def telegram_media_caption_limit_for_account(account: TelegramAccount | None) -> int:
    if account and account.is_premium:
        return TELEGRAM_MEDIA_CAPTION_LIMIT_PREMIUM
    return TELEGRAM_MEDIA_CAPTION_LIMIT_DEFAULT


def normalize_schedule_config(config: dict) -> dict:
    normalized = dict(config)
    normalized["posts_per_day"] = max(
        1, min(CONTENT_FACTORY_MAX_POSTS_PER_DAY, parse_int(normalized.get("posts_per_day"), 1))
    )
    return normalized


def normalize_content_config(config: dict, *, previous_config: dict | None = None, bump_revision: bool = False) -> dict:
    normalized = dict(config)
    plan = normalize_content_plan(normalized.get("content_plan") or normalized.get("weekly_content_plan"))
    if plan:
        normalized["content_plan"] = plan
    else:
        normalized.pop("content_plan", None)
    normalized.pop("weekly_content_plan", None)
    revision = parse_int((previous_config or {}).get("config_revision") or normalized.get("config_revision"), 0)
    normalized["config_revision"] = revision + 1 if bump_revision else max(1, revision or 1)
    return normalized


def normalize_content_plan(value: object) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(value or "").strip())[:12000]


def _int_config_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def create_factory(
    session: AsyncSession,
    *,
    workspace_id: str,
    created_by: str,
    payload: ContentFactoryCreate,
) -> ContentFactory:
    content_config = dict(payload.content_config)
    image_config = dict(payload.image_config)
    quality_config = dict(payload.quality_config)
    schedule_config = normalize_schedule_config(dict(payload.schedule_config))
    content_config = normalize_content_config(content_config)
    content_config = await normalize_content_config_for_telegram_limits(
        session,
        workspace_id=workspace_id,
        publisher_account_id=payload.publisher_account_id,
        content_config=content_config,
        image_config=image_config,
    )

    knowledge_items = list(payload.knowledge_items)
    rubrics = list(payload.rubrics)
    factory = ContentFactory(
        workspace_id=workspace_id,
        name=payload.name,
        status="active" if payload.activate else "draft",
        target_type=payload.target_type,
        target_ref=payload.target_ref,
        publisher_account_id=payload.publisher_account_id,
        mode=payload.mode,
        schedule_config=schedule_config,
        content_config=content_config,
        image_config=image_config,
        quality_config=quality_config,
        next_run_at=content_factory_next_run_at(schedule_config, datetime.now(UTC)) if payload.activate else None,
        created_by=created_by,
    )
    session.add(factory)
    await session.flush()

    seen_source_ids: set[str] = set()
    for source_id in payload.source_ids:
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        session.add(
            ContentFactorySource(
                factory_id=factory.id,
                source_id=source_id,
                source_type="telegram_channel",
                settings={
                    "collect_policy": "regular_new_posts",
                    "usage": "content_inputs",
                },
            )
        )
    for ref in split_source_refs(payload.manual_source_refs):
        source = await find_or_create_telegram_source(session, workspace_id, ref)
        if source.id in seen_source_ids:
            continue
        seen_source_ids.add(source.id)
        session.add(
            ContentFactorySource(
                factory_id=factory.id,
                source_id=source.id,
                source_ref=ref,
                source_type="telegram_channel",
                settings={
                    "collect_policy": "regular_new_posts",
                    "usage": "content_inputs",
                    "manual_ref": True,
                },
            )
        )
    for item in knowledge_items:
        _add_knowledge_item(session, factory.id, item)
    for rubric in rubrics or _default_rubrics():
        session.add(
            ContentFactoryRubric(
                factory_id=factory.id,
                name=rubric.name,
                description=rubric.description,
                target_share_percent=rubric.target_share_percent,
                format_preset=rubric.format_preset,
                prompt_override=rubric.prompt_override,
                enabled=rubric.enabled,
            )
        )

    await add_event(session, factory.id, None, None, "info", "factory.created", "Контент-завод создан", {})
    await session.commit()
    await session.refresh(factory)
    return factory


async def find_or_create_telegram_source(session: AsyncSession, workspace_id: str, ref: str) -> TelegramSource:
    username, url, title = normalize_source_ref(ref)
    if not username and not url:
        raise ContentFactoryError("CONTENT_FACTORY_INVALID_SOURCE_REF")
    existing = await find_existing_telegram_source(session, workspace_id, username, url)
    if existing:
        return existing
    source = TelegramSource(
        workspace_id=workspace_id,
        type="channel",
        username=username,
        url=url,
        title=title,
        access_status="pending_check",
        source_metadata={
            "created_by": "content_factory",
            "collect_policy": "regular_new_posts",
        },
    )
    session.add(source)
    await session.flush()
    return source


async def find_existing_telegram_source(
    session: AsyncSession,
    workspace_id: str,
    username: str | None,
    url: str | None,
) -> TelegramSource | None:
    if username:
        result = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                func.lower(TelegramSource.username) == username.lower(),
            )
        )
        source = result.scalar_one_or_none()
        if source:
            return source
    if url:
        result = await session.execute(
            select(TelegramSource).where(
                TelegramSource.workspace_id == workspace_id,
                TelegramSource.url == url,
            )
        )
        return result.scalar_one_or_none()
    return None


async def update_factory(
    session: AsyncSession,
    factory: ContentFactory,
    payload: ContentFactoryUpdate,
) -> ContentFactory:
    updates = payload.model_dump(exclude_unset=True)
    if any(key in updates for key in {"content_config", "image_config", "publisher_account_id"}):
        updates["content_config"] = await normalize_content_config_for_telegram_limits(
            session,
            workspace_id=factory.workspace_id,
            publisher_account_id=updates.get("publisher_account_id", factory.publisher_account_id),
            content_config=dict(
                updates.get(
                    "content_config", factory.content_config if isinstance(factory.content_config, dict) else {}
                )
            ),
            image_config=dict(
                updates.get("image_config", factory.image_config if isinstance(factory.image_config, dict) else {})
            ),
        )
    if "content_config" in updates:
        updates["content_config"] = normalize_content_config(
            updates["content_config"],
            previous_config=factory.content_config if isinstance(factory.content_config, dict) else {},
            bump_revision=True,
        )
    if "schedule_config" in updates:
        updates["schedule_config"] = normalize_schedule_config(dict(updates["schedule_config"]))
    for key, value in updates.items():
        setattr(factory, key, value)
    if any(key in updates for key in {"schedule_config", "status"}):
        factory.next_run_at = (
            content_factory_next_run_at(
                factory.schedule_config if isinstance(factory.schedule_config, dict) else {}, datetime.now(UTC)
            )
            if factory.status == "active"
            else None
        )
    await add_event(
        session,
        factory.id,
        None,
        None,
        "info",
        "factory.updated",
        "Настройки контент-завода обновлены",
        {"fields": list(updates)},
    )
    await session.commit()
    await session.refresh(factory)
    return factory


async def pause_factory(session: AsyncSession, factory: ContentFactory) -> ContentFactory:
    factory.status = "paused"
    factory.next_run_at = None
    await add_event(session, factory.id, None, None, "info", "factory.paused", "Контент-завод поставлен на паузу", {})
    await session.commit()
    await session.refresh(factory)
    return factory


async def resume_factory(session: AsyncSession, factory: ContentFactory) -> ContentFactory:
    factory.status = "active"
    factory.next_run_at = content_factory_next_run_at(
        factory.schedule_config if isinstance(factory.schedule_config, dict) else {}, datetime.now(UTC)
    )
    await add_event(session, factory.id, None, None, "info", "factory.resumed", "Контент-завод возобновлен", {})
    await session.commit()
    await session.refresh(factory)
    return factory


async def add_source(
    session: AsyncSession,
    factory: ContentFactory,
    payload: ContentFactorySourceCreate,
    workspace_id: str | None = None,
) -> ContentFactorySource:
    source_id = payload.source_id
    source_ref = payload.source_ref
    settings = dict(payload.settings)
    if source_ref and not source_id and workspace_id:
        source = await find_or_create_telegram_source(session, workspace_id, source_ref)
        source_id = source.id
        settings.setdefault("collect_policy", "regular_new_posts")
        settings.setdefault("usage", "content_inputs")
        settings.setdefault("manual_ref", True)
    existing = await find_existing_factory_source_link(session, factory.id, source_id=source_id, source_ref=source_ref)
    if existing:
        if not existing.enabled and payload.enabled:
            existing.enabled = True
            await add_event(
                session,
                factory.id,
                None,
                None,
                "info",
                "source.enabled",
                "Источник уже был подключен и снова включен",
                {"source_id": source_id, "source_ref": source_ref},
            )
            await session.commit()
            await session.refresh(existing)
        return existing
    source = ContentFactorySource(
        factory_id=factory.id,
        source_id=source_id,
        source_ref=source_ref,
        source_type=payload.source_type,
        enabled=payload.enabled,
        weight=payload.weight,
        settings=settings,
    )
    session.add(source)
    await add_event(
        session,
        factory.id,
        None,
        None,
        "info",
        "source.added",
        "Источник подключен",
        {"source_id": payload.source_id, "source_ref": payload.source_ref},
    )
    await session.commit()
    await session.refresh(source)
    return source


async def find_existing_factory_source_link(
    session: AsyncSession,
    factory_id: str,
    *,
    source_id: str | None,
    source_ref: str | None,
) -> ContentFactorySource | None:
    rows = (
        (
            await session.execute(
                select(ContentFactorySource)
                .where(ContentFactorySource.factory_id == factory_id)
                .order_by(ContentFactorySource.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    target_key = content_factory_source_link_key(source_id=source_id, source_ref=source_ref)
    if not target_key:
        return None
    for row in rows:
        if content_factory_source_link_key(source_id=row.source_id, source_ref=row.source_ref) == target_key:
            return row
    return None


def dedupe_content_factory_source_links(rows: list[ContentFactorySource]) -> list[ContentFactorySource]:
    unique: list[ContentFactorySource] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: item.created_at):
        key = content_factory_source_link_key(source_id=row.source_id, source_ref=row.source_ref)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(row)
    return unique


def content_factory_source_link_key(*, source_id: str | None, source_ref: str | None) -> str:
    if source_id:
        return f"id:{source_id}"
    if source_ref:
        return f"ref:{normalize_source_ref_key(source_ref)}"
    return ""


def normalize_source_ref_key(ref: str) -> str:
    username, url, title = normalize_source_ref(ref)
    if username:
        return f"telegram:{username.lower()}"
    if url:
        return f"url:{url.rstrip('/').lower()}"
    return title.strip().lower()


async def add_knowledge(
    session: AsyncSession,
    factory: ContentFactory,
    payload: ContentFactoryKnowledgeCreate,
) -> ContentFactoryKnowledgeItem:
    item = _add_knowledge_item(session, factory.id, payload)
    await add_event(
        session,
        factory.id,
        None,
        None,
        "info",
        "knowledge.added",
        "Материал базы знаний добавлен",
        {"title": payload.title},
    )
    await session.commit()
    await session.refresh(item)
    return item


async def add_knowledge_files(
    session: AsyncSession,
    factory: ContentFactory,
    uploads: list[UploadFile],
) -> list[ContentFactoryKnowledgeItem]:
    if not uploads:
        raise ContentFactoryError("CONTENT_FACTORY_KNOWLEDGE_FILES_REQUIRED")
    if len(uploads) > MAX_KNOWLEDGE_UPLOAD_FILES:
        raise ContentFactoryError("CONTENT_FACTORY_TOO_MANY_KNOWLEDGE_FILES")

    items: list[ContentFactoryKnowledgeItem] = []
    for upload in uploads:
        filename = safe_upload_filename(upload.filename or "knowledge.txt")
        if is_ignored_knowledge_upload(filename):
            continue
        content = await upload.read()
        if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
            raise ContentFactoryError("CONTENT_FACTORY_KNOWLEDGE_FILE_TOO_LARGE")
        if not content and filename.startswith("._"):
            continue
        content_type = upload.content_type or guess_knowledge_content_type(filename)
        is_image = is_knowledge_image(filename, content_type)
        image_size = image_dimensions(content) if is_image else None
        text = (
            image_knowledge_description(filename, content_type, image_size)
            if is_image
            else extract_knowledge_text(filename, content)
        )
        path = knowledge_artifact_path(factory.id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        item = ContentFactoryKnowledgeItem(
            factory_id=factory.id,
            title=filename,
            kind="image" if is_image else "file",
            content_text=text[:120_000],
            file_path=str(path),
            file_data=content,
            item_metadata={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(content),
                "extracted_chars": len(text),
                **({"width": image_size[0], "height": image_size[1]} if image_size else {}),
            },
            enabled=True,
        )
        session.add(item)
        items.append(item)
    if not items:
        raise ContentFactoryError("CONTENT_FACTORY_KNOWLEDGE_FILES_REQUIRED")
    await add_event(
        session,
        factory.id,
        None,
        None,
        "info",
        "knowledge.files_added",
        "Файлы базы знаний добавлены",
        {"count": len(items), "filenames": [item.title for item in items]},
    )
    await session.commit()
    for item in items:
        await session.refresh(item)
    return items


async def collect_site_context(
    session: AsyncSession,
    factory: ContentFactory,
    *,
    site_url: str | None = None,
    max_pages: int = 8,
) -> ContentFactoryKnowledgeItem:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    normalized_url = normalize_site_context_url(site_url or str(config.get("product_site_url") or ""))
    pages = await fetch_site_context_pages(normalized_url, max_pages=max_pages)
    if not pages:
        raise ContentFactoryError("CONTENT_FACTORY_SITE_CONTEXT_EMPTY")

    extracted_text = format_site_context_pages(normalized_url, pages)
    brief = await build_site_context_brief(factory, normalized_url, extracted_text)
    now = datetime.now(UTC)
    metadata = {
        "source_url": normalized_url,
        "pages": [{"url": page.url, "title": page.title, "chars": len(page.text)} for page in pages],
        "page_count": len(pages),
        "collected_at": now.isoformat(),
        "collection_mode": "website_text_mvp",
    }

    result = await session.execute(
        select(ContentFactoryKnowledgeItem)
        .where(
            ContentFactoryKnowledgeItem.factory_id == factory.id,
            ContentFactoryKnowledgeItem.kind == "dynamic_site_context",
        )
        .limit(1)
    )
    item = result.scalar_one_or_none()
    if item:
        item.title = f"Контекст сайта: {site_context_title(normalized_url)}"
        item.content_text = brief[:120_000]
        item.item_metadata = metadata
        item.enabled = True
    else:
        item = ContentFactoryKnowledgeItem(
            factory_id=factory.id,
            title=f"Контекст сайта: {site_context_title(normalized_url)}",
            kind="dynamic_site_context",
            content_text=brief[:120_000],
            item_metadata=metadata,
            enabled=True,
        )
        session.add(item)

    factory.content_config = {
        **config,
        "product_site_url": normalized_url,
        "site_context_last_collected_at": now.isoformat(),
        "site_context_page_count": len(pages),
    }
    await add_event(
        session,
        factory.id,
        None,
        None,
        "info",
        "knowledge.site_context_collected",
        "Контекст сайта собран и добавлен в базу знаний",
        metadata,
    )
    await session.commit()
    await session.refresh(item)
    await session.refresh(factory)
    return item


async def run_factory(
    session: AsyncSession,
    factory: ContentFactory,
    *,
    trigger_type: str = "manual",
    topic_prompt: str | None = None,
) -> tuple[ContentFactoryRun, ContentPost | None]:
    active_result = await session.execute(
        select(ContentFactoryRun).where(
            ContentFactoryRun.factory_id == factory.id,
            ContentFactoryRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active_result.scalar_one_or_none():
        raise ContentFactoryError("CONTENT_FACTORY_RUN_ALREADY_ACTIVE")

    topic_prompt = normalize_manual_topic_prompt(topic_prompt)
    effective_trigger_type = "manual_topic" if trigger_type == "manual" and topic_prompt else trigger_type
    await validate_workspace_daily_generation_limit(session, factory)
    publisher_account = (
        await session.get(TelegramAccount, factory.publisher_account_id) if factory.publisher_account_id else None
    )
    premium_emoji_catalog = await content_factory_effective_premium_emoji_catalog(session, factory, publisher_account)
    now = datetime.now(UTC)
    run = ContentFactoryRun(
        factory_id=factory.id, status="researching", trigger_type=effective_trigger_type, started_at=now
    )
    session.add(run)
    await session.flush()
    await add_event(
        session,
        factory.id,
        run.id,
        None,
        "info",
        "run.started",
        "Запуск пайплайна начат",
        {"trigger_type": effective_trigger_type, **({"topic_prompt": topic_prompt} if topic_prompt else {})},
    )

    try:
        source_inputs = await collect_source_inputs(session, factory, run.id)
        knowledge_text = await collect_knowledge_text(session, factory)
        recent_memory = await collect_recent_post_memory(session, factory)
        slot_brief = await build_content_factory_slot_brief_for_run(
            session, factory, trigger_type=effective_trigger_type, now_utc=now
        )
        run.status = "writing"
        await session.flush()

        ai_payload = await generate_post_payload(
            factory,
            source_inputs,
            knowledge_text,
            slot_brief,
            topic_prompt=topic_prompt,
            premium_emoji_catalog=premium_emoji_catalog,
            recent_memory=recent_memory,
        )
        raw_text = enforce_content_factory_humanizer_output(
            normalize_content_factory_brand_text(factory, _clean_generated_post_text(str(ai_payload.get("text") or "")))
        )
        text = apply_content_factory_post_format(
            factory,
            raw_text,
            allow_truncate=False,
        )
        text = enforce_content_factory_humanizer_output(text)
        text = await fit_generated_post_to_telegram_limit(factory, text)
        if topic_prompt:
            text = ensure_manual_topic_heading(
                factory, text, str(ai_payload.get("title") or "Пост").strip(), topic_prompt
            )
            text = enforce_content_factory_humanizer_output(text)
            text = await fit_generated_post_to_telegram_limit(factory, text)
        text = enforce_content_factory_humanizer_output(normalize_content_factory_brand_text(factory, text))
        text = ensure_premium_emoji_placeholders_for_post(
            text,
            premium_emoji_catalog,
            footer=_content_factory_required_footer(factory)
            if _content_factory_custom_system_prompt(factory)
            else _content_factory_footer(factory),
        )
        text, premium_emoji_entities = apply_premium_emoji_placeholders(text, premium_emoji_catalog)
        if not text:
            raise ContentFactoryError("CONTENT_FACTORY_EMPTY_AI_OUTPUT")

        fingerprint = text_fingerprint(text)
        quality_score, quality_notes = quality_check(factory, text, duplicate=None)
        status = (
            "rejected" if quality_score < int(factory.quality_config.get("min_quality_score", 55)) else "needs_review"
        )
        if factory.mode == "drafts_only":
            status = "draft"

        post = ContentPost(
            factory_id=factory.id,
            run_id=run.id,
            status=status,
            title=normalize_content_factory_brand_text(factory, str(ai_payload.get("title") or "Черновик")).strip()[
                :220
            ],
            text=text,
            source_summary=source_inputs.get("summary"),
            source_refs=list(ai_payload.get("source_refs") or source_inputs.get("refs") or []),
            image_prompt=_image_prompt_for_factory(factory, ai_payload, text),
            quality_score=quality_score,
            quality_notes=[*quality_notes, *normalize_content_factory_quality_notes(ai_payload.get("quality_notes"))],
            post_metadata={
                "risk_flags": list(ai_payload.get("risk_flags") or []),
                "text_fingerprint": fingerprint,
                "style_signature": editorial_style_signature(text),
                "ai_usage": ai_payload.get("_usage"),
                "config_revision": (factory.content_config or {}).get("config_revision")
                if isinstance(factory.content_config, dict)
                else None,
                "planned_slot": {
                    "slot_index": slot_brief.slot_index,
                    "slot_count": slot_brief.slot_count,
                    "slot_local_time": slot_brief.slot_local_time,
                    "weekday": slot_brief.weekday_label,
                    "day_theme": slot_brief.day_theme,
                    "subtopic": slot_brief.subtopic,
                    "angle": slot_brief.angle,
                },
                **({"manual_topic_prompt": topic_prompt} if topic_prompt else {}),
                **(
                    {
                        "custom_emoji_entities": premium_emoji_entities,
                        "custom_emoji_text": text,
                    }
                    if premium_emoji_entities
                    else {}
                ),
            },
        )
        session.add(post)
        await session.flush()
        image_required = _image_enabled(factory)
        if image_required:
            await generate_image_for_post(session, factory, post)
            if not _post_image_ready(post):
                post.status = "needs_review" if post.status != "rejected" else post.status
                post.quality_notes = [
                    *post.quality_notes,
                    "Изображения включены, но файл не готов. Автопубликация остановлена до регенерации изображения.",
                ]
                post.post_metadata = {
                    **post.post_metadata,
                    "image_required": True,
                    "publish_blocked_reason": "image_missing",
                }
                await add_event(
                    session,
                    factory.id,
                    run.id,
                    post.id,
                    "warning",
                    "post.auto_publish_blocked",
                    "Автопубликация остановлена: нет готового изображения",
                    {"reason": "image_missing"},
                )
        session.add(
            ContentMemoryItem(
                factory_id=factory.id,
                post_id=post.id,
                topic=post.title,
                angle=str(ai_payload.get("angle") or "")[:220] or None,
                rubric=str(ai_payload.get("rubric") or "")[:120] or None,
                key_claims=list(ai_payload.get("key_claims") or []),
                cta=_optional_str(ai_payload.get("cta")),
                source_refs=post.source_refs,
                text_fingerprint=fingerprint,
                performance_snapshot={"style_signature": editorial_style_signature(text)},
                created_at=now,
            )
        )
        run.status = "ready"
        run.completed_at = datetime.now(UTC)
        run.metrics = {
            "source_inputs": len(source_inputs.get("items", [])),
            "quality_score": quality_score,
            "post_id": post.id,
            "duplicate": False,
            **({"topic_prompt": topic_prompt} if topic_prompt else {}),
        }
        await add_event(session, factory.id, run.id, post.id, "info", "post.generated", "Черновик создан", run.metrics)

        can_auto_publish_with_image = not image_required or _post_image_ready(post)
        is_auto_publish_mode = factory.mode in {"auto_publish", "auto_publish_safe", "auto_publish_full"}
        auto_publish_ready, auto_publish_block_reason = (
            validate_auto_publish_ready(factory, post) if is_auto_publish_mode else (True, "")
        )
        if is_auto_publish_mode and not auto_publish_ready:
            post.status = "needs_review" if post.status != "rejected" else post.status
            post.quality_notes = [*post.quality_notes, auto_publish_block_reason]
            post.post_metadata = {
                **post.post_metadata,
                "publish_blocked_reason": post.post_metadata.get("publish_blocked_reason") or "auto_publish_preflight",
            }
            await add_event(
                session,
                factory.id,
                run.id,
                post.id,
                "warning",
                "post.auto_publish_blocked",
                auto_publish_block_reason,
                {"reason": "auto_publish_preflight"},
            )
        if is_auto_publish_mode and status != "rejected" and can_auto_publish_with_image and auto_publish_ready:
            await publish_post(session, factory, post)

        await session.commit()
        await session.refresh(run)
        await session.refresh(post)
        return run, post
    except (AiProviderError, ContentFactoryError, ValueError) as exc:
        run.status = "failed"
        run.error = str(exc)
        run.completed_at = datetime.now(UTC)
        await add_event(
            session, factory.id, run.id, None, "error", "run.failed", "Запуск завершился ошибкой", {"error": str(exc)}
        )
        await session.commit()
        await session.refresh(run)
        return run, None


async def tick_due_content_factories(
    session: AsyncSession,
    *,
    workspace_id: str | None = None,
    limit: int = 20,
) -> list[ContentFactoryRun]:
    now = datetime.now(UTC)
    query = (
        select(ContentFactory)
        .where(
            ContentFactory.status == "active",
            or_(ContentFactory.next_run_at.is_(None), ContentFactory.next_run_at <= now),
        )
        .order_by(nullsfirst(ContentFactory.next_run_at.asc()), ContentFactory.updated_at.asc())
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )
    if workspace_id:
        query = query.where(ContentFactory.workspace_id == workspace_id)
    factories = list((await session.execute(query)).scalars().all())
    started_runs: list[ContentFactoryRun] = []
    for factory in factories:
        if factory.next_run_at is None:
            factory.next_run_at = content_factory_next_run_at(
                factory.schedule_config if isinstance(factory.schedule_config, dict) else {}, now
            )
        next_run_at = ensure_aware_utc(factory.next_run_at)
        if next_run_at and next_run_at > now:
            await session.commit()
            continue
        active_run_id = await session.scalar(
            select(ContentFactoryRun.id)
            .where(ContentFactoryRun.factory_id == factory.id, ContentFactoryRun.status.in_(ACTIVE_RUN_STATUSES))
            .limit(1)
        )
        if active_run_id:
            factory.next_run_at = now + timedelta(minutes=5)
            await session.commit()
            continue
        factory.next_run_at = content_factory_next_run_at(
            factory.schedule_config if isinstance(factory.schedule_config, dict) else {}, now
        )
        await session.commit()
        run, _post = await run_factory(session, factory, trigger_type="schedule")
        started_runs.append(run)
    return started_runs


def content_factory_next_run_at(config: dict, after_utc: datetime) -> datetime:
    after_utc = ensure_aware_utc(after_utc) or datetime.now(UTC)
    timezone = content_factory_timezone(str(config.get("timezone") or "Europe/Moscow"))
    after_local = after_utc.astimezone(timezone)
    publish_days = content_factory_publish_days(config)
    slots = content_factory_schedule_slot_minutes(config)
    for day_offset in range(0, 15):
        candidate_day = after_local + timedelta(days=day_offset)
        if CONTENT_FACTORY_WEEKDAY_VALUES[candidate_day.weekday()] not in publish_days:
            continue
        day_start = candidate_day.replace(hour=0, minute=0, second=0, microsecond=0)
        for slot in slots:
            candidate = day_start + timedelta(minutes=slot)
            if candidate > after_local:
                return candidate.astimezone(UTC)
    return (after_utc + timedelta(days=1)).astimezone(UTC)


def ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def regenerate_text(session: AsyncSession, factory: ContentFactory, post: ContentPost) -> ContentPost:
    if post.status == "published":
        raise ContentFactoryError("CONTENT_POST_FINAL_STATUS")

    active_result = await session.execute(
        select(ContentFactoryRun).where(
            ContentFactoryRun.factory_id == factory.id,
            ContentFactoryRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active_result.scalar_one_or_none():
        raise ContentFactoryError("CONTENT_FACTORY_RUN_ALREADY_ACTIVE")

    await validate_workspace_daily_generation_limit(session, factory)
    publisher_account = (
        await session.get(TelegramAccount, factory.publisher_account_id) if factory.publisher_account_id else None
    )
    premium_emoji_catalog = await content_factory_effective_premium_emoji_catalog(session, factory, publisher_account)
    now = datetime.now(UTC)
    run = ContentFactoryRun(factory_id=factory.id, status="researching", trigger_type="retry", started_at=now)
    session.add(run)
    await session.flush()
    await add_event(
        session,
        factory.id,
        run.id,
        post.id,
        "info",
        "post.regenerate_text_started",
        "Перегенерация текста начата",
        {"post_id": post.id},
    )

    try:
        source_inputs = await collect_source_inputs(session, factory, run.id)
        knowledge_text = await collect_knowledge_text(session, factory)
        recent_memory = await collect_recent_post_memory(session, factory)
        slot_brief = await build_content_factory_slot_brief_for_run(session, factory, trigger_type="retry", now_utc=now)
        run.status = "writing"
        await session.flush()

        ai_payload = await generate_post_payload(
            factory,
            source_inputs,
            knowledge_text,
            slot_brief,
            premium_emoji_catalog=premium_emoji_catalog,
            recent_memory=recent_memory,
        )
        raw_text = enforce_content_factory_humanizer_output(
            normalize_content_factory_brand_text(factory, _clean_generated_post_text(str(ai_payload.get("text") or "")))
        )
        text = apply_content_factory_post_format(factory, raw_text, allow_truncate=False)
        text = enforce_content_factory_humanizer_output(text)
        text = await fit_generated_post_to_telegram_limit(factory, text)
        text = enforce_content_factory_humanizer_output(normalize_content_factory_brand_text(factory, text))
        text = ensure_premium_emoji_placeholders_for_post(
            text,
            premium_emoji_catalog,
            footer=_content_factory_required_footer(factory)
            if _content_factory_custom_system_prompt(factory)
            else _content_factory_footer(factory),
        )
        text, premium_emoji_entities = apply_premium_emoji_placeholders(text, premium_emoji_catalog)
        if not text:
            raise ContentFactoryError("CONTENT_FACTORY_EMPTY_AI_OUTPUT")

        fingerprint = text_fingerprint(text)
        quality_score, quality_notes = quality_check(factory, text, duplicate=None)
        next_status = (
            "rejected" if quality_score < int(factory.quality_config.get("min_quality_score", 55)) else "needs_review"
        )
        if factory.mode == "drafts_only":
            next_status = "draft"
        previous_text = post.text
        post.run_id = run.id
        post.status = next_status
        post.title = normalize_content_factory_brand_text(
            factory, str(ai_payload.get("title") or post.title or "Черновик")
        ).strip()[:220]
        post.text = text
        post.source_summary = source_inputs.get("summary")
        post.source_refs = list(ai_payload.get("source_refs") or source_inputs.get("refs") or [])
        post.quality_score = quality_score
        post.quality_notes = [*quality_notes, *normalize_content_factory_quality_notes(ai_payload.get("quality_notes"))]
        post.telegram_message_id = None
        post.telegram_link = None
        post.published_at = None
        post.post_metadata = {
            **post.post_metadata,
            "previous_ai_text": previous_text,
            "text_fingerprint": fingerprint,
            "style_signature": editorial_style_signature(text),
            "ai_usage": ai_payload.get("_usage"),
            "config_revision": (factory.content_config or {}).get("config_revision")
            if isinstance(factory.content_config, dict)
            else None,
            "last_publish_problem": None,
            "publish_blocked_reason": None,
            **(
                {
                    "custom_emoji_entities": premium_emoji_entities,
                    "custom_emoji_text": text,
                }
                if premium_emoji_entities
                else {"custom_emoji_entities": [], "custom_emoji_text": text}
            ),
        }
        session.add(
            ContentMemoryItem(
                factory_id=factory.id,
                post_id=post.id,
                topic=post.title,
                angle=str(ai_payload.get("angle") or "")[:220] or None,
                rubric=str(ai_payload.get("rubric") or "")[:120] or None,
                key_claims=list(ai_payload.get("key_claims") or []),
                cta=_optional_str(ai_payload.get("cta")),
                source_refs=post.source_refs,
                text_fingerprint=fingerprint,
                performance_snapshot={"style_signature": editorial_style_signature(text), "regenerated": True},
                created_at=now,
            )
        )
        run.status = "ready"
        run.completed_at = datetime.now(UTC)
        run.metrics = {
            "source_inputs": len(source_inputs.get("items", [])),
            "quality_score": quality_score,
            "post_id": post.id,
            "regenerated_post_id": post.id,
        }
        await add_event(
            session,
            factory.id,
            run.id,
            post.id,
            "info",
            "post.regenerated_text",
            "Текст поста перегенерирован",
            run.metrics,
        )
        await session.commit()
        await session.refresh(post)
        return post
    except (AiProviderError, ContentFactoryError, ValueError) as exc:
        run.status = "failed"
        run.error = str(exc)
        run.completed_at = datetime.now(UTC)
        await add_event(
            session,
            factory.id,
            run.id,
            post.id,
            "error",
            "post.regenerate_text_failed",
            "Перегенерация текста завершилась ошибкой",
            {"error": str(exc)},
        )
        await session.commit()
        raise ContentFactoryError(str(exc)) from exc


async def regenerate_image(session: AsyncSession, factory: ContentFactory, post: ContentPost) -> ContentPost:
    if post.status == "published":
        raise ContentFactoryError("CONTENT_POST_FINAL_STATUS")
    prompt = _image_prompt_for_factory(factory, {"title": post.title, "image_prompt": post.image_prompt}, post.text)
    post.image_prompt = prompt
    await generate_image_for_post(session, factory, post)
    await session.commit()
    await session.refresh(post)
    return post


async def generate_image_for_post(session: AsyncSession, factory: ContentFactory, post: ContentPost) -> None:
    prompt = post.image_prompt or _image_prompt_fallback_for_factory(factory, {"title": post.title}, post.text)
    knowledge_only = _image_knowledge_only(factory)
    knowledge_image = await select_knowledge_image_for_post(
        session,
        factory,
        prompt,
        allow_fallback=knowledge_only,
        allow_recent_requested=knowledge_only,
    )
    if knowledge_image:
        payload = knowledge_file_payload(knowledge_image)
        if payload:
            artifact_path = _image_artifact_path(factory.id, post.id).with_suffix(payload.suffix)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(payload.data)
            post.image_prompt = prompt
            post.image_attachment_id = str(artifact_path)
            post.image_data = payload.data
            post.image_content_type = payload.content_type
            post.post_metadata = {
                **post.post_metadata,
                "image_status": "ready",
                "image_error": None,
                "image_retry_error": None,
                "image_retry_attempt": None,
                "image_retry_delay_seconds": None,
                "image_refreshed_at": datetime.now(UTC).isoformat(),
                "image_source": "knowledge_image",
                "knowledge_image_id": knowledge_image.id,
                "knowledge_image_title": knowledge_image.title,
            }
            await add_event(
                session,
                factory.id,
                post.run_id,
                post.id,
                "info",
                "image.knowledge_attached",
                "Скриншот из базы знаний прикреплён к посту",
                {"knowledge_image_id": knowledge_image.id, "title": knowledge_image.title},
            )
            return

    if knowledge_only:
        post.quality_notes = [
            *post.quality_notes,
            "Изображения настроены только из базы знаний, но подходящий файл не найден.",
        ]
        post.post_metadata = {
            **post.post_metadata,
            "image_status": "failed",
            "image_source": "knowledge_only",
            "image_error": "CONTENT_FACTORY_KNOWLEDGE_IMAGE_NOT_FOUND",
        }
        await add_event(
            session,
            factory.id,
            post.run_id,
            post.id,
            "warning",
            "image.knowledge_missing",
            "Изображение из базы знаний не найдено",
            {"prompt": prompt[:500]},
        )
        return

    fallback_prompt = _image_prompt_fallback_for_factory(factory, {"title": post.title}, post.text)
    prompts = [prompt]
    if fallback_prompt != prompt:
        prompts.append(fallback_prompt)
    image_bytes = b""
    usage: dict[str, Any] = {}
    last_error: AiProviderError | None = None
    used_prompt = prompt
    total_wait_seconds = 0.0
    for candidate in prompts:
        for attempt in range(1, IMAGE_RETRY_MAX_ATTEMPTS + 1):
            try:
                image_bytes, usage = await generate_profile_avatar_image(candidate)
                used_prompt = candidate
                break
            except AiProviderError as exc:
                last_error = exc
                retry_delay = image_retry_delay_seconds(str(exc), attempt)
                if retry_delay is None or attempt >= IMAGE_RETRY_MAX_ATTEMPTS:
                    break
                retry_delay = min(retry_delay, max(0.0, IMAGE_RETRY_MAX_TOTAL_WAIT_SECONDS - total_wait_seconds))
                if retry_delay <= 0:
                    break
                post.post_metadata = {
                    **post.post_metadata,
                    "image_status": "retrying",
                    "image_retry_attempt": attempt,
                    "image_retry_delay_seconds": retry_delay,
                    "image_retry_error": str(exc)[:500],
                }
                await add_event(
                    session,
                    factory.id,
                    post.run_id,
                    post.id,
                    "info",
                    "image.retry_wait",
                    "Ждём лимит провайдера изображений",
                    {"attempt": attempt, "delay_seconds": retry_delay, "error": str(exc)[:500]},
                )
                await session.flush()
                await asyncio.sleep(retry_delay)
                total_wait_seconds += retry_delay
        if image_bytes:
            break
        if last_error and is_retryable_image_error(str(last_error)):
            break
    if not image_bytes:
        post.quality_notes = [*post.quality_notes, f"Image generation failed: {last_error}"]
        post.post_metadata = {**post.post_metadata, "image_status": "failed", "image_error": str(last_error)}
        await add_event(
            session,
            factory.id,
            post.run_id,
            post.id,
            "warning",
            "image.failed",
            "Изображение не сгенерировано",
            {"error": str(last_error)},
        )
        return

    path = _image_artifact_path(factory.id, post.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)
    post.image_prompt = used_prompt
    post.image_attachment_id = str(path)
    post.image_data = image_bytes
    post.image_content_type = "image/jpeg"
    post.post_metadata = {
        **post.post_metadata,
        "image_status": "ready",
        "image_error": None,
        "image_retry_error": None,
        "image_retry_attempt": None,
        "image_retry_delay_seconds": None,
        "image_refreshed_at": datetime.now(UTC).isoformat(),
        "image_usage": usage,
    }
    await add_event(
        session,
        factory.id,
        post.run_id,
        post.id,
        "info",
        "image.generated",
        "Изображение сгенерировано",
        {"path": str(path)},
    )


async def select_knowledge_image_for_post(
    session: AsyncSession,
    factory: ContentFactory,
    image_prompt: str,
    *,
    allow_fallback: bool = False,
    allow_recent_requested: bool = True,
) -> ContentFactoryKnowledgeItem | None:
    requested = requested_knowledge_image_name(image_prompt)
    result = await session.execute(
        select(ContentFactoryKnowledgeItem)
        .where(
            ContentFactoryKnowledgeItem.factory_id == factory.id,
            ContentFactoryKnowledgeItem.enabled.is_(True),
            ContentFactoryKnowledgeItem.kind == "image",
        )
        .order_by(ContentFactoryKnowledgeItem.created_at.desc())
        .limit(50)
    )
    all_images = [
        item
        for item in result.scalars().all()
        if (item.file_path or item.file_data) and not is_sensitive_knowledge_image(item)
    ]
    if not all_images:
        return None
    if requested:
        requested_lower = requested.lower()
        if is_sensitive_knowledge_image_name(requested_lower):
            return None
        recent_image_titles = (
            set() if allow_recent_requested else await collect_recent_knowledge_image_titles(session, factory, limit=5)
        )
        if requested_lower in recent_image_titles:
            return None
        for item in all_images:
            filename = str((item.item_metadata or {}).get("filename") or item.title).lower()
            if requested_lower in {filename, item.title.lower()} or requested_lower in filename:
                if not allow_recent_requested and (
                    filename in recent_image_titles or item.title.lower() in recent_image_titles
                ):
                    return None
                return item
        tokens = set(knowledge_image_tokens(requested_lower))
        if tokens:
            ranked = sorted(
                all_images,
                key=lambda item: len(tokens & set(knowledge_image_tokens(item.title))),
                reverse=True,
            )
            if ranked and len(tokens & set(knowledge_image_tokens(ranked[0].title))) > 0:
                return ranked[0]
        return all_images[0] if allow_fallback else None

    if not allow_fallback:
        return None

    recent_image_titles = await collect_recent_knowledge_image_titles(session, factory, limit=5)
    images = [
        item
        for item in all_images
        if str((item.item_metadata or {}).get("filename") or item.title).lower() not in recent_image_titles
        and item.title.lower() not in recent_image_titles
    ]
    if not images:
        return None
    prompt_tokens = set(knowledge_image_tokens(image_prompt))
    if prompt_tokens:
        ranked = sorted(
            images,
            key=lambda item: len(prompt_tokens & set(knowledge_image_tokens(item.title))),
            reverse=True,
        )
        if ranked and len(prompt_tokens & set(knowledge_image_tokens(ranked[0].title))) > 0:
            return ranked[0]
    return images[0]


async def collect_recent_knowledge_image_titles(
    session: AsyncSession, factory: ContentFactory, limit: int = 5
) -> set[str]:
    result = await session.execute(
        select(ContentPost)
        .where(ContentPost.factory_id == factory.id)
        .order_by(ContentPost.created_at.desc())
        .limit(max(limit, 1) * 3)
    )
    titles: set[str] = set()
    for post in result.scalars().all():
        metadata = post.post_metadata if isinstance(post.post_metadata, dict) else {}
        title = str(metadata.get("knowledge_image_title") or "").strip().lower()
        if title:
            titles.add(title)
    return titles


def is_sensitive_knowledge_image(item: ContentFactoryKnowledgeItem) -> bool:
    metadata = item.item_metadata if isinstance(item.item_metadata, dict) else {}
    name = str(metadata.get("filename") or item.title or "")
    text = f"{name} {item.title or ''} {item.content_text or ''}"
    return is_sensitive_knowledge_image_name(text)


def is_sensitive_knowledge_image_name(value: str) -> bool:
    normalized = re.sub(r"[^a-zа-яё0-9]+", "-", value.lower())
    return any(token in normalized for token in SENSITIVE_KNOWLEDGE_IMAGE_TOKENS)


def requested_knowledge_image_name(image_prompt: str) -> str | None:
    marker = "USE_KNOWLEDGE_IMAGE:"
    if marker not in image_prompt:
        return None
    request = image_prompt.split(marker, 1)[1].strip()
    request = request.split("|", 1)[0].strip()
    request = request.splitlines()[0].strip()
    return Path(request).name if request else None


def knowledge_image_tokens(value: str) -> list[str]:
    normalized = Path(value).stem.lower().replace("_", "-")
    return [token for token in normalized.split("-") if len(token) >= 3]


def image_retry_delay_seconds(error: str, attempt: int) -> float | None:
    if not is_retryable_image_error(error):
        return None
    provider_delay = parse_provider_retry_delay_seconds(error)
    rate_limit_floor = rate_limit_floor_seconds(error)
    if rate_limit_floor is not None:
        provider_delay = max(provider_delay or 0, rate_limit_floor)
    if provider_delay is None:
        provider_delay = min(IMAGE_RETRY_MAX_DELAY_SECONDS, IMAGE_RETRY_MIN_DELAY_SECONDS * (2 ** max(0, attempt - 1)))
    delay = max(IMAGE_RETRY_MIN_DELAY_SECONDS, min(provider_delay, IMAGE_RETRY_MAX_DELAY_SECONDS))
    return round(delay + random.uniform(0, min(0.35, delay * 0.15)), 3)


def is_retryable_image_error(error: str) -> bool:
    lowered = error.lower()
    return (
        "rate limit" in lowered
        or "too many requests" in lowered
        or " 429 " in lowered
        or "http_error: 429" in lowered
        or "http_error: 500" in lowered
        and "rate limit" in lowered
    )


def parse_provider_retry_delay_seconds(error: str) -> float | None:
    match = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?)", error, flags=re.IGNORECASE)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        return value / 1000 if unit.startswith("m") else value
    match = re.search(r"retry-after[\"':=\s]+(\d+(?:\.\d+)?)", error, flags=re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def rate_limit_floor_seconds(error: str) -> float | None:
    lowered = error.lower()
    if "rate limit" not in lowered and "too many requests" not in lowered:
        return None
    if "per min" in lowered or "per minute" in lowered:
        return IMAGE_RATE_LIMIT_PER_MINUTE_DELAY_SECONDS
    return None


async def approve_post(session: AsyncSession, factory: ContentFactory, post: ContentPost) -> ContentPost:
    if post.status == "published":
        raise ContentFactoryError("CONTENT_POST_FINAL_STATUS")
    previous_status = post.status
    post.status = "approved"
    await add_event(
        session,
        factory.id,
        post.run_id,
        post.id,
        "info",
        "post.approved",
        "Черновик одобрен",
        {"previous_status": previous_status},
    )
    await session.commit()
    await session.refresh(post)
    return post


async def update_post(
    session: AsyncSession, factory: ContentFactory, post: ContentPost, payload: ContentPostUpdate
) -> ContentPost:
    updates = payload.model_dump(exclude_unset=True)
    if "text" in updates and updates["text"]:
        previous_text = post.text
        post.text = updates["text"]
        post.post_metadata = {
            **post.post_metadata,
            "previous_ai_text": previous_text,
            "text_fingerprint": text_fingerprint(post.text),
            "edited_by_user": True,
        }
    if "title" in updates:
        post.title = updates["title"]
    if "scheduled_at" in updates:
        post.scheduled_at = updates["scheduled_at"]
    await add_event(
        session,
        factory.id,
        post.run_id,
        post.id,
        "info",
        "post.updated",
        "Черновик обновлен",
        {"fields": list(updates)},
    )
    await session.commit()
    await session.refresh(post)
    return post


async def publish_post(session: AsyncSession, factory: ContentFactory, post: ContentPost) -> ContentPost:
    if post.status == "published":
        return post
    if post.status not in {"approved", "needs_review", "draft", "scheduled"}:
        raise ContentFactoryError("CONTENT_POST_NOT_PUBLISHABLE")
    if _image_enabled(factory) and not _post_image_ready(post):
        publish_problem = content_factory_publish_problem(
            "image_missing",
            "Изображение включено, но файл ещё не готов. Пересоздайте изображение и дождитесь завершения.",
        )
        post.post_metadata = {
            **post.post_metadata,
            "image_required": True,
            "publish_blocked_reason": "image_missing",
            "last_publish_problem": publish_problem,
        }
        await add_event(
            session,
            factory.id,
            post.run_id,
            post.id,
            "warning",
            "post.publish_blocked",
            "Публикация остановлена: нет готового изображения",
            {"reason": "image_missing"},
        )
        await session.commit()
        raise ContentFactoryError("CONTENT_POST_IMAGE_REQUIRED")
    await validate_publish_preflight(session, factory)
    now = datetime.now(UTC)
    target = (factory.target_ref or "local-draft").strip()
    if real_telegram_enabled():
        account = await session.get(TelegramAccount, factory.publisher_account_id)
        if not account:
            raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_ACCOUNT_UNAVAILABLE")
        image_path, temp_image_path = _post_image_path_for_publish(post)
        if image_path:
            post.text = apply_content_factory_post_format(factory, post.text, allow_truncate=False)
            caption_limit = _content_factory_max_length(factory)
            if len(post.text) > caption_limit:
                publish_problem = content_factory_publish_problem(
                    "caption_too_long",
                    "Подпись к изображению длиннее лимита Telegram. Сократите текст и сохраните пост перед публикацией.",
                    details={"caption_length": len(post.text), "caption_limit": caption_limit},
                )
                post.post_metadata = {
                    **post.post_metadata,
                    "caption_length": len(post.text),
                    "caption_limit": caption_limit,
                    "publish_blocked_reason": "caption_too_long",
                    "last_publish_problem": publish_problem,
                }
                await add_event(
                    session,
                    factory.id,
                    post.run_id,
                    post.id,
                    "warning",
                    "post.publish_blocked",
                    "Публикация остановлена: подпись к изображению длиннее лимита Telegram",
                    {"reason": "caption_too_long", "caption_length": len(post.text), "caption_limit": caption_limit},
                )
                await session.commit()
                raise ContentFactoryError("CONTENT_POST_CAPTION_TOO_LONG")
        post.text, custom_emoji_entities = await prepare_post_custom_emoji_for_publish(session, factory, account, post)
        if image_path:
            caption_limit = _content_factory_max_length(factory)
            if len(post.text) > caption_limit:
                publish_problem = content_factory_publish_problem(
                    "caption_too_long",
                    "Подпись к изображению длиннее лимита Telegram после подготовки форматирования. Сократите текст и сохраните пост перед публикацией.",
                    details={"caption_length": len(post.text), "caption_limit": caption_limit},
                )
                post.post_metadata = {
                    **post.post_metadata,
                    "caption_length": len(post.text),
                    "caption_limit": caption_limit,
                    "publish_blocked_reason": "caption_too_long",
                    "last_publish_problem": publish_problem,
                }
                await add_event(
                    session,
                    factory.id,
                    post.run_id,
                    post.id,
                    "warning",
                    "post.publish_blocked",
                    "Публикация остановлена: подпись к изображению длиннее лимита Telegram",
                    {"reason": "caption_too_long", "caption_length": len(post.text), "caption_limit": caption_limit},
                )
                await session.commit()
                raise ContentFactoryError("CONTENT_POST_CAPTION_TOO_LONG")
        post.post_metadata = {
            **post.post_metadata,
            **(
                {"caption_length": len(post.text), "caption_limit": _content_factory_max_length(factory)}
                if image_path
                else {}
            ),
            **(
                {"custom_emoji_entities": custom_emoji_entities, "custom_emoji_text": post.text}
                if custom_emoji_entities
                else {}
            ),
        }
        await session.flush()
        try:
            send_result = await send_channel_post_with_retries(
                session,
                account,
                target,
                post.text,
                image_path=image_path,
                formatting_entities=custom_emoji_entities,
                business_intent=f"content_post:{post.id}",
            )
        except TelegramGatewayError as exc:
            publish_problem = content_factory_publish_problem(
                exc.code,
                telegram_publish_error_message(exc),
                details=exc.raw,
            )
            post.post_metadata = {
                **post.post_metadata,
                "publish_blocked_reason": "telegram_gateway",
                "last_publish_problem": publish_problem,
            }
            await add_event(
                session,
                factory.id,
                post.run_id,
                post.id,
                "error",
                "post.publish_failed",
                "Telegram не опубликовал пост",
                {"error_code": exc.code, "message": str(exc), "details": exc.raw},
            )
            await session.commit()
            raise ContentFactoryError(f"TELEGRAM_PUBLISH_FAILED:{exc.code}:{str(exc)}") from exc
        finally:
            if temp_image_path:
                temp_image_path.unlink(missing_ok=True)
        post.telegram_message_id = send_result.tg_message_id
        target_slug = target.lstrip("@")
        post.telegram_link = f"https://t.me/{target_slug}/{send_result.tg_message_id}" if target_slug else None
        event_type = "post.published.telegram"
        event_message = "Пост опубликован в Telegram"
        event_payload = {"telegram_message_id": post.telegram_message_id, "raw": send_result.raw}
    else:
        post.telegram_message_id = f"scaffold-{post.id}"
        target_slug = target.lstrip("@") or "local-draft"
        post.telegram_link = f"https://t.me/{target_slug}" if target_slug else None
        event_type = "post.published.scaffold"
        event_message = "Пост помечен опубликованным в scaffold-режиме"
        event_payload = {"telegram_message_id": post.telegram_message_id}

    post.status = "published"
    post.published_at = now
    post.post_metadata = {**post.post_metadata, "last_publish_problem": None, "publish_blocked_reason": None}
    await add_event(session, factory.id, post.run_id, post.id, "info", event_type, event_message, event_payload)
    await session.commit()
    await session.refresh(post)
    return post


async def send_channel_post_with_retries(
    session: AsyncSession,
    account: TelegramAccount,
    target: str,
    text: str,
    *,
    image_path: str | None,
    formatting_entities: list[dict[str, Any]] | None,
    business_intent: str | None = None,
) -> Any:
    return await retry_telegram_connection(
        lambda: send_channel_post_with_account(
            session,
            account,
            target,
            text,
            image_path=image_path,
            formatting_entities=formatting_entities,
            business_intent=business_intent,
        )
    )


async def fetch_posts_with_retries(
    session: AsyncSession,
    account: TelegramAccount,
    source_ref: str,
    limit: int,
) -> Any:
    return await retry_telegram_connection(lambda: fetch_posts_with_account(session, account, source_ref, limit))


async def retry_telegram_connection(operation: Any) -> Any:
    # Keep the legacy entry point, but never repeat an uncertain Telegram operation.
    return await operation()


def content_factory_publish_problem(code: str, message: str, *, details: dict | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message[:600],
        "details": details or {},
        "at": datetime.now(UTC).isoformat(),
    }


def telegram_publish_error_message(exc: TelegramGatewayError) -> str:
    raw = exc.raw if isinstance(exc.raw, dict) else {}
    raw_text = " ".join(str(value) for value in raw.values()).lower()
    message = str(exc)
    if exc.code == "TELEGRAM_CONNECTION_FAILED":
        if "authentication failed" in raw_text or "authentication failure" in raw_text or "auth" in message.lower():
            return "Telegram не принял подключение через прокси: ошибка авторизации прокси. Проверьте прокси аккаунта-публикатора или выберите другой рабочий прокси."
        return "Не удалось подключиться к Telegram через аккаунт-публикатор. Проверьте сессию аккаунта, прокси и доступность Telegram."
    if exc.code == "FLOOD_WAIT":
        seconds = raw.get("seconds")
        return (
            f"Telegram попросил подождать{f' {seconds} сек.' if seconds else ''}. Пост не отправлен, попробуйте позже."
        )
    if exc.code == "SESSION_UNAUTHORIZED":
        return "Сессия аккаунта-публикатора не авторизована или истекла. Переподключите аккаунт."
    if exc.code == "SESSION_2FA_REQUIRED":
        return "Аккаунт-публикатор требует повторный вход с 2FA."
    return f"Telegram не опубликовал пост: {message}"


def validate_auto_publish_ready(factory: ContentFactory, post: ContentPost) -> tuple[bool, str]:
    text = _clean_generated_post_text(post.text)
    if not text:
        return False, "Автопубликация остановлена: текст поста пустой."
    if len(text) > _content_factory_max_length(factory):
        return False, "Автопубликация остановлена: текст длиннее лимита Telegram."
    if text.endswith(("…", "...")):
        return False, "Автопубликация остановлена: текст выглядит обрезанным."
    if _image_enabled(factory) and not _post_image_ready(post):
        return False, "Автопубликация остановлена: изображение включено, но файл не готов."
    required_footer = _content_factory_required_footer(factory)
    if required_footer and not _text_contains_footer(text, required_footer):
        return False, "Автопубликация остановлена: в посте нет footer/CTA, заданного в системном промпте."
    if _auto_publish_requires_footer(factory) and not _has_footer_like_link(text):
        return False, "Автопубликация остановлена: в посте нет обязательного footer/CTA со ссылкой."
    return True, ""


def _auto_publish_requires_footer(factory: ContentFactory) -> bool:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    if "require_footer" in config:
        return bool(config.get("require_footer"))
    custom_prompt = _content_factory_custom_system_prompt(factory).lower()
    if any(marker in custom_prompt for marker in ("footer", "футер", "cta", "ссылк")):
        return True
    return bool(_content_factory_primary_link(factory))


def _post_image_ready(post: ContentPost) -> bool:
    return bool((post.image_attachment_id and Path(post.image_attachment_id).exists()) or post.image_data)


def _post_image_path_for_publish(post: ContentPost) -> tuple[str | None, Path | None]:
    if post.image_attachment_id and Path(post.image_attachment_id).exists():
        source_path = Path(post.image_attachment_id)
        if source_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return str(source_path), None
        suffix = image_suffix_for_content_type(post.image_content_type)
        with tempfile.NamedTemporaryFile(prefix=f"content-post-{post.id}-", suffix=suffix, delete=False) as handle:
            handle.write(source_path.read_bytes())
            temp_path = Path(handle.name)
        return str(temp_path), temp_path
    if not post.image_data:
        return None, None
    suffix = image_suffix_for_content_type(post.image_content_type)
    with tempfile.NamedTemporaryFile(prefix=f"content-post-{post.id}-", suffix=suffix, delete=False) as handle:
        handle.write(post.image_data)
        path = Path(handle.name)
    return str(path), path


def image_suffix_for_content_type(content_type: str | None) -> str:
    normalized = (content_type or "image/jpeg").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized, ".jpg")


async def validate_publish_preflight(session: AsyncSession, factory: ContentFactory) -> None:
    if not factory.target_ref:
        raise ContentFactoryError("CONTENT_FACTORY_TARGET_REQUIRED")
    if not factory.publisher_account_id:
        raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_ACCOUNT_REQUIRED")
    account = await session.get(TelegramAccount, factory.publisher_account_id)
    if not account or account.workspace_id != factory.workspace_id or account.status in {"deleted", "invalid"}:
        raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_ACCOUNT_UNAVAILABLE")
    if account.restriction_status and account.restriction_status != "clear":
        raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_ACCOUNT_RESTRICTED")
    if account.warmup_status in {"quarantined", "paused_by_risk"}:
        raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_ACCOUNT_QUARANTINED")
    if account.proxy_required and not account.proxy_id:
        raise ContentFactoryError("CONTENT_FACTORY_PUBLISHER_PROXY_REQUIRED")


async def collect_source_inputs(session: AsyncSession, factory: ContentFactory, run_id: str) -> dict:
    result = await session.execute(
        select(ContentFactorySource).where(
            ContentFactorySource.factory_id == factory.id, ContentFactorySource.enabled.is_(True)
        )
    )
    source_links = result.scalars().all()
    source_ids = [item.source_id for item in source_links if item.source_id]
    if not source_ids:
        return {"items": [], "refs": [], "summary": "Источники не подключены; генерация идет по промпту и базе знаний."}

    await ensure_content_factory_source_posts(session, factory, source_links, run_id)

    messages = await session.execute(
        select(TelegramMessage, TelegramSource)
        .join(TelegramSource, TelegramSource.id == TelegramMessage.source_id)
        .where(TelegramMessage.workspace_id == factory.workspace_id, TelegramMessage.source_id.in_(source_ids))
        .order_by(TelegramMessage.published_at.desc().nullslast(), TelegramMessage.created_at.desc())
        .limit(30)
    )
    items = []
    refs = []
    for message, source in messages.all():
        text = _clean_text(message.text or "")
        if len(text) < 80:
            continue
        ref = source.url or (
            f"https://t.me/{source.username}/{message.tg_message_id}" if source.username else message.tg_message_id
        )
        refs.append(ref)
        items.append(
            {
                "id": message.id,
                "source_type": "telegram_source",
                "source_ref": source.title or source.username or source.id,
                "title": source.title,
                "text": text[:1400],
                "url": ref,
                "published_at": message.published_at.isoformat() if message.published_at else None,
                "freshness_score": 70,
                "relevance_score": 60,
                "trust_score": 50,
            }
        )
    summary = "\n\n".join(f"- {item['source_ref']}: {item['text'][:360]}" for item in items[:8])
    await add_event(
        session,
        factory.id,
        run_id,
        None,
        "info",
        "research.collected",
        "Материалы источников собраны",
        {"count": len(items)},
    )
    return {"items": items, "refs": refs[:12], "summary": summary}


async def ensure_content_factory_source_posts(
    session: AsyncSession,
    factory: ContentFactory,
    source_links: list[ContentFactorySource],
    run_id: str | None,
) -> None:
    source_ids = [item.source_id for item in source_links if item.source_id]
    if not source_ids or not factory.publisher_account_id:
        return
    account = await session.get(TelegramAccount, factory.publisher_account_id)
    if not account or account.workspace_id != factory.workspace_id or account.status in {"deleted", "invalid"}:
        return

    sources_result = await session.execute(select(TelegramSource).where(TelegramSource.id.in_(source_ids)))
    sources_by_id = {source.id: source for source in sources_result.scalars().all()}
    now = datetime.now(UTC)
    fetched_total = 0
    failed: list[dict[str, str]] = []
    for link in source_links:
        if not link.enabled or not link.source_id:
            continue
        source = sources_by_id.get(link.source_id)
        if not source:
            continue
        if not should_refresh_content_factory_source(link, source, now):
            continue
        try:
            fetched_total += await fetch_content_factory_source_posts(session, factory, account, link, source)
        except Exception as exc:  # noqa: BLE001
            failed.append(
                {
                    "source_id": source.id,
                    "source_ref": link.source_ref or source.username or source.url or source.id,
                    "error": str(exc)[:240],
                }
            )

    if fetched_total or failed:
        await add_event(
            session,
            factory.id,
            run_id,
            None,
            "info" if not failed else "warning",
            "sources.refreshed",
            "Источники контент-завода обновлены" if not failed else "Часть источников не удалось обновить",
            {"fetched": fetched_total, "failed": failed[:5]},
        )
        await session.commit()


def should_refresh_content_factory_source(link: ContentFactorySource, source: TelegramSource, now: datetime) -> bool:
    settings = link.settings if isinstance(link.settings, dict) else {}
    refresh_minutes = max(5, min(24 * 60, parse_int(settings.get("refresh_minutes"), 60)))
    last = link.last_parsed_at or source.last_parsed_at
    if not last:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last.astimezone(UTC)).total_seconds() >= refresh_minutes * 60


async def fetch_content_factory_source_posts(
    session: AsyncSession,
    factory: ContentFactory,
    account: TelegramAccount,
    link: ContentFactorySource,
    source: TelegramSource,
) -> int:
    source_ref = link.source_ref or source_ref_from_fields(source.username, source.url, source.tg_id)
    if not source_ref:
        return 0
    settings = link.settings if isinstance(link.settings, dict) else {}
    limit = max(5, min(50, parse_int(settings.get("post_limit"), 20)))
    posts = await fetch_posts_with_retries(session, account, source_ref, limit)
    now = datetime.now(UTC)
    upserted = 0
    for post in posts:
        text = _clean_text(post.text or "")
        if not text:
            continue
        with session.no_autoflush:
            existing_result = await session.execute(
                select(TelegramMessage).where(
                    TelegramMessage.workspace_id == factory.workspace_id,
                    TelegramMessage.source_id == source.id,
                    TelegramMessage.tg_message_id == post.tg_message_id,
                )
            )
        message = existing_result.scalar_one_or_none()
        if message:
            message.text = post.text
            message.published_at = post.published_at
            message.views_count = post.views_count
            message.forwards_count = post.forwards_count
            message.replies_count = post.replies_count
            message.raw = {**(post.raw or {}), "content_factory_source_link_id": link.id}
        else:
            message = TelegramMessage(
                workspace_id=factory.workspace_id,
                source_id=source.id,
                tg_message_id=post.tg_message_id,
                text=post.text,
                published_at=post.published_at,
                views_count=post.views_count,
                forwards_count=post.forwards_count,
                replies_count=post.replies_count,
                raw={**(post.raw or {}), "content_factory_source_link_id": link.id},
            )
            session.add(message)
        upserted += 1
    source.last_parsed_at = now
    source.access_status = "available"
    link.last_parsed_at = now
    await session.flush()
    return upserted


async def collect_knowledge_text(session: AsyncSession, factory: ContentFactory) -> str:
    result = await session.execute(
        select(ContentFactoryKnowledgeItem)
        .where(ContentFactoryKnowledgeItem.factory_id == factory.id, ContentFactoryKnowledgeItem.enabled.is_(True))
        .order_by(ContentFactoryKnowledgeItem.created_at.desc())
        .limit(20)
    )
    parts = []
    for item in result.scalars().all():
        text = _clean_text(item.content_text or "")
        if text:
            parts.append(f"### {item.title}\n{text[:3000]}")
    return "\n\n".join(parts)[:12000]


def build_content_factory_slot_brief(
    factory: ContentFactory,
    *,
    trigger_type: str,
    now_utc: datetime | None = None,
    slot_index_override: int | None = None,
    posts_per_day_override: int | None = None,
) -> ContentFactorySlotBrief:
    config = normalize_schedule_config(factory.schedule_config if isinstance(factory.schedule_config, dict) else {})
    content_config = factory.content_config if isinstance(factory.content_config, dict) else {}
    timezone = content_factory_timezone(str(config.get("timezone") or "Europe/Moscow"))
    now_local = ensure_aware_utc(now_utc or datetime.now(UTC)).astimezone(timezone)  # type: ignore[union-attr]
    slots = content_factory_schedule_slot_minutes(config)
    posts_per_day = max(
        1,
        min(
            CONTENT_FACTORY_MAX_POSTS_PER_DAY,
            posts_per_day_override
            if posts_per_day_override is not None
            else parse_int(config.get("posts_per_day"), len(slots) or 1),
        ),
    )
    slot_index = (
        slot_index_override if slot_index_override is not None else content_factory_slot_index(config, now_local)
    )
    plan = normalize_content_plan(content_config.get("content_plan") or "")
    day_theme = content_factory_day_theme(factory, now_local, plan)
    subtopics = content_factory_subtopics_for_day(day_theme, posts_per_day)
    subtopic = subtopics[slot_index % len(subtopics)] if subtopics else day_theme
    angle = content_factory_angle_for_slot(day_theme, subtopic, slot_index)
    return ContentFactorySlotBrief(
        trigger_type=trigger_type,
        posts_per_day=posts_per_day,
        slot_index=slot_index,
        slot_count=max(1, len(slots)),
        slot_local_time=minutes_to_time_label(
            slots[min(slot_index, len(slots) - 1)] if slots else now_local.hour * 60 + now_local.minute
        ),
        weekday_label=CONTENT_FACTORY_WEEKDAY_VALUES[now_local.weekday()],
        day_theme=day_theme,
        subtopic=subtopic,
        angle=angle,
    )


async def build_content_factory_slot_brief_for_run(
    session: AsyncSession,
    factory: ContentFactory,
    *,
    trigger_type: str,
    now_utc: datetime | None = None,
) -> ContentFactorySlotBrief:
    if trigger_type not in {"manual", "retry"}:
        return build_content_factory_slot_brief(factory, trigger_type=trigger_type, now_utc=now_utc)

    config = normalize_schedule_config(factory.schedule_config if isinstance(factory.schedule_config, dict) else {})
    timezone = content_factory_timezone(str(config.get("timezone") or "Europe/Moscow"))
    now = ensure_aware_utc(now_utc or datetime.now(UTC))
    now_local = now.astimezone(timezone)  # type: ignore[union-attr]
    day_start_utc, day_end_utc = content_factory_local_day_bounds_utc(now_local, timezone)
    recent_count = await count_factory_posts_between(session, factory, day_start_utc, day_end_utc)
    manual_topic_count = max(5, parse_int(config.get("posts_per_day"), 1))
    manual_topic_count = min(CONTENT_FACTORY_MAX_POSTS_PER_DAY, manual_topic_count)
    return build_content_factory_slot_brief(
        factory,
        trigger_type=trigger_type,
        now_utc=now,
        slot_index_override=recent_count % manual_topic_count,
        posts_per_day_override=manual_topic_count,
    )


def content_factory_local_day_bounds_utc(now_local: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    return day_start_local.astimezone(UTC), day_end_local.astimezone(UTC)


async def count_factory_posts_between(
    session: AsyncSession,
    factory: ContentFactory,
    start_utc: datetime,
    end_utc: datetime,
) -> int:
    result = await session.execute(
        select(func.count(ContentPost.id)).where(
            ContentPost.factory_id == factory.id,
            ContentPost.created_at >= start_utc,
            ContentPost.created_at < end_utc,
        )
    )
    return int(result.scalar_one() or 0)


async def validate_workspace_daily_generation_limit(
    session: AsyncSession,
    factory: ContentFactory,
    *,
    now_utc: datetime | None = None,
) -> None:
    config = normalize_schedule_config(factory.schedule_config if isinstance(factory.schedule_config, dict) else {})
    timezone = content_factory_timezone(str(config.get("timezone") or "Europe/Moscow"))
    now = ensure_aware_utc(now_utc or datetime.now(UTC))
    now_local = now.astimezone(timezone)  # type: ignore[union-attr]
    day_start_utc, day_end_utc = content_factory_local_day_bounds_utc(now_local, timezone)
    result = await session.execute(
        select(func.count(ContentPost.id))
        .select_from(ContentPost)
        .join(ContentFactory, ContentFactory.id == ContentPost.factory_id)
        .where(
            ContentFactory.workspace_id == factory.workspace_id,
            ContentPost.created_at >= day_start_utc,
            ContentPost.created_at < day_end_utc,
        )
    )
    used = int(result.scalar_one() or 0)
    if used >= CONTENT_FACTORY_WORKSPACE_DAILY_GENERATION_LIMIT:
        raise ContentFactoryError(
            f"CONTENT_FACTORY_DAILY_GENERATION_LIMIT_REACHED:{used}/{CONTENT_FACTORY_WORKSPACE_DAILY_GENERATION_LIMIT}"
        )


def content_factory_slot_index(config: dict, now_local: datetime) -> int:
    slots = content_factory_schedule_slot_minutes(config)
    if not slots:
        return 0
    current_minutes = now_local.hour * 60 + now_local.minute
    nearest_index = min(range(len(slots)), key=lambda index: abs(slots[index] - current_minutes))
    return max(0, min(len(slots) - 1, nearest_index))


def content_factory_day_theme(factory: ContentFactory, now_local: datetime, plan: str) -> str:
    if plan:
        line = find_content_plan_line_for_weekday(plan, now_local.weekday())
        if line:
            return line
        return plan.splitlines()[0].strip()[:500]
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    goal = str(config.get("goal") or config.get("prompt") or DEFAULT_CONTENT_PROMPT).strip()
    weekday_defaults = [
        "Понедельник — контекст, проблема недели, главный пользовательский сценарий и зачем продукт нужен сейчас",
        "Вторник — функции, ограничения, статусы, настройки и практическая польза продукта",
        "Среда — источники, база знаний, материалы, фактура и превращение данных в контент",
        "Четверг — черновики, согласование, качество, публикации и безопасный рабочий процесс",
        "Пятница — кейсы, рабочие процессы, аналитика, ошибки и выводы недели",
        "Суббота — антипримеры, разбор частых ошибок, что ломается без системы",
        "Воскресенье — дайджест, мягкий обзор пользы, планы и идеи для следующей недели",
    ]
    return f"{weekday_defaults[now_local.weekday()]}. Контекст завода: {goal[:360]}"


def find_content_plan_line_for_weekday(plan: str, weekday: int) -> str | None:
    aliases = [
        ("понедельник", "пн", "monday", "mon"),
        ("вторник", "вт", "tuesday", "tue"),
        ("среда", "ср", "wednesday", "wed"),
        ("четверг", "чт", "thursday", "thu"),
        ("пятница", "пт", "friday", "fri"),
        ("суббота", "сб", "saturday", "sat"),
        ("воскресенье", "вс", "sunday", "sun"),
    ][weekday]
    for raw_line in plan.splitlines():
        line = raw_line.strip(" -•\t")
        if not line:
            continue
        lowered = line.lower()
        if any(
            lowered.startswith(alias)
            or lowered.startswith(f"{alias} ")
            or lowered.startswith(f"{alias} —")
            or lowered.startswith(f"{alias}:")
            for alias in aliases
        ):
            return line[:500]
    return None


def content_factory_subtopics_for_day(day_theme: str, posts_per_day: int) -> list[str]:
    clean = re.sub(r"^[^—:-]+[—:-]\s*", "", day_theme).strip() or day_theme.strip()
    if posts_per_day <= 1:
        return [clean[:500]]
    editorial_angles = [
        "стратегия Telegram-трафика: аудитория, оффер, источник, касание, заявка и следующий шаг",
        "Лидогенерация в Telegram: где человек теряется между постом, комментарием, диалогом и менеджером",
        "Источники трафика и конкурентная разведка: как брать фактуру из каналов, постов и реакций без копирования",
        "контент и доверие: как пост, скриншот, CTA и тон подводят человека к диалогу",
        "инфраструктура роста: аккаунты, прогрев, лимиты, прокси и статусы как часть одного процесса",
        "кампании и касания: как собрать гипотезу, запустить действия и видеть, что реально произошло",
        "аналитика и логи: как понять, где воронка сломалась и что чинить первым",
        "ошибка или антипример: почему активность есть, а управляемой системы трафика нет",
        "продуктовый сценарий demo-platform: какой экран, модуль или настройка помогает в этой теме",
        "фаундерский вывод: что стоит протестировать на следующей неделе и почему",
    ]
    patterns = [
        "начать с понятного контекста для нового читателя",
        "раскрыть через конкретную боль предпринимателя, маркетолога или агентства",
        "дать рабочий сценарий: что пользователь делает в интерфейсе или процессе",
        "показать практический пример без канцелярита и общих слов",
        "разобрать частую ошибку и нормальный порядок действий",
        "связать тему с одним подходящим модулем demo-platform, но не превращать пост в справку",
        "показать ограничение или риск и аккуратный способ с ним работать",
        "закончить человеческим выводом или мягким CTA",
        "использовать другой заход, заголовок и ритм, чем в соседних постах",
        "не сводить широкую тему дня к одному слову вроде прокси, черновики или логи",
    ]
    result: list[str] = []
    for index in range(max(1, min(CONTENT_FACTORY_MAX_POSTS_PER_DAY, posts_per_day))):
        base = clean[:260]
        editorial_angle = editorial_angles[index % len(editorial_angles)]
        result.append(f"{base}: {editorial_angle}; {patterns[index % len(patterns)]}")
    return result


def content_factory_angle_for_slot(day_theme: str, subtopic: str, slot_index: int) -> str:
    variants = [
        "объяснить для нового читателя без внутренних терминов",
        "показать через боль и рабочую ситуацию",
        "разобрать как продуктовый сценарий",
        "дать практический чеклист без шаблонного списка",
        "сравнить хаотичный и управляемый подход",
        "показать ошибку до запуска и правильный порядок",
        "дать наблюдение автора и мягкий вывод",
        "связать с конкретным экраном, модулем или материалом базы знаний",
        "сделать короткий антипример и вывод",
        "собрать мини-дайджест внутри темы дня",
    ]
    return f"{variants[slot_index % len(variants)]}; не повторять общий тезис дня, раскрыть именно подтему «{subtopic}»"


def minutes_to_time_label(value: int) -> str:
    hours = max(0, min(23, value // 60))
    minutes = max(0, min(59, value % 60))
    return f"{hours:02d}:{minutes:02d}"


async def collect_recent_post_memory(session: AsyncSession, factory: ContentFactory) -> str:
    result = await session.execute(
        select(ContentPost)
        .where(
            ContentPost.factory_id == factory.id,
            ContentPost.status.in_(["published", "approved", "needs_review", "draft"]),
        )
        .order_by(ContentPost.created_at.desc())
        .limit(10)
    )
    parts = []
    for post in result.scalars().all():
        metadata = post.post_metadata if isinstance(post.post_metadata, dict) else {}
        image_title = str(metadata.get("knowledge_image_title") or "").strip()
        image_note = f" [knowledge_image: {image_title}]" if image_title else ""
        parts.append(f"- {post.title or 'Без названия'}{image_note}: {post.text[:260]}")
    return "\n".join(parts)


async def generate_post_payload(
    factory: ContentFactory,
    source_inputs: dict,
    knowledge_text: str,
    slot_brief: ContentFactorySlotBrief,
    *,
    topic_prompt: str | None = None,
    premium_emoji_catalog: list[PremiumEmojiEntry] | None = None,
    recent_memory: str = "",
) -> dict:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    image_config = factory.image_config if isinstance(factory.image_config, dict) else {}
    brand_name = _content_factory_brand_name(factory)
    brand_instruction = (
        f"Каноническое название бренда/продукта: {brand_name}. "
        "Используй именно это написание. Старые варианты из базы знаний, источников или истории постов не копируй."
        if brand_name
        else "Если в настройках явно задано название продукта, соблюдай его написание и не придумывай альтернативный нейминг."
    )
    product_grounding_enabled = content_factory_product_grounding_enabled(factory)
    product_focus = (
        infer_content_factory_product_focus(factory, slot_brief, topic_prompt, knowledge_text)
        if product_grounding_enabled
        else {}
    )
    knowledge_image_catalog = extract_knowledge_image_catalog(
        knowledge_text, product_focus, limit=20 if not product_grounding_enabled else 12
    )
    product_focus_block = (
        build_product_focus_prompt_block(product_focus, knowledge_image_catalog)
        if product_grounding_enabled
        else build_visual_persona_prompt_block(knowledge_image_catalog)
    )
    topic_prompt = normalize_manual_topic_prompt(topic_prompt)
    premium_emoji_catalog = list(premium_emoji_catalog or [])
    author_gender = normalize_author_gender(config.get("author_gender"))
    author_gender_instructions = author_gender_prompt_block(author_gender)
    custom_system_prompt = _content_factory_custom_system_prompt(factory)
    max_length = _content_factory_max_length(factory)
    requested_min_length = int(config.get("min_length") or 400)
    required_custom_footer = _content_factory_required_footer(factory) if custom_system_prompt else ""
    footer = required_custom_footer if custom_system_prompt else _content_factory_footer(factory)
    footer_separator = "\n\n" if footer else ""
    reserved_footer_length = len(footer_separator) + len(footer)
    body_max_length = max(0, max_length - reserved_footer_length)
    min_length = min(requested_min_length, max_length)
    body_min_length = min(requested_min_length, body_max_length) if body_max_length else 0
    footer_budget_instruction = (
        f"Обязательный footer ровно такой: {footer}"
        if footer
        else (
            "Footer/CTA/ссылки определяются только пользовательским системным промптом. "
            "Если он требует CTA или ссылки без точного шаблона, учитывай их внутри общего Telegram-лимита. Если он не требует footer, не добавляй его автоматически."
            if custom_system_prompt
            else "Footer не задан: не выдумывай финальную ссылку, если её нет в базе знаний, источниках или настройках."
        )
    )
    premium_emoji_instruction = premium_emoji_prompt_instruction(premium_emoji_catalog)
    manual_topic_mode = bool(topic_prompt)
    json_schema_instruction = (
        'JSON schema: {"title": "короткое внутреннее название", "text": "готовая короткая Telegram-подпись от лица persona: 1-4 живые строки, бытовой блоговый тон, без Markdown, product/CTA/links и без AI-поэзии", "image_prompt": "USE_KNOWLEDGE_IMAGE: filename.png | reason", "source_refs": [], "quality_notes": ["русские служебные заметки"], "risk_flags": [], "rubric": "mood/blog caption", "angle": "живой угол подписи", "key_claims": []}'
        if not product_grounding_enabled
        else 'JSON schema: {"title": "...", "text": "Готовый Telegram-пост с Markdown-ссылками, короткими абзацами, жирным заголовком и footer-ссылкой", "image_prompt": "USE_KNOWLEDGE_IMAGE: filename.png | reason OR короткая идея картинки под пост", "product_module": "конкретный модуль продукта", "product_feature": "конкретная функция/режим/экран", "concrete_example": "одна рабочая ситуация из поста", "source_refs": [], "quality_notes": ["только русские служебные заметки, без английского"], "risk_flags": [], "rubric": "тема дня или рубрика", "angle": "конкретный редакционный угол текущего слота", "key_claims": [], "cta": "короткий CTA со ссылкой"}'
    )
    slot_instruction = (
        "Ручное задание пользователя главнее контент-плана и рассчитанного слота. Слот используй только как фоновую редакционную подсказку, если он не конфликтует с ручным заданием."
        if manual_topic_mode
        else "Контент-план и слот запуска уже рассчитаны платформой. Пиши строго под переданную подтему слота, не пытайся самостоятельно повторно планировать день."
    )
    manual_topic_contract = (
        "\n".join(
            [
                "Режим ручной темы:",
                "- выполни конкретный запрос пользователя как главное ТЗ;",
                "- если пользователь задал структуру, соблюдай её буквально: заголовок, заход, количество абзацев, финал, запреты;",
                "- если пользователь просит вводный/обзорный пост, не углубляйся в один модуль и не превращай текст в плановый пост по слоту;",
                "- если пользователь просит сильный заголовок, первая строка поля text должна быть отдельным заголовком в Markdown, лучше **жирным**;",
                "- контент-план, слот дня и подтема не должны менять тему ручного запроса; используй их только для тона и контекста, если они помогают;",
                "- текст должен быть самостоятельным постом, а не пересказом задания.",
            ]
        )
        if manual_topic_mode
        else ""
    )
    if custom_system_prompt:
        system_prompt = "\n".join(
            [
                custom_system_prompt,
                "",
                "Низкоуровневые правила платформы:",
                author_gender_instructions,
                "Не выдумывай факты: если тезис не следует из источников или базы знаний, помечай его как мнение или не используй.",
                "Не обещай гарантированные результаты, отсутствие банов, обход ограничений или агрессивный спам.",
                "Соблюдай Telegram-формат: короткие абзацы, пустая строка между блоками, аккуратный Markdown.",
                brand_instruction,
                premium_emoji_instruction,
                product_focus_block,
                slot_instruction,
                manual_topic_contract,
                "Если передан блок последних постов, используй его только как список запретов на повторы. Не копируй темы, заходы, заголовки, модули и knowledge_image из него.",
                "Скриншоты выбирай безопасно и по теме продуктового модуля: если пост про контент-завод, выбирай скриншот контент-завода; если про прогрев, выбирай прогрев; если про источники, выбирай источники. Не используй приватные админские, operations, Sentry, alert/error dashboards или внутренние служебные экраны. Не повторяй knowledge_image из истории последних постов; если подходящий публичный скрин уже использовался недавно, лучше сгенерируй новую редакционную обложку.",
                "Поле image_prompt заполняй кратко. Подробный визуальный бриф находится в отдельной настройке изображения, не смешивай его с текстом поста.",
                "Верни только валидный JSON без Markdown-обёртки.",
            ]
        )
        author_instructions = [
            f"Пользовательский системный промпт:\n{custom_system_prompt}",
            author_gender_instructions,
            "Пользовательский системный промпт главнее редакторских настроек текста. Отдельный промпт изображения используется только для картинки и не должен менять текст поста.",
        ]
    else:
        system_prompt = "\n".join(
            [
                "Ты редактор живого Telegram-канала о продукте пользователя. Пиши новый авторский текст, не копируй источники дословно.",
                author_gender_instructions,
                "Не выдумывай факты: если тезис не следует из источников или базы знаний, помечай его как мнение или не используй.",
                "Не обещай гарантированные результаты, отсутствие банов, обход ограничений или агрессивный спам.",
                "Текст поста должен быть готов для Telegram: короткие абзацы, пустая строка между абзацами, аккуратное Markdown-выделение.",
                "Не возвращай сплошное полотно текста. Используй 4-7 смысловых блоков, **жирное** для 1-4 ключевых фраз и только уместные списки.",
                "Пост должен отвечать на три вопроса: о чём речь, зачем это читателю, как это связано с продуктом/заводом.",
                brand_instruction,
                premium_emoji_instruction,
                product_focus_block,
                slot_instruction,
                manual_topic_contract,
                "Если передан блок последних постов, используй его только как список запретов на повторы. Не копируй темы, заходы, заголовки, модули и knowledge_image из него.",
                "Скриншоты выбирай безопасно и по теме продуктового модуля: если пост про контент-завод, выбирай скриншот контент-завода; если про прогрев, выбирай прогрев; если про источники, выбирай источники. Не используй приватные админские, operations, Sentry, alert/error dashboards или внутренние служебные экраны. Не повторяй knowledge_image из истории последних постов; если подходящий публичный скрин уже использовался недавно, лучше сгенерируй новую редакционную обложку.",
                EDITORIAL_STYLE_GUIDE,
                EDITORIAL_FORMAT_VARIANTS,
                "Верни только валидный JSON без Markdown.",
            ]
        )
        author_instructions = [
            f"Цель и промпт завода:\n{config.get('goal') or DEFAULT_CONTENT_PROMPT}",
            author_gender_instructions,
            f"Формат: {config.get('format_preset') or 'telegram_structured_post'}",
            f"Tone of voice: {config.get('tone') or 'живой, уверенный, без инфоцыганщины'}",
            "Редакционная задача: выбери конкретную тему из базы знаний, источников или промпта и раскрой её так, чтобы новый читатель сразу понял контекст. Не пиши обезличенно и не делай общий пост «про контроль» без конкретного сценария.",
            "Пиши как продуктовый канал: пост должен продавать понимание конкретной возможности, обновления, сценария, ограничения, кейса или пользы. Называй продукт/платформу в тексте, если это уместно.",
            "Структура гибкая: можно использовать интро, кейс, чеклист, вопрос-ответ, разбор модуля, антипример или наблюдение. Нельзя повторять один и тот же скелет в соседних постах. В JSON строке используй \\n\\n между абзацами.",
            "Если используешь список, он должен быть оправдан темой; не вставляй 1️⃣ 2️⃣ 3️⃣ автоматически. Если список не нужен, пиши абзацами.",
            "Заголовок должен быть конкретным, не абстрактным: лучше «Как черновики спасают публикацию от ошибки» вместо «Контроль важнее хаоса».",
            "Обязательный паттерн ссылки: если в настройках или базе есть сайт/модуль/канал, добавь Markdown-ссылку внутри текста и короткий footer в последнем абзаце. Пример footer-логики: 🤖 **[Платформа](https://...)** | **[Канал](https://...)**. Не оставляй пост без ссылки.",
            f"Главная ссылка продукта: {_content_factory_primary_link(factory) or 'не указана, используй ссылки из базы знаний или источников'}",
            f"Изображения: {'включены' if image_config.get('enabled') else 'выключены'}; режим: {'только изображения из базы знаний' if _image_knowledge_only(factory) else 'скриншоты из базы или генерация'}; стиль: {image_config.get('style') or 'editorial'}. Для картинки есть отдельный промпт изображения в настройках завода; не смешивай визуальный бриф с текстом поста.",
            (
                "Если изображения включены в режиме только базы знаний, поле image_prompt должно начинаться с USE_KNOWLEDGE_IMAGE и выбирать безопасный публичный файл из базы. Не предлагай генеративную обложку."
                if _image_knowledge_only(factory)
                else "Если изображения включены и пост разбирает конкретный экран/модуль, можешь выбрать скриншот из базы через USE_KNOWLEDGE_IMAGE. В остальных случаях дай короткую идею image_prompt; backend применит отдельный визуальный промпт завода."
            ),
        ]
    user_prompt = "\n\n".join(
        [
            f"Название завода: {factory.name}",
            *(
                [
                    "ГЛАВНОЕ ЗАДАНИЕ ДЛЯ ЭТОГО РУЧНОГО ПОСТА:",
                    topic_prompt,
                    "Сделай пост именно про эту тему. Это задание важнее контент-плана, подтемы и слота дня. Контент-план и слот можно использовать только как фоновую подсказку, если они не конфликтуют с этим заданием. Если задание содержит структуру, запреты, формат, требования к заголовку, финалу или изображению, соблюдай их буквально.",
                ]
                if topic_prompt
                else []
            ),
            (
                f"Фоновый редакционный слот, не главный для ручного поста:\n{slot_brief.as_prompt_block()}"
                if topic_prompt
                else f"Редакционный слот этого запуска:\n{slot_brief.as_prompt_block()}"
            ),
            *author_instructions,
            product_focus_block,
            f"Жёсткий Telegram-лимит: поле text целиком не длиннее {max_length} символов, включая Markdown, emoji, пробелы, переносы строк и footer.",
            f"Длина смысловой части до footer: от {body_min_length} до {body_max_length} символов. Не превышай этот бюджет изначально, не рассчитывай на обрезку после генерации.",
            f"Итоговая длина с footer: желательно от {min_length} до {max_length} символов.",
            f"{footer_budget_instruction} "
            + (
                f"Он уже учтён в лимите; оставь под него {reserved_footer_length} символов и ставь после пустой строки."
                if footer
                else ""
            ),
            f"База знаний:\n{knowledge_text or 'Нет базы знаний. Используй только промпт и источники.'}",
            f"Summary источников:\n{source_inputs.get('summary') or 'Источников нет.'}",
            f"Последние посты завода — НЕ ПОВТОРЯТЬ темы, заходы и knowledge_image:\n{recent_memory or 'Пока нет истории постов.'}",
            (
                "Не загружай и не пересказывай историю старых постов. Уникальность ручного поста обеспечивает конкретное задание пользователя; не подменяй его подтемой слота."
                if topic_prompt
                else "История выше нужна только для анти-повтора. Уникальность обеспечивает редакционный слот: используй его подтему, угол и локальное время как главный фокус."
            ),
            json_schema_instruction,
        ]
    )
    output, usage = await generate_text_response(
        system_prompt=build_content_factory_humanized_system_prompt(system_prompt, author_gender=author_gender),
        user_prompt=user_prompt,
        model=get_settings().openai_quality_model,
        temperature=0.78,
        max_output_tokens=max(800, min(2500, max_length * 2)),
    )
    payload = parse_json_object(output or "")
    payload["_usage"] = usage
    return payload


def quality_check(
    factory: ContentFactory,
    text: str,
    *,
    duplicate: ContentMemoryItem | None,
    style_duplicate: ContentMemoryItem | None = None,
) -> tuple[int, list[str]]:
    config = factory.quality_config if isinstance(factory.quality_config, dict) else {}
    content_config = factory.content_config if isinstance(factory.content_config, dict) else {}
    notes = []
    score = 82
    _ = duplicate
    _ = style_duplicate
    for word in config.get("hard_stop_words") or []:
        if str(word).strip() and str(word).lower() in text.lower():
            notes.append(f"Найдена запрещённая тема для этого завода: {word}")
            return 0, notes
    for word in config.get("stop_words") or []:
        if str(word).strip() and str(word).lower() in text.lower():
            score -= 20
            notes.append(f"Найдено стоп-слово: {word}")
    if len(re.findall(r"[\U0001f300-\U0001faff]", text)) > int(config.get("max_emoji", 12)):
        score -= 10
        notes.append("Слишком много эмодзи")
    if len(text) < int(content_config.get("min_length") or config.get("min_length") or 120):
        score -= 15
        notes.append("Пост слишком короткий")
    if len(text) > _content_factory_max_length(factory) * 1.15:
        score -= 10
        notes.append("Пост длиннее заданного максимума")
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    is_visual_persona_factory = not content_factory_product_grounding_enabled(factory)
    if len(paragraphs) < 3 and not is_visual_persona_factory:
        score -= 20
        notes.append("Нет нормального разделения на абзацы")
    if any(len(paragraph) > 520 for paragraph in paragraphs):
        score -= 10
        notes.append("Есть слишком длинный абзац")
    template_markers = [
        "Вот как это работает:",
        "Таким образом,",
        "Для этого и нужен",
    ]
    if sum(1 for marker in template_markers if marker.lower() in text.lower()) >= 2:
        score -= 8
        notes.append("Пост похож на шаблонный редакционный скелет")
    if _starts_with_generic_context(text):
        score -= 8
        notes.append("Начало слишком общее, нужен более понятный контекст темы")
    if (
        content_factory_product_grounding_enabled(factory)
        and _content_factory_primary_link(factory)
        and not _has_markdown_or_plain_url(text)
    ):
        score -= 16
        notes.append("Нет ссылки на продукт, платформу или источник")
    if content_factory_product_grounding_enabled(factory):
        product_notes = content_factory_product_grounding_notes(factory, text)
        if product_notes:
            score -= min(24, 8 * len(product_notes))
            notes.extend(product_notes)
    humanizer_notes = content_factory_humanizer_quality_notes(text)
    if humanizer_notes:
        score -= min(18, 6 * len(humanizer_notes))
        notes.extend(humanizer_notes)
    if not notes:
        notes.append("Базовая проверка качества пройдена")
    return max(0, min(100, score)), notes


async def fit_generated_post_to_telegram_limit(factory: ContentFactory, text: str) -> str:
    max_length = _content_factory_max_length(factory)
    if len(text) <= max_length:
        return text
    current = text
    for _ in range(2):
        current = await rewrite_generated_post_to_length(factory, current, max_length)
        current = apply_content_factory_post_format(factory, current, allow_truncate=False)
        current = enforce_content_factory_humanizer_output(current)
        if len(current) <= max_length:
            return current
    raise ContentFactoryError("CONTENT_FACTORY_POST_EXCEEDS_TELEGRAM_LIMIT")


async def rewrite_generated_post_to_length(factory: ContentFactory, text: str, max_length: int) -> str:
    custom_system_prompt = _content_factory_custom_system_prompt(factory)
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    author_gender = normalize_author_gender(config.get("author_gender"))
    footer = _content_factory_required_footer(factory) if custom_system_prompt else _content_factory_footer(factory)
    reserved_footer_length = len("\n\n") + len(footer) if footer else 0
    body_max_length = max(0, max_length - reserved_footer_length)
    system_prompt = "\n".join(
        [
            "Ты редактор Telegram-поста. Нужно сжать уже написанный пост до жёсткого лимита, не меняя смысл.",
            "Не обрезай текст механически и не ставь многоточие в конце.",
            "Сохрани Markdown-ссылки, CTA/footer, факты, тон и нормальные абзацы.",
            "Если в конце есть footer или ссылка, они обязательны и должны остаться в финальном тексте.",
            "Верни только валидный JSON без Markdown-обёртки.",
        ]
    )
    user_prompt = "\n\n".join(
        [
            f"Жёсткий лимит итогового поля text: {max_length} символов, включая emoji, Markdown, ссылки, пробелы и переносы.",
            (
                f"Автоматический footer должен остаться ровно таким: {footer}\n"
                f"Смысловая часть до footer должна быть не длиннее {body_max_length} символов."
                if footer
                else "Footer/CTA задан пользовательским промптом или уже есть в тексте. Если в исходном тексте есть финальный CTA, ссылка или footer, сохрани его, но сделай компактнее."
            ),
            "Сожми пост редакционно: убери повторы, общие фразы и лишние вводные. Не удаляй финальный CTA/ссылки.",
            f"Исходный пост ({len(text)} символов):\n{text}",
            'JSON schema: {"text": "готовый пост в лимите, без обрыва и без финального многоточия"}',
        ]
    )
    output, _usage = await generate_text_response(
        system_prompt=build_content_factory_humanized_system_prompt(system_prompt, author_gender=author_gender),
        user_prompt=user_prompt,
        model=get_settings().openai_quality_model,
        temperature=0.25,
        max_output_tokens=max(700, min(2200, max_length * 2)),
    )
    payload = parse_json_object(output or "")
    rewritten = enforce_content_factory_humanizer_output(_clean_generated_post_text(str(payload.get("text") or "")))
    return rewritten or text


def apply_content_factory_post_format(factory: ContentFactory, text: str, *, allow_truncate: bool = True) -> str:
    text = _clean_generated_post_text(text)
    max_length = _content_factory_max_length(factory)
    if _content_factory_custom_system_prompt(factory):
        footer = _content_factory_required_footer(factory)
        if footer:
            text = _remove_duplicate_footer_lines(text, footer)
        if footer and not _text_contains_footer(text, footer):
            text = f"{text.rstrip()}\n\n{footer}".strip()
        return _trim_generated_post_to_length(text, max_length) if allow_truncate else text
    footer = _content_factory_footer(factory)
    if not footer:
        return _trim_generated_post_to_length(text, max_length) if allow_truncate else text
    body = _remove_existing_footer(text, footer)
    separator = "\n\n" if body else ""
    budget = max(0, max_length - len(separator) - len(footer))
    body = _trim_generated_post_to_length(body, budget) if allow_truncate and budget else body
    return f"{body}{separator if body else ''}{footer}".strip()


def ensure_manual_topic_heading(factory: ContentFactory, text: str, title: str, topic_prompt: str) -> str:
    if not manual_topic_requires_heading(topic_prompt) or post_has_heading(text):
        return text
    clean_title = normalize_content_factory_brand_text(factory, _clean_generated_post_text(title)).strip()
    clean_title = re.sub(r"^[#*\s]+|[#*\s]+$", "", clean_title)[:120].strip()
    if not clean_title or clean_title.lower() in {"пост", "черновик", "без названия"}:
        clean_title = "Знакомство с платформой"
    return f"**{clean_title}**\n\n{text.strip()}".strip()


def manual_topic_requires_heading(topic_prompt: str) -> bool:
    lowered = topic_prompt.lower()
    markers = (
        "заголов",
        "первая строка",
        "структура",
        "вводный пост",
        "пост-знакомство",
        "знакомство",
        "интро",
        "обзорный пост",
    )
    return any(marker in lowered for marker in markers)


def post_has_heading(text: str) -> bool:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return False
    if first.startswith(("**", "#", ">", "🧩", "🚀", "🧭", "⚙️", "💡", "📌", "🔎", "🤖")):
        return True
    if len(first) <= 90 and not first.endswith((".", "!", "?")):
        return True
    return False


def _remove_existing_footer(text: str, footer: str) -> str:
    normalized = text.strip()
    if _footer_signature(normalized[-600:]) == _footer_signature(footer):
        return ""
    if normalized.endswith(footer):
        return normalized[: -len(footer)].strip()
    paragraphs = re.split(r"\n{2,}", normalized)
    while paragraphs and _footer_signature(paragraphs[-1]) == _footer_signature(footer):
        paragraphs.pop()
    if len(paragraphs) != len(re.split(r"\n{2,}", normalized)):
        return "\n\n".join(part.strip() for part in paragraphs if part.strip()).strip()
    return normalized


def _text_contains_footer(text: str, footer: str) -> bool:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    normalized_footer = re.sub(r"\s+", " ", footer).strip()
    if normalized_footer and normalized_footer in normalized_text:
        return True
    footer_signature = _footer_signature(footer)
    if not footer_signature:
        return False
    tail = text.strip()[-800:]
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", tail) if part.strip()]
    return any(_footer_signature(part) == footer_signature for part in [*lines, *paragraphs])


def _remove_duplicate_footer_lines(text: str, footer: str) -> str:
    footer_signature = _footer_signature(footer)
    if not footer_signature:
        return text
    lines = text.strip().splitlines()
    result: list[str] = []
    footer_seen = False
    for line in reversed(lines):
        if _footer_signature(line) == footer_signature:
            if footer_seen:
                continue
            footer_seen = True
        result.append(line)
    result.reverse()
    return "\n".join(result).strip()


def _footer_signature(value: str) -> str:
    normalized = _clean_generated_post_text(value)
    normalized = re.sub(r"\[\[emoji:[A-Za-z0-9_-]{1,40}\]\]\s*", "", normalized)
    normalized = re.sub(r"^[\U0001f300-\U0001faff]\ufe0f?\s*", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not _has_markdown_or_plain_url(normalized):
        return ""
    links = re.findall(r"\[[^\]]+\]\(https?://[^)]+\)|https?://\S+|t\.me/\S+", normalized, flags=re.IGNORECASE)
    return " | ".join(link.strip().lower() for link in links)


def _has_footer_like_link(text: str) -> bool:
    tail = text.strip()[-320:]
    if not tail:
        return False
    paragraphs = [part.strip() for part in tail.split("\n\n") if part.strip()]
    last = paragraphs[-1] if paragraphs else tail
    return bool(re.search(r"\[[^\]]+\]\(https?://[^)]+\)|https?://|t\.me/", last, re.IGNORECASE))


def _content_factory_custom_system_prompt(factory: ContentFactory) -> str:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    for key in ("system_prompt", "prompt", "prompt_override"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_manual_topic_prompt(value: str | None) -> str | None:
    topic = re.sub(r"\s+", " ", str(value or "")).strip()
    if not topic:
        return None
    return topic[:2000]


def infer_content_factory_product_focus(
    factory: ContentFactory,
    slot_brief: ContentFactorySlotBrief | None,
    topic_prompt: str | None,
    knowledge_text: str = "",
) -> dict[str, Any]:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    primary_context = " ".join(
        [
            str(topic_prompt or ""),
            slot_brief.day_theme if slot_brief else "",
            slot_brief.subtopic if slot_brief else "",
            slot_brief.angle if slot_brief else "",
        ]
    )
    haystack = " ".join(
        [
            primary_context,
            primary_context,
            primary_context,
            str(config.get("goal") or ""),
            str(config.get("prompt") or ""),
            factory.name or "",
            knowledge_text[:3000],
        ]
    ).lower()
    best_rule = PRODUCT_MODULE_FOCUS_RULES[0]
    best_score = -1
    for rule in PRODUCT_MODULE_FOCUS_RULES:
        score = sum(1 for alias in rule["aliases"] if str(alias).lower() in haystack)
        if score > best_score:
            best_rule = rule
            best_score = score
    return dict(best_rule)


def extract_knowledge_image_catalog(
    knowledge_text: str, product_focus: dict[str, Any], *, limit: int = 12
) -> list[str]:
    filenames = re.findall(r"Изображение/скриншот:\s*([^\n]+)", knowledge_text)
    if not filenames:
        filenames = re.findall(r"USE_KNOWLEDGE_IMAGE:\s*([^|\n]+)", knowledge_text)
    unique: list[str] = []
    seen: set[str] = set()
    for raw in filenames:
        filename = Path(str(raw).strip()).name
        lowered = filename.lower()
        if not filename or lowered in seen or is_sensitive_knowledge_image_name(lowered):
            continue
        seen.add(lowered)
        unique.append(filename)
    tokens = [str(token).lower() for token in product_focus.get("screenshot_tokens") or []]
    return sorted(
        unique,
        key=lambda filename: (0 if any(token in filename.lower() for token in tokens) else 1, filename.lower()),
    )[:limit]


def build_product_focus_prompt_block(product_focus: dict[str, Any], knowledge_image_catalog: list[str]) -> str:
    screenshots = (
        "\n".join(f"- {filename}" for filename in knowledge_image_catalog)
        or "Нет явного списка безопасных скриншотов в базе знаний."
    )
    return "\n".join(
        [
            "Обязательная продуктовая привязка:",
            f"- выбери и явно раскрой модуль продукта: {product_focus.get('name')};",
            f"- конкретные функции/режимы, которые можно использовать: {product_focus.get('features')};",
            f"- практическая польза модуля: {product_focus.get('benefit')};",
            "- пост должен назвать модуль или его понятную функцию в тексте, а не говорить только общими словами вроде платформа, процесс, порядок;",
            "- добавь одну конкретную рабочую ситуацию: что пользователь нажимает, выбирает, проверяет, редактирует, запускает или видит в интерфейсе;",
            "- если тема явно про Контент-завод, черновики, drafts_only, редактуру, расписание публикаций, базу знаний или изображения для постов, раскрывай модуль Контент-завод;",
            "- обычные слова вроде контент, пост, публикация или материал сами по себе не означают Контент-завод: сначала смотри на тему дня, источник трафика, лидогенерацию, аккаунты, комментарии и кампании;",
            "- если тема про прогрев, это модуль Прогрев аккаунтов;",
            "Доступные безопасные скриншоты из базы знаний, выбирай ближайший к модулю и теме:",
            screenshots,
            "Если подходящий скриншот есть, image_prompt должен начинаться строго так: USE_KNOWLEDGE_IMAGE: filename.png | почему этот экран подходит.",
        ]
    )


def build_visual_persona_prompt_block(knowledge_image_catalog: list[str]) -> str:
    images = (
        "\n".join(f"- {filename}" for filename in knowledge_image_catalog)
        or "Нет явного списка изображений в базе знаний."
    )
    return "\n".join(
        [
            "Режим визуального persona/lifestyle-канала:",
            "- продуктовая привязка отключена для этого завода;",
            "- не вставляй темы Telegram-трафика, лидогенерации, модулей платформы, источников, кампаний, прогрева, логов или SaaS-продукта, если пользователь явно не попросил;",
            "- пиши короткую живую подпись к фото, как в личном Telegram-блоге: 1-4 строки, простая человеческая фраза, эмоция в моменте, иногда лёгкая самоирония;",
            "- изображение используй как повод для бытового наблюдения или настроения, но не превращай текст в литературный монолог;",
            "- избегай AI-поэзии, туманных обобщений, слов вроде послевкусие/дистанция/внутренний голос/граница/пауза, если они не звучат как обычная речь;",
            "- не пиши полотна и не делай 4-9 абзацев для persona-captions; лучше одна живая мысль, чем красивый искусственный текст;",
            "- поле image_prompt должно выбрать подходящий файл из базы знаний через USE_KNOWLEDGE_IMAGE: filename.png | почему этот кадр подходит;",
            "- не называй filename в самом тексте поста.",
            "Доступные изображения из базы знаний:",
            images,
        ]
    )


def content_factory_product_grounding_enabled(factory: ContentFactory) -> bool:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    if "product_grounding_enabled" in config:
        return bool(config.get("product_grounding_enabled"))
    content_type = str(config.get("content_type") or config.get("channel_type") or "").strip().lower()
    if content_type in {"persona_diary", "visual_diary", "lifestyle_persona", "muse_diary"}:
        return False
    return True


def content_factory_product_grounding_notes(factory: ContentFactory, text: str) -> list[str]:
    lowered = text.lower()
    notes: list[str] = []
    brand = _content_factory_brand_name(factory)
    product_context = bool(brand or _content_factory_primary_link(factory))
    if not product_context:
        return notes
    module_terms = (
        "контент-завод",
        "контент завод",
        "прогрев",
        "аккаунт",
        "источник",
        "база знаний",
        "черновик",
        "расписание",
        "публикац",
        "комментар",
        "прокси",
        "лог",
        "статус",
        "quality-check",
        "проверка качества",
    )
    if not any(term in lowered for term in module_terms):
        notes.append("Пост слабо привязан к конкретному модулю продукта")
    if re.search(r"\b(платформа|система|процесс|порядок|контроль)\b", lowered) and not any(
        term in lowered for term in ("экран", "модул", "режим", "вкладк", "кнопк", "черновик", "контент-завод")
    ):
        notes.append("Слишком общий продуктовый текст, нужна конкретика интерфейса")
    return notes


def build_content_factory_humanized_system_prompt(system_prompt: str, *, author_gender: AuthorGender) -> str:
    return build_humanizer_system_prompt(
        profile_system_prompt=(
            f"Humanizer prompt version used by content factory: {HUMANIZER_PROMPT_VERSION}\n\n"
            f"{CONTENT_FACTORY_HUMANIZER_LAYER}\n\n{system_prompt.strip()}"
        ),
        safety_prompt=(
            "Content factory posts must stay safe for Telegram marketing: no guaranteed bans avoidance, "
            "no bypassing platform limits, no aggressive spam promises, no invented product claims."
        ),
        author_gender=author_gender,
    )


def enforce_content_factory_humanizer_output(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    replacements = {
        "\u2014": ",",
        "\u2013": "-",
        "«": "",
        "»": "",
        "“": "",
        "”": "",
        "„": "",
        '"': "",
        "'": "",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    cleaned = re.sub(r"^(конечно|отличный вопрос|рад помочь)[,.! ]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*(похоже|кажется)\s*,\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(Таким образом|В заключение|Надеюсь, это поможет)[,.]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(Важно отметить|Стоит понимать),?\s+что\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t\f\v]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def content_factory_humanizer_quality_notes(text: str) -> list[str]:
    notes = []
    violations = humanizer_violations(text)
    if "dash" in violations:
        notes.append("Humanizer: в тексте осталось длинное тире")
    if "quotes" in violations:
        notes.append("Humanizer: в тексте остались кавычки")
    if re.search(
        r"\b(Таким образом|В заключение|Надеюсь, это поможет|Важно отметить|Стоит понимать)\b",
        text,
        flags=re.IGNORECASE,
    ):
        notes.append("Humanizer: текст содержит AI-штампы")
    return notes


def content_factory_premium_emoji_catalog(factory: ContentFactory) -> list[PremiumEmojiEntry]:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    if not config.get("premium_emoji_enabled"):
        return []
    raw_catalog = config.get("premium_emoji_catalog")
    if raw_catalog is None:
        raw_catalog = config.get("premium_emoji_catalog_text")
    return parse_premium_emoji_catalog(raw_catalog)


async def content_factory_effective_premium_emoji_catalog(
    session: AsyncSession,
    factory: ContentFactory,
    publisher_account: TelegramAccount | None,
) -> list[PremiumEmojiEntry]:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    if not config.get("premium_emoji_enabled") or not publisher_account:
        return []

    manual_catalog = content_factory_premium_emoji_catalog(factory)
    auto_catalog: list[PremiumEmojiEntry] = []
    is_premium = publisher_account.is_premium
    try:
        is_premium, raw_auto_catalog = await fetch_account_premium_emoji_catalog(
            session,
            publisher_account,
            limit=CONTENT_FACTORY_PREMIUM_EMOJI_CATALOG_LIMIT,
        )
        auto_catalog = parse_premium_emoji_catalog(raw_auto_catalog)
    except TelegramGatewayError:
        raw_auto_catalog = []

    if not is_premium:
        return []
    return merge_premium_emoji_catalogs(manual_catalog, auto_catalog)


def merge_premium_emoji_catalogs(*catalogs: list[PremiumEmojiEntry]) -> list[PremiumEmojiEntry]:
    merged: list[PremiumEmojiEntry] = []
    seen: set[str] = set()
    for catalog in catalogs:
        for entry in catalog:
            if entry.alias in seen:
                continue
            merged.append(entry)
            seen.add(entry.alias)
            if len(merged) >= CONTENT_FACTORY_PREMIUM_EMOJI_CATALOG_LIMIT:
                return merged
    return merged


def normalize_content_factory_brand_text(factory: ContentFactory, text: str) -> str:
    brand = _content_factory_brand_name(factory)
    if not brand:
        return text
    normalized = text
    if brand.lower() == "demo-platform":
        normalized = re.sub(r"\bwhy\s+not\s+ai\b", brand, normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bdemo\s+platform\b", brand, normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bwhy\s+notai\b", brand, normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\bdemo-platform\b", brand, normalized, flags=re.IGNORECASE)
    return normalized


def _content_factory_brand_name(factory: ContentFactory) -> str:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    for key in ("brand_name", "product_name", "platform_name"):
        value = str(config.get(key) or "").strip()
        if value:
            return value[:80]
    haystack = " ".join(
        [
            str(config.get("goal") or ""),
            str(config.get("prompt") or ""),
            factory.name or "",
            str(config.get("product_site_url") or ""),
        ]
    )
    if re.search(r"\bdemo-platform\b", haystack, re.IGNORECASE):
        return "demo-platform"
    link = _content_factory_primary_link(factory)
    if link:
        host = urlparse(link).hostname or ""
        first_label = host.split(".")[0].strip()
        if first_label and first_label not in {"www", "t", "telegram"}:
            return first_label[:80]
    return ""


def normalize_content_factory_quality_notes(raw_notes: object) -> list[str]:
    if not isinstance(raw_notes, list):
        return []
    notes: list[str] = []
    for raw in raw_notes:
        note = str(raw or "").strip()
        if not note:
            continue
        lowered = note.lower()
        if is_hidden_content_factory_quality_note(lowered):
            continue
        if "sunday digest tone" in lowered:
            note = "Тон дайджеста адаптирован под базу знаний и историю постов."
        elif "kept within telegram length budget" in lowered:
            note = "Пост уложен в лимит Telegram и содержит обязательный footer."
        elif "included required footer" in lowered:
            note = "Обязательный footer добавлен."
        elif "adapted to available knowledge base" in lowered:
            note = "Пост адаптирован под доступную базу знаний."
        elif re.fullmatch(r"[\x00-\x7F]+", note):
            note = "Служебная заметка генерации переведена в журнал событий."
        notes.append(note[:300])
    return notes[:5]


def is_hidden_content_factory_quality_note(lowered_note: str) -> bool:
    hidden_markers = (
        "формат слишком похож",
        "похож на недавний пост",
        "слишком похож на недавний",
        "similar to a recent",
        "recent post",
        "нет конкретного пользовательского действия",
        "нет конкретного действия",
        "no concrete user action",
        "no specific user action",
    )
    return any(marker in lowered_note for marker in hidden_markers)


def parse_premium_emoji_catalog(raw_catalog: object) -> list[PremiumEmojiEntry]:
    rows: list[object]
    if isinstance(raw_catalog, str):
        rows = [line for line in raw_catalog.splitlines() if line.strip()]
    elif isinstance(raw_catalog, list):
        rows = raw_catalog
    else:
        rows = []

    catalog: list[PremiumEmojiEntry] = []
    seen: set[str] = set()
    for row in rows:
        alias = ""
        emoji = ""
        custom_emoji_id = ""
        if isinstance(row, dict):
            alias = str(row.get("alias") or row.get("name") or "").strip()
            emoji = str(row.get("emoji") or "").strip()
            custom_emoji_id = str(row.get("custom_emoji_id") or row.get("document_id") or row.get("id") or "").strip()
        else:
            parts = [part.strip() for part in str(row).split("|")]
            if len(parts) >= 3:
                alias, emoji, custom_emoji_id = parts[0], parts[1], parts[2]
        alias = normalize_premium_emoji_alias(alias)
        if not alias or alias in seen or not emoji or not custom_emoji_id.isdigit():
            continue
        catalog.append(PremiumEmojiEntry(alias=alias, emoji=emoji[:8], custom_emoji_id=custom_emoji_id))
        seen.add(alias)
    return catalog[:CONTENT_FACTORY_PREMIUM_EMOJI_CATALOG_LIMIT]


def normalize_premium_emoji_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", value.strip().lower())[:40]


def premium_emoji_prompt_instruction(catalog: list[PremiumEmojiEntry]) -> str:
    if not catalog:
        return "Премиум-эмодзи недоступны: не используй плейсхолдеры вида [[emoji:alias]]."
    aliases = ", ".join(f"[[emoji:{entry.alias}]] → {entry.emoji}" for entry in catalog[:24])
    footer_entry = next((entry for entry in catalog if entry.alias == "footer"), None)
    footer_rule = (
        f"Footer всегда начинай с [[emoji:{footer_entry.alias}]] вместо обычного emoji {footer_entry.emoji}. "
        if footer_entry
        else ""
    )
    return (
        "Премиум-эмодзи доступны только через плейсхолдеры из каталога. "
        + f"Каталог: {aliases}. "
        + footer_rule
        + "Если пост не строгий и не юридический, используй минимум 1 премиум-эмодзи внутри тела поста: "
        + "в смысловом переходе, коротком выводе, списке или важном акценте. Не оставляй premium emoji только в заголовке или footer. "
        + "Всего 1-3 штуки на пост внутри текста плюс обязательный footer-emoji, только по смыслу. "
        + "Не используй обычные emoji вне каталога: если нужен emoji, бери только символ или плейсхолдер из каталога. "
        + "Не выдумывай другие alias и не ставь плейсхолдеры подряд. Backend заменит их на Telegram custom emoji entities."
    )


def ensure_premium_emoji_placeholders_for_post(
    text: str,
    catalog: list[PremiumEmojiEntry],
    *,
    footer: str = "",
) -> str:
    if not catalog:
        return text
    normalized = _clean_generated_post_text(text)
    footer_entry = next((entry for entry in catalog if entry.alias == "footer"), None)
    if footer_entry:
        normalized = ensure_premium_footer_emoji_placeholder(normalized, footer_entry, footer=footer)

    body_entry = select_body_premium_emoji_entry(normalized, catalog)
    if body_entry and not has_non_footer_premium_emoji_placeholder(normalized):
        normalized = insert_body_premium_emoji_placeholder(normalized, body_entry, footer=footer)
    return normalized


def select_body_premium_emoji_entry(text: str, catalog: list[PremiumEmojiEntry]) -> PremiumEmojiEntry | None:
    body_entries = [entry for entry in catalog if entry.alias != "footer"]
    if not body_entries:
        return None
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    index = int(digest[:8], 16) % len(body_entries)
    return body_entries[index]


def has_non_footer_premium_emoji_placeholder(text: str) -> bool:
    for match in re.finditer(r"\[\[emoji:([A-Za-z0-9_-]{1,40})\]\]", text):
        if normalize_premium_emoji_alias(match.group(1)) != "footer":
            return True
    return False


def ensure_premium_footer_emoji_placeholder(text: str, entry: PremiumEmojiEntry, *, footer: str = "") -> str:
    if f"[[emoji:{entry.alias}]]" in text:
        return text
    lines = text.splitlines()
    footer_line_index = find_footer_line_index(lines, footer=footer)
    if footer_line_index is None:
        return text
    line = lines[footer_line_index]
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if stripped.startswith(entry.emoji):
        stripped = stripped[len(entry.emoji) :].lstrip()
    else:
        stripped = re.sub(r"^[\U0001f300-\U0001faff]\ufe0f?\s*", "", stripped, count=1)
    lines[footer_line_index] = f"{indent}[[emoji:{entry.alias}]] {stripped}".rstrip()
    return "\n".join(lines)


def insert_body_premium_emoji_placeholder(text: str, entry: PremiumEmojiEntry, *, footer: str = "") -> str:
    paragraphs = re.split(r"(\n{2,})", text)
    content_indexes = [index for index in range(0, len(paragraphs), 2) if paragraphs[index].strip()]
    if not content_indexes:
        return text

    footer_norm = re.sub(r"\s+", " ", footer).strip()
    body_indexes = []
    for index in content_indexes:
        paragraph_norm = re.sub(r"\s+", " ", paragraphs[index]).strip()
        if footer_norm and paragraph_norm == footer_norm:
            continue
        if (
            re.search(r"\[[^\]]+\]\(https?://[^)]+\)", paragraphs[index])
            and "Платформа" in paragraphs[index]
            and "Канал" in paragraphs[index]
        ):
            continue
        body_indexes.append(index)

    if not body_indexes:
        return text
    target_index = body_indexes[1] if len(body_indexes) > 1 else body_indexes[0]
    paragraph = paragraphs[target_index].lstrip()
    indent = paragraphs[target_index][: len(paragraphs[target_index]) - len(paragraph)]
    if paragraph.startswith("[[emoji:") or paragraph.startswith(entry.emoji):
        return text
    paragraphs[target_index] = f"{indent}[[emoji:{entry.alias}]] {paragraph}"
    return "".join(paragraphs)


def find_footer_line_index(lines: list[str], *, footer: str = "") -> int | None:
    footer_norm = re.sub(r"\s+", " ", footer).strip()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if not line:
            continue
        line_norm = re.sub(r"\s+", " ", line).strip()
        if footer_norm and line_norm == footer_norm:
            return index
        if re.search(r"\[[^\]]+\]\(https?://[^)]+\)", line) and "Платформа" in line and "Канал" in line:
            return index
    return None


def apply_premium_emoji_placeholders(text: str, catalog: list[PremiumEmojiEntry]) -> tuple[str, list[dict[str, Any]]]:
    if not catalog:
        return text, []
    entries = {entry.alias: entry for entry in catalog}
    entities: list[dict[str, Any]] = []
    output_text = text

    if "[[emoji:" in text:
        output_parts: list[str] = []
        cursor = 0
        output_text = ""
        pattern = re.compile(r"\[\[emoji:([A-Za-z0-9_-]{1,40})\]\]")
        for match in pattern.finditer(text):
            before = text[cursor : match.start()]
            output_parts.append(before)
            output_text += before
            alias = normalize_premium_emoji_alias(match.group(1))
            entry = entries.get(alias)
            if entry:
                offset = utf16_length(output_text)
                output_parts.append(entry.emoji)
                output_text += entry.emoji
                entities.append(build_premium_emoji_entity(entry, offset))
            cursor = match.end()
        tail = text[cursor:]
        output_parts.append(tail)
        output_text += tail

    entities = add_catalog_premium_emoji_entities(output_text, catalog, entities)
    entities.sort(key=lambda entity: int(entity.get("offset") or 0))
    return output_text, entities


def add_catalog_premium_emoji_entities(
    text: str,
    catalog: list[PremiumEmojiEntry],
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_emoji: dict[str, PremiumEmojiEntry] = {}
    for entry in catalog:
        if entry.emoji and entry.emoji not in by_emoji:
            by_emoji[entry.emoji] = entry
    if not by_emoji:
        return entities

    covered_ranges: list[tuple[int, int]] = []
    for entity in entities:
        try:
            offset = int(entity.get("offset") or 0)
            length = int(entity.get("length") or 0)
        except (TypeError, ValueError):
            continue
        if length > 0:
            covered_ranges.append((offset, offset + length))

    for emoji, entry in by_emoji.items():
        cursor = 0
        while True:
            index = text.find(emoji, cursor)
            if index < 0:
                break
            offset = utf16_length(text[:index])
            length = utf16_length(emoji)
            end = offset + length
            if not any(offset < covered_end and end > covered_start for covered_start, covered_end in covered_ranges):
                entities.append(build_premium_emoji_entity(entry, offset))
                covered_ranges.append((offset, end))
            cursor = index + len(emoji)
    return entities


def build_premium_emoji_entity(entry: PremiumEmojiEntry, offset: int) -> dict[str, Any]:
    return {
        "type": "custom_emoji",
        "offset": offset,
        "length": utf16_length(entry.emoji),
        "custom_emoji_id": entry.custom_emoji_id,
        "alias": entry.alias,
        "emoji": entry.emoji,
    }


async def prepare_post_custom_emoji_for_publish(
    session: AsyncSession,
    factory: ContentFactory,
    account: TelegramAccount,
    post: ContentPost,
) -> tuple[str, list[dict[str, Any]]]:
    metadata = post.post_metadata if isinstance(post.post_metadata, dict) else {}
    catalog = await content_factory_effective_premium_emoji_catalog(session, factory, account)
    if catalog:
        prepared_text = ensure_premium_emoji_placeholders_for_post(
            post.text,
            catalog,
            footer=_content_factory_required_footer(factory)
            if _content_factory_custom_system_prompt(factory)
            else _content_factory_footer(factory),
        )
        return apply_premium_emoji_placeholders(prepared_text, catalog)
    entities = metadata.get("custom_emoji_entities")
    if metadata.get("custom_emoji_text") == post.text and isinstance(entities, list):
        return post.text, [entity for entity in entities if isinstance(entity, dict)]
    return post.text, []


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _content_factory_footer(factory: ContentFactory) -> str:
    link = _content_factory_primary_link(factory)
    if not link:
        return ""
    label = _content_factory_link_label(factory)
    return f"🤖 **[{label}]({link})**"


def _content_factory_required_footer(factory: ContentFactory) -> str:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    for key in ("required_footer", "footer", "cta_footer", "custom_footer"):
        value = _clean_generated_post_text(str(config.get(key) or ""))
        if value and _has_markdown_or_plain_url(value):
            return value[:360]
    return _extract_required_footer_from_prompt(_content_factory_custom_system_prompt(factory))


def _extract_required_footer_from_prompt(prompt: str) -> str:
    if not prompt:
        return ""
    link_pattern = r"\[[^\]]+\]\(https?://[^)]+\)"
    exact_patterns = [
        rf"(?:footer|футер|cta|финал)[^\n]{{0,120}}(?:ровно|именно|как|такой|формат(?:е)?)[\s:,-]+(?P<footer>[^\n]*{link_pattern}[^\n]*)",
        rf"(?:ровно|именно|как|такой|формат(?:е)?)[\s:,-]+(?P<footer>[^\n]*{link_pattern}[^\n]*)",
    ]
    for pattern in exact_patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            return _clean_extracted_footer(match.group("footer"))

    lines = [line.strip(" \t-*") for line in prompt.splitlines()]
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if not line or not re.search(link_pattern, line):
            continue
        context = " ".join(lines[max(0, index - 3) : index + 1]).lower()
        if any(marker in context for marker in ("footer", "футер", "cta", "финал", "ссылк")):
            return _clean_extracted_footer(line)
    return ""


def _clean_extracted_footer(value: str) -> str:
    footer = _clean_generated_post_text(value)
    footer = re.sub(
        r"^(?:ставь|добавляй|используй|footer|футер|cta|финал)\s*[:,-]?\s*", "", footer, flags=re.IGNORECASE
    )
    link_matches = list(re.finditer(r"(?:\*\*)?\[[^\]]+\]\(https?://[^)]+\)(?:\*\*)?", footer))
    if link_matches:
        footer = footer[: link_matches[-1].end()]
    footer = re.sub(r"[.。]+$", "", footer).strip()
    if len(footer) > 360 or not _has_markdown_or_plain_url(footer):
        return ""
    return footer


def _content_factory_primary_link(factory: ContentFactory) -> str | None:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    candidates = [
        config.get("product_site_url"),
        config.get("platform_url"),
        config.get("cta_url"),
        factory.target_ref,
    ]
    for value in candidates:
        link = _normalize_content_link(value)
        if link:
            return link
    return None


def _content_factory_link_label(factory: ContentFactory) -> str:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    raw = str(config.get("product_name") or config.get("platform_name") or "").strip()
    if raw:
        return raw[:40]
    if factory.target_ref and "t.me/" in factory.target_ref:
        return "Канал"
    return "Платформа"


def _normalize_content_link(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("@") and len(raw) > 1:
        return f"https://t.me/{raw[1:]}"
    if re.match(r"^[A-Za-z0-9_]{3,}$", raw) and "." not in raw:
        return f"https://t.me/{raw}"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return None


def _content_factory_max_length(factory: ContentFactory) -> int:
    config = factory.content_config if isinstance(factory.content_config, dict) else {}
    max_length = max(200, _int_config_value(config.get("max_length"), 1200))
    if not _image_enabled(factory):
        return max_length
    caption_limit = _int_config_value(config.get("telegram_caption_limit"), TELEGRAM_MEDIA_CAPTION_LIMIT_DEFAULT)
    if caption_limit > TELEGRAM_MEDIA_CAPTION_LIMIT_DEFAULT:
        caption_limit = min(caption_limit, TELEGRAM_MEDIA_CAPTION_LIMIT_PREMIUM)
    else:
        caption_limit = TELEGRAM_MEDIA_CAPTION_LIMIT_DEFAULT
    return min(max_length, caption_limit)


def _has_markdown_or_plain_url(text: str) -> bool:
    return bool(re.search(r"\[[^\]]+\]\(https?://[^)]+\)|https?://|t\.me/", text, re.IGNORECASE))


def _trim_generated_post_to_length(text: str, max_length: int) -> str:
    text = text.strip()
    if len(text) <= max_length:
        return text
    limit = max(0, max_length - 1)
    trimmed = text[:limit].rstrip()
    sentence_cut = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"), trimmed.rfind("…"))
    if sentence_cut >= max(120, int(limit * 0.55)):
        trimmed = trimmed[: sentence_cut + 1].rstrip()
    paragraph_cut = trimmed.rfind("\n\n")
    if paragraph_cut >= max(120, int(limit * 0.55)):
        trimmed = trimmed[:paragraph_cut].rstrip()
    return f"{trimmed}…"


def _image_enabled(factory: ContentFactory) -> bool:
    config = factory.image_config if isinstance(factory.image_config, dict) else {}
    return bool(config.get("enabled"))


def _image_knowledge_only(factory: ContentFactory) -> bool:
    config = factory.image_config if isinstance(factory.image_config, dict) else {}
    return bool(config.get("knowledge_only") or config.get("use_knowledge_only") or config.get("knowledge_images_only"))


async def find_duplicate_fingerprint(
    session: AsyncSession, factory_id: str, fingerprint: str
) -> ContentMemoryItem | None:
    result = await session.execute(
        select(ContentMemoryItem)
        .where(ContentMemoryItem.factory_id == factory_id, ContentMemoryItem.text_fingerprint == fingerprint)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_recent_style_match(session: AsyncSession, factory_id: str, text: str) -> ContentMemoryItem | None:
    signature = editorial_style_signature(text)
    if not signature:
        return None
    result = await session.execute(
        select(ContentMemoryItem)
        .where(ContentMemoryItem.factory_id == factory_id)
        .order_by(ContentMemoryItem.created_at.desc())
        .limit(8)
    )
    for item in result.scalars().all():
        snapshot = item.performance_snapshot if isinstance(item.performance_snapshot, dict) else {}
        if snapshot.get("style_signature") == signature:
            return item
    return None


def editorial_style_signature(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    paragraphs = [part.strip() for part in normalized.split("\n\n") if part.strip()]
    first_line = paragraphs[0] if paragraphs else normalized.splitlines()[0]
    lower = normalized.lower()
    numbered_items = len(re.findall(r"(?:^|\n)\s*(?:[1-9][.)]|[1-9]️⃣)", normalized))
    bold_count = normalized.count("**") // 2
    markers = [
        "emoji_title" if re.match(r"^[\U0001f300-\U0001faff]", first_line) else "plain_title",
        "numbered_list" if numbered_items >= 2 else "no_numbered_list",
        "how_block" if "вот как это работает" in lower else "no_how_block",
        "thus_final" if "таким образом" in lower or "в итоге" in lower else "no_thus_final",
        "question" if "?" in first_line[:120] else "statement",
        f"p{min(len(paragraphs), 7)}",
        f"b{min(bold_count, 4)}",
    ]
    return "|".join(markers)


def _starts_with_generic_context(text: str) -> bool:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if len(paragraphs) < 2:
        return False
    intro = paragraphs[1].lower() if len(paragraphs[0]) < 120 else paragraphs[0].lower()
    generic_starts = (
        "когда в telegram",
        "когда команда",
        "когда в telegram-маркетинге",
        "в telegram-автоматизации",
    )
    product_words = ("tg.demo-platform", "платформ", "модул", "экран", "контент-завод", "источник", "черновик")
    return intro.startswith(generic_starts) and not any(word in intro for word in product_words)


async def add_event(
    session: AsyncSession,
    factory_id: str,
    run_id: str | None,
    post_id: str | None,
    level: str,
    event_type: str,
    message: str,
    payload: dict,
) -> None:
    session.add(
        ContentFactoryEvent(
            factory_id=factory_id,
            run_id=run_id,
            post_id=post_id,
            level=level,
            event_type=event_type,
            message=message,
            payload=payload,
            created_at=datetime.now(UTC),
        )
    )


async def factory_summary_counts(session: AsyncSession, factory_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not factory_ids:
        return {}
    now = datetime.now(UTC)
    rows = await session.execute(
        select(ContentPost.factory_id, ContentPost.status, func.count(ContentPost.id))
        .where(ContentPost.factory_id.in_(factory_ids))
        .group_by(ContentPost.factory_id, ContentPost.status)
    )
    counts: dict[str, dict[str, Any]] = {
        factory_id: {"drafts_count": 0, "published_24h": 0, "published_7d": 0, "errors_count": 0}
        for factory_id in factory_ids
    }
    for factory_id, status, count in rows.all():
        if status in {"draft", "needs_review", "approved", "scheduled"}:
            counts[factory_id]["drafts_count"] += count
        if status == "failed":
            counts[factory_id]["errors_count"] += count
    published_rows = await session.execute(
        select(ContentPost.factory_id, func.count(ContentPost.id))
        .where(
            ContentPost.factory_id.in_(factory_ids),
            ContentPost.status == "published",
            ContentPost.published_at >= now - timedelta(days=1),
        )
        .group_by(ContentPost.factory_id)
    )
    for factory_id, count in published_rows.all():
        counts[factory_id]["published_24h"] = count
    published_week_rows = await session.execute(
        select(ContentPost.factory_id, func.count(ContentPost.id))
        .where(
            ContentPost.factory_id.in_(factory_ids),
            ContentPost.status == "published",
            ContentPost.published_at >= now - timedelta(days=7),
        )
        .group_by(ContentPost.factory_id)
    )
    for factory_id, count in published_week_rows.all():
        counts[factory_id]["published_7d"] = count
    success_rows = await session.execute(
        select(ContentFactoryRun.factory_id, func.max(ContentFactoryRun.completed_at))
        .where(ContentFactoryRun.factory_id.in_(factory_ids), ContentFactoryRun.status.in_(["ready", "published"]))
        .group_by(ContentFactoryRun.factory_id)
    )
    for factory_id, completed_at in success_rows.all():
        counts[factory_id]["last_successful_run_at"] = completed_at
    return counts


def text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\wа-яА-ЯёЁ]+", " ", text.lower())).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{", cleaned)
        if not match:
            raise ValueError("CONTENT_FACTORY_AI_JSON_PARSE_FAILED") from None
        try:
            data, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError as exc:
            raise ValueError("CONTENT_FACTORY_AI_JSON_PARSE_FAILED") from exc
    if not isinstance(data, dict):
        raise ValueError("CONTENT_FACTORY_AI_JSON_NOT_OBJECT")
    return data


def content_factory_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def content_factory_publish_days(config: dict) -> set[str]:
    raw_days = config.get("publish_days")
    if isinstance(raw_days, list) and raw_days:
        days = {str(day) for day in raw_days if str(day) in CONTENT_FACTORY_WEEKDAY_VALUES}
        if days:
            return days
    return {"mon", "tue", "wed", "thu", "fri"}


def content_factory_schedule_slot_minutes(config: dict) -> list[int]:
    posts_per_day = max(1, min(CONTENT_FACTORY_MAX_POSTS_PER_DAY, parse_int(config.get("posts_per_day"), 1)))
    start = parse_time_to_minutes(str(config.get("publish_time_from") or "10:00"), 10 * 60)
    end = parse_time_to_minutes(str(config.get("publish_time_to") or "18:00"), 18 * 60)
    if posts_per_day <= 1 or end <= start:
        return [start]
    step = (end - start) / (posts_per_day - 1)
    return sorted({round(start + step * index) for index in range(posts_per_day)})


def parse_time_to_minutes(value: str, fallback: int) -> int:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        return fallback
    hours = max(0, min(23, int(match.group(1))))
    minutes = max(0, min(59, int(match.group(2))))
    return hours * 60 + minutes


def parse_int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def split_source_refs(refs: str | list[str]) -> list[str]:
    if isinstance(refs, str):
        raw_refs = re.split(r"[\s,;]+", refs)
    else:
        raw_refs = []
        for ref in refs:
            raw_refs.extend(re.split(r"[\s,;]+", ref))
    cleaned = []
    seen = set()
    for ref in raw_refs:
        value = ref.strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        cleaned.append(value)
    return cleaned


def normalize_source_ref(ref: str) -> tuple[str | None, str | None, str]:
    value = ref.strip()
    if value.startswith("@"):
        username = clean_username(value[1:])
        return username, None, username or value

    parsed = urlparse(value if "://" in value else f"https://{value}" if value.startswith("t.me/") else value)
    host = (parsed.netloc or "").lower()
    if host in {"t.me", "telegram.me"}:
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts and not path_parts[0].startswith("+") and path_parts[0] != "joinchat":
            username = clean_username(path_parts[0])
            return username, None, username or value
        normalized_url = f"https://{host}{parsed.path}".rstrip("/")
        return None, normalized_url, normalized_url

    if "://" in value:
        normalized_url = value.rstrip("/")
        return None, normalized_url, normalized_url

    username = clean_username(value)
    return username, None, username or value


def clean_username(value: str) -> str | None:
    cleaned = value.strip().lstrip("@").split("?")[0].strip("/")
    if not cleaned:
        return None
    return cleaned


def extract_knowledge_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_KNOWLEDGE_SUFFIXES:
        return ""
    if suffix in {".txt", ".md", ".csv", ".json", ".html", ".xml"}:
        return decode_text_bytes(content)
    if suffix == ".pdf":
        return extract_pdf_text(content)
    if suffix == ".docx":
        return extract_docx_text(content)
    if suffix in {".xlsx", ".xlsm"}:
        return extract_xlsx_text(content)
    return decode_text_bytes(content)


def is_knowledge_image(filename: str, content_type: str | None = None) -> bool:
    suffix = Path(filename).suffix.lower()
    normalized_content_type = (content_type or "").split(";")[0].strip().lower()
    return suffix in IMAGE_KNOWLEDGE_SUFFIXES or normalized_content_type in IMAGE_KNOWLEDGE_CONTENT_TYPES


def guess_knowledge_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
        ".html": "text/html",
        ".xml": "application/xml",
    }.get(suffix, "application/octet-stream")


def knowledge_item_filename(item: ContentFactoryKnowledgeItem) -> str:
    metadata = item.item_metadata if isinstance(item.item_metadata, dict) else {}
    metadata_filename = str(metadata.get("filename") or "").strip()
    path_filename = Path(item.file_path).name if item.file_path else ""
    return Path(metadata_filename or path_filename or item.title or "knowledge-file").name


def knowledge_item_content_type(item: ContentFactoryKnowledgeItem) -> str:
    metadata = item.item_metadata if isinstance(item.item_metadata, dict) else {}
    content_type = str(metadata.get("content_type") or "").split(";", 1)[0].strip().lower()
    return content_type or guess_knowledge_content_type(knowledge_item_filename(item))


def knowledge_file_payload(item: ContentFactoryKnowledgeItem) -> KnowledgeFilePayload | None:
    filename = knowledge_item_filename(item)
    content_type = knowledge_item_content_type(item)
    path = Path(item.file_path) if item.file_path else None
    if path and path.exists() and path.is_file():
        data = path.read_bytes()
    elif item.file_data:
        data = bytes(item.file_data)
    else:
        return None
    return KnowledgeFilePayload(
        filename=filename, content_type=content_type, data=data, suffix=knowledge_image_suffix(filename, content_type)
    )


def knowledge_image_suffix(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_KNOWLEDGE_SUFFIXES:
        return suffix
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".png")


def image_knowledge_description(filename: str, content_type: str | None, size: tuple[int, int] | None) -> str:
    dimensions = f"{size[0]}x{size[1]}" if size else "размер не определён"
    readable_name = Path(filename).stem.replace("-", " ").replace("_", " ")
    return (
        f"Изображение/скриншот: {filename}\n"
        f"Описание по имени файла: {readable_name}\n"
        f"Тип: {content_type or guess_knowledge_content_type(filename)}; размер: {dimensions}.\n"
        "Используй этот материал как визуальный контекст продукта. "
        "Если пост разбирает соответствующий экран, модуль или интерфейс, можно прикрепить этот файл как изображение поста через image_prompt: "
        f"USE_KNOWLEDGE_IMAGE: {filename} | короткая причина выбора."
    )


def image_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) >= 24 and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if len(content) >= 10 and content[:6] in {b"GIF87a", b"GIF89a"}:
        return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if len(content) >= 30 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        if content[12:16] == b"VP8X" and len(content) >= 30:
            width = int.from_bytes(content[24:27], "little") + 1
            height = int.from_bytes(content[27:30], "little") + 1
            return width, height
    if len(content) > 4 and content.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(content):
                break
            segment_length = int.from_bytes(content[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(content[index + 3 : index + 5], "big")
                width = int.from_bytes(content[index + 5 : index + 7], "big")
                return width, height
            index += segment_length
    return None


def decode_text_bytes(content: bytes) -> str:
    for encoding in ["utf-8", "utf-16", "cp1251", "latin-1"]:
        try:
            return content.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return ""


def extract_pdf_text(content: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    parts = [page.extract_text() or "" for page in reader.pages[:80]]
    return "\n\n".join(part.strip() for part in parts if part.strip())


def extract_docx_text(content: bytes) -> str:
    from io import BytesIO

    from docx import Document

    document = Document(BytesIO(content))
    return "\n".join(paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip())


def extract_xlsx_text(content: bytes) -> str:
    from io import BytesIO

    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets[:10]:
        parts.append(f"### {sheet.title}")
        for row in sheet.iter_rows(max_row=300, values_only=True):
            values = [str(value).strip() for value in row if value is not None and str(value).strip()]
            if values:
                parts.append(" | ".join(values))
    return "\n".join(parts)


def safe_upload_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "knowledge.txt"
    return re.sub(r"[^A-Za-z0-9А-Яа-яёЁ._ -]+", "_", name)[:160]


def is_ignored_knowledge_upload(filename: str) -> bool:
    return filename == ".DS_Store" or filename.startswith("._")


def _add_knowledge_item(
    session: AsyncSession, factory_id: str, payload: ContentFactoryKnowledgeCreate
) -> ContentFactoryKnowledgeItem:
    item = ContentFactoryKnowledgeItem(
        factory_id=factory_id,
        title=payload.title,
        kind=payload.kind,
        content_text=payload.content_text,
        file_path=payload.file_path,
        item_metadata=payload.metadata,
        enabled=payload.enabled,
    )
    session.add(item)
    return item


def normalize_site_context_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise ContentFactoryError("CONTENT_FACTORY_SITE_URL_REQUIRED")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContentFactoryError("CONTENT_FACTORY_SITE_URL_INVALID")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ContentFactoryError("CONTENT_FACTORY_SITE_URL_INVALID")
    if hostname in {"localhost", "0.0.0.0"} or hostname.endswith(".local"):
        raise ContentFactoryError("CONTENT_FACTORY_SITE_URL_PRIVATE")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise ContentFactoryError("CONTENT_FACTORY_SITE_URL_PRIVATE")
    normalized_path = parsed.path or "/"
    return (
        urlunparse((parsed.scheme, parsed.netloc.lower(), normalized_path, "", "", "")).rstrip("/")
        or f"{parsed.scheme}://{parsed.netloc.lower()}"
    )


async def fetch_site_context_pages(site_url: str, *, max_pages: int) -> list[SiteContextPage]:
    max_pages = max(1, min(max_pages, 20))
    root = normalize_site_context_url(site_url)
    root_host = urlparse(root).hostname or ""
    crawl_host = root_host
    pages: list[SiteContextPage] = []
    seen: set[str] = set()
    queue = [root]

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": "DemoPlatform-ContentFactory/1.0 (+https://demo.invalid)"},
    ) as client:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                page = await fetch_site_context_page(client, url)
            except (httpx.HTTPError, UnicodeDecodeError):
                continue
            page_host = (urlparse(page.url).hostname or "").lower()
            if not page or not same_site_host(page_host, crawl_host):
                continue
            if not pages:
                crawl_host = page_host
            pages.append(page)
            for link in prioritize_site_links(page.links):
                normalized_link = normalize_crawl_link(page.url, link)
                if normalized_link and normalized_link not in seen and normalized_link not in queue:
                    queue.append(normalized_link)
            queue = prioritize_site_links(queue)[: max_pages * 3]
    return pages


async def fetch_site_context_page(client: httpx.AsyncClient, url: str) -> SiteContextPage | None:
    response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return None
    content = response.content[:MAX_SITE_CONTEXT_BYTES]
    html = content.decode(response.encoding or "utf-8", errors="ignore")
    parser = SiteHtmlTextParser(base_url=str(response.url))
    parser.feed(html)
    text = _clean_multiline_text("\n".join(parser.text_parts))
    if len(text) < 120:
        return None
    return SiteContextPage(
        url=str(response.url).split("#")[0].rstrip("/"),
        title=_clean_text(parser.title or site_context_title(str(response.url))),
        text=text[:8000],
        links=parser.links,
    )


class SiteHtmlTextParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._current_tag = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._current_tag = tag
        if tag in {"script", "style", "noscript", "svg", "canvas", "iframe"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        attrs_dict = {key.lower(): value for key, value in attrs if value}
        if tag == "a" and attrs_dict.get("href"):
            self.links.append(urljoin(self.base_url, attrs_dict["href"]))
        if tag == "meta" and attrs_dict.get("name", "").lower() in {
            "description",
            "og:description",
            "twitter:description",
        }:
            self.text_parts.append(attrs_dict.get("content", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas", "iframe"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        self._current_tag = ""

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or self._skip_depth:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        if self._current_tag in {"h1", "h2", "h3", "p", "li", "strong", "b", "em", "button", "a"} or len(text) > 40:
            self.text_parts.append(text)


def normalize_crawl_link(root_url: str, link: str) -> str | None:
    parsed_root = urlparse(root_url)
    parsed = urlparse(link)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if (parsed.hostname or "").lower() != (parsed_root.hostname or "").lower():
        return None
    if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4|mp3|mov|avi|css|js)$", parsed.path, re.IGNORECASE):
        return None
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", "", "")).rstrip("/")


def same_site_host(left: str, right: str) -> bool:
    left = left.lower().removeprefix("www.")
    right = right.lower().removeprefix("www.")
    return bool(left and right and left == right)


def prioritize_site_links(links: list[str]) -> list[str]:
    keywords = (
        "features",
        "product",
        "platform",
        "pricing",
        "about",
        "docs",
        "faq",
        "cases",
        "use-cases",
        "blog",
        "solutions",
        "tariff",
        "price",
        "vozmozhnosti",
        "produkt",
    )
    unique = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return sorted(
        unique, key=lambda link: (0 if any(keyword in link.lower() for keyword in keywords) else 1, len(link), link)
    )


def format_site_context_pages(site_url: str, pages: list[SiteContextPage]) -> str:
    blocks = [f"Сайт продукта: {site_url}", f"Собрано страниц: {len(pages)}"]
    for page in pages:
        blocks.append(f"## {page.title}\nURL: {page.url}\n{page.text[:5000]}")
    return "\n\n".join(blocks)[:28_000]


async def build_site_context_brief(factory: ContentFactory, site_url: str, extracted_text: str) -> str:
    system_prompt = (
        "Ты продуктовый аналитик для контент-маркетинга. Сожми текст сайта в базу знаний для Telegram-контент-завода. "
        "Не выдумывай факты, сохраняй конкретику продукта, аудитории, функций, ограничений, терминов и офферов."
    )
    user_prompt = "\n\n".join(
        [
            f"Название завода: {factory.name}",
            f"Сайт: {site_url}",
            "Сделай структурированную выжимку на русском:",
            "- что это за продукт;",
            "- для кого;",
            "- ключевые возможности;",
            "- типовые сценарии и боли;",
            "- факты/формулировки, которые можно использовать в постах;",
            "- чего нельзя обещать без подтверждения;",
            "- идеи углов для постов.",
            "Текст сайта:",
            extracted_text,
        ]
    )
    try:
        brief, usage = await generate_text_response(
            system_prompt=build_content_factory_humanized_system_prompt(system_prompt, author_gender="neutral"),
            user_prompt=user_prompt,
            model=get_settings().openai_quality_model,
            temperature=0.2,
            max_output_tokens=1800,
        )
    except AiProviderError:
        return extracted_text[:24_000]
    clean_brief = _clean_multiline_text(brief or "")
    if len(clean_brief) < 80:
        clean_brief = extracted_text[:24_000]
    return (
        f"Динамический контекст сайта: {site_url}\n\n{clean_brief}\n\nAI usage: {json.dumps(usage, ensure_ascii=False)}"
    )


def site_context_title(site_url: str) -> str:
    parsed = urlparse(site_url)
    return (parsed.hostname or parsed.netloc or site_url).removeprefix("www.")


def _clean_multiline_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.replace("\r", "\n").split("\n")]
    cleaned = [line for line in lines if line and not _looks_sensitive(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def _default_rubrics() -> list[ContentFactoryRubricCreate]:
    return [
        ContentFactoryRubricCreate(
            name="Новость",
            description="Короткий пост по свежему инфоповоду",
            target_share_percent=25,
            format_preset="news",
        ),
        ContentFactoryRubricCreate(
            name="Мнение", description="Редакционный угол и вывод", target_share_percent=25, format_preset="opinion"
        ),
        ContentFactoryRubricCreate(
            name="Инструкция", description="Пошаговый полезный пост", target_share_percent=25, format_preset="guide"
        ),
        ContentFactoryRubricCreate(
            name="Дайджест",
            description="Подборка нескольких наблюдений",
            target_share_percent=25,
            format_preset="digest",
        ),
    ]


def _looks_sensitive(line: str) -> bool:
    lowered = line.lower()
    return any(
        marker in lowered
        for marker in ["token=", "secret", "password", "encrypted_session_blob", "bearer", "api_hash", ".env"]
    )


def _image_artifact_path(factory_id: str, post_id: str) -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "artifacts" / "content-factory-images" / factory_id / f"{post_id}.jpg"


def knowledge_artifact_path(factory_id: str, filename: str) -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "artifacts" / "content-factory-knowledge" / factory_id / filename


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_generated_post_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\\n" in text or "\\r" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = re.sub(r"^```(?:json|markdown|md)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if "\n\n" not in text and len(text) > 520:
        text = _split_plain_text_into_paragraphs(text)
    return text


def _split_plain_text_into_paragraphs(text: str) -> str:
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    paragraphs: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and current_len + len(sentence) > 420:
            paragraphs.append(" ".join(current))
            current = [sentence]
            current_len = len(sentence)
        else:
            current.append(sentence)
            current_len += len(sentence) + 1
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _image_prompt_for_factory(factory: ContentFactory, ai_payload: dict, text: str) -> str:
    ai_prompt = _optional_str(ai_payload.get("image_prompt"))
    if ai_prompt and requested_knowledge_image_name(ai_prompt):
        return ai_prompt
    configured_prompt = _configured_image_prompt(factory)
    if configured_prompt:
        return _compose_configured_image_prompt(factory, configured_prompt, ai_payload, text, ai_prompt)
    return ai_prompt or _image_prompt_fallback_for_factory(factory, ai_payload, text)


def _configured_image_prompt(factory: ContentFactory) -> str:
    config = factory.image_config if isinstance(factory.image_config, dict) else {}
    for key in ("prompt", "image_prompt", "visual_prompt"):
        value = str(config.get(key) or "").strip()
        if value:
            return value[:1600]
    return ""


def _compose_configured_image_prompt(
    factory: ContentFactory,
    configured_prompt: str,
    ai_payload: dict,
    text: str,
    ai_prompt: str | None,
) -> str:
    title = str(ai_payload.get("title") or factory.name or "Telegram post").strip()
    post_context = _clean_text(text)[:700]
    ai_note = f" AI image note for this post: {ai_prompt[:450]}." if ai_prompt else ""
    return (
        f"User visual brief: {configured_prompt}. "
        f"Post title: {title}. Post context: {post_context}.{ai_note} "
        "Create one distinctive image for this exact post. Follow the user visual brief first, adapt it to the post context, "
        "avoid repeating generic Telegram operations dashboards, abstract blue holograms, floating UI panels and network-node visuals. "
        "No text, no letters, no logos."
    )[:1800]


def _image_prompt_fallback_for_factory(factory: ContentFactory, ai_payload: dict, text: str) -> str:
    custom_system_prompt = _content_factory_custom_system_prompt(factory)
    if not custom_system_prompt:
        return _fallback_image_prompt(factory, ai_payload, text)
    title = str(ai_payload.get("title") or factory.name or "Telegram post").strip()
    post_context = _clean_text(text)[:700]
    return (
        "Create a Telegram post cover image strictly following the user's system prompt. "
        f"User system prompt: {custom_system_prompt[:700]}. "
        f"Post title: {title}. Post context: {post_context}. "
        "Respect the user's visual direction above. No text, no letters, no logos, no UI screenshots unless the user explicitly requested screenshots."
    )[:1200]


def _fallback_image_prompt(factory: ContentFactory, ai_payload: dict, text: str) -> str:
    config = factory.image_config if isinstance(factory.image_config, dict) else {}
    title = str(ai_payload.get("title") or factory.name or "Telegram marketing automation").strip()
    style = str(config.get("style") or "premium Telegram marketing editorial cover").strip()
    configured_prompt = _configured_image_prompt(factory)
    post_context = _clean_text(text)[:700]
    scene_hint = _image_scene_hint(title, post_context)
    return (
        "Avoid generic blue holographic dashboards, abstract network nodes, floating UI panels, "
        "interface wireframes, repeated data streams, tiny app screens and sci-fi control rooms. "
        f"{configured_prompt or DEFAULT_IMAGE_PROMPT}. Visual style: {style}. Topic: {title}. Context: {post_context}. Scene: {scene_hint}. "
        "Create a distinctive editorial cover for this exact Telegram automation post. "
        "Use one clear human-scale metaphor, natural composition, strong foreground subject, "
        "premium lighting and realistic depth. No text, no letters, no logos, no UI screenshots."
    )[:1200]


def _image_scene_hint(title: str, context: str) -> str:
    lowered = f"{title} {context}".lower()
    if "draft" in lowered or "чернов" in lowered:
        return "a Telegram post draft on a clean editorial desk, red pencil marks, approval stamp, publication arrow waiting"
    if "статус" in lowered or "status" in lowered:
        return "a team sorting Telegram account status cards on a wall board, clear ready pause warning zones"
    if "источник" in lowered or "source" in lowered:
        return "news sources flowing into an editor desk and then into one polished Telegram channel card"
    if "распис" in lowered or "schedule" in lowered:
        return "calendar slots and Telegram post cards arranged on a planning table with morning and evening light"
    if "ошиб" in lowered or "риск" in lowered or "risk" in lowered:
        return "a careful preflight checklist catching a Telegram campaign error before launch"
    if "контент-завод" in lowered or "content factory" in lowered:
        return "an editorial production line turning source notes into polished Telegram posts, warm studio light"
    return (
        "a concrete editorial workspace where Telegram marketing work moves from raw notes to a polished channel post"
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text
