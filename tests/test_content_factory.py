import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from app.modules.telegram_gateway.service import TelegramGatewayError
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.import_models  # noqa: F401
from app.db.base import Base
from app.modules.ai.service import AiProviderError
from app.modules.auth.models import User
from app.modules.content_factory.api import get_content_post_image
from app.modules.content_factory.models import ContentFactoryKnowledgeItem, ContentMemoryItem, ContentPost
from app.modules.content_factory.schemas import (
    ContentFactoryCreate,
    ContentFactoryKnowledgeCreate,
    ContentFactoryUpdate,
)
from app.modules.content_factory.service import (
    ContentFactoryError,
    SiteContextPage,
    _fallback_image_prompt,
    _image_prompt_for_factory,
    add_knowledge_files,
    apply_content_factory_post_format,
    approve_post,
    build_content_factory_slot_brief,
    build_content_factory_slot_brief_for_run,
    collect_site_context,
    content_factory_next_run_at,
    create_factory,
    ensure_aware_utc,
    generate_image_for_post,
    generate_post_payload,
    image_retry_delay_seconds,
    normalize_content_factory_brand_text,
    normalize_content_factory_quality_notes,
    parse_json_object,
    publish_post,
    quality_check,
    regenerate_text,
    run_factory,
    select_knowledge_image_for_post,
    tick_due_content_factories,
    update_factory,
)
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramSendResult
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


async def _workspace(session: AsyncSession) -> tuple[User, Workspace]:
    user = User(email="owner@example.com", password_hash="test")
    session.add(user)
    await session.flush()
    workspace = Workspace(owner_user_id=user.id, name="Test", slug=f"test-{user.id[:8]}")
    session.add(workspace)
    await session.flush()
    return user, workspace


async def _source_with_message(session: AsyncSession, workspace: Workspace) -> TelegramSource:
    source = TelegramSource(
        workspace_id=workspace.id,
        type="channel",
        username="ai_news",
        title="AI News",
        access_status="available",
    )
    session.add(source)
    await session.flush()
    session.add(
        TelegramMessage(
            workspace_id=workspace.id,
            source_id=source.id,
            tg_message_id="101",
            text=(
                "OpenAI выпустила новую функцию для рабочих команд. "
                "Маркетологам важно тестировать автоматизацию аккуратно, с лимитами и ручной проверкой."
            ),
            published_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return source


@pytest.mark.asyncio
async def test_content_factory_creates_default_rubrics(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)

    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Канал продукта"),
    )

    assert factory.mode == "approval_required"
    assert factory.content_config["config_revision"] == 1
    assert factory.image_config == {}


@pytest.mark.asyncio
async def test_content_factory_clamps_posts_per_day_to_ten(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)

    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Heavy schedule", schedule_config={"posts_per_day": 50}),
    )

    assert factory.schedule_config["posts_per_day"] == 10


@pytest.mark.asyncio
async def test_content_factory_limits_workspace_daily_generation_to_ten(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Daily cap"),
    )
    other_factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Other daily cap"),
    )
    now = datetime.now(UTC)
    posts = [
        ContentPost(
            factory_id=factory.id if index < 6 else other_factory.id,
            status="needs_review",
            title=f"Post {index}",
            text="Generated post",
            created_at=now - timedelta(minutes=index),
        )
        for index in range(10)
    ]
    session.add_all(posts)
    await session.commit()

    with pytest.raises(ContentFactoryError, match="CONTENT_FACTORY_DAILY_GENERATION_LIMIT_REACHED"):
        await run_factory(session, factory)


@pytest.mark.asyncio
async def test_content_factory_bumps_config_revision_on_settings_update(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Revisioned", content_config={"prompt": "old"}),
    )

    updated = await update_factory(
        session,
        factory,
        ContentFactoryUpdate(
            content_config={
                **factory.content_config,
                "prompt": "new",
                "content_plan": "Понедельник — интро",
            }
        ),
    )

    assert updated.content_config["config_revision"] == 2
    assert updated.content_config["content_plan"] == "Понедельник — интро"


def test_content_factory_json_parser_uses_first_complete_object() -> None:
    payload = parse_json_object('{"title": "Первый", "text": "ok"}\n{"title": "Второй"}')

    assert payload == {"title": "Первый", "text": "ok"}


def test_content_factory_json_parser_ignores_text_around_object() -> None:
    payload = parse_json_object('Вот JSON:\n{"title": "Черновик", "text": "готово"}\nГотово.')

    assert payload == {"title": "Черновик", "text": "готово"}


@pytest.mark.asyncio
async def test_content_factory_limits_photo_posts_to_telegram_caption_limit(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)

    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Photo posts",
            image_config={"enabled": True},
            content_config={"min_length": 700, "max_length": 5000},
        ),
    )

    assert factory.content_config["max_length"] == 1000
    assert factory.content_config["telegram_caption_limit"] == 1000
    assert factory.content_config["telegram_caption_limit_source"] == "default"


@pytest.mark.asyncio
async def test_content_factory_uses_premium_caption_limit_for_publisher(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        import_type="session",
        username="premium_publisher",
        daily_limits={"profile": {"is_premium": True}},
    )
    session.add(account)
    await session.flush()

    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Premium photo posts",
            publisher_account_id=account.id,
            image_config={"enabled": True},
            content_config={"min_length": 1700, "max_length": 5000},
        ),
    )

    assert factory.content_config["min_length"] == 1700
    assert factory.content_config["max_length"] == 2000
    assert factory.content_config["telegram_caption_limit"] == 2000
    assert factory.content_config["telegram_caption_limit_source"] == "premium"


@pytest.mark.asyncio
async def test_content_factory_update_clamps_existing_photo_factory(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Text only", content_config={"min_length": 700, "max_length": 5000}),
    )

    updated = await update_factory(
        session,
        factory,
        ContentFactoryUpdate(image_config={"enabled": True}, content_config={"min_length": 1800, "max_length": 5000}),
    )

    assert updated.content_config["min_length"] == 1000
    assert updated.content_config["max_length"] == 1000


@pytest.mark.asyncio
async def test_content_factory_post_format_adds_product_footer(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Product channel",
            content_config={"product_site_url": "https://demo.invalid", "max_length": 1000},
        ),
    )

    text = apply_content_factory_post_format(
        factory,
        "**Контент-завод без хаоса**\n\nТеперь можно управлять источниками, черновиками и расписанием в одном месте.",
    )

    assert "[Платформа](https://demo.invalid)" in text
    assert len(text) <= 1000


@pytest.mark.asyncio
async def test_content_factory_post_format_keeps_footer_inside_caption_limit(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Photo product channel",
            image_config={"enabled": True},
            content_config={"product_site_url": "https://demo.invalid", "max_length": 4000},
        ),
    )

    text = apply_content_factory_post_format(factory, "**Большой пост**\n\n" + "Подробный разбор. " * 200)

    assert "[Платформа](https://demo.invalid)" in text
    assert text.endswith("🤖 **[Платформа](https://demo.invalid)**")
    assert len(text) <= 1000


@pytest.mark.asyncio
async def test_content_factory_post_format_unescapes_literal_newlines(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Escaped newline channel",
            content_config={
                "prompt": (
                    "В конце всегда ставь footer ровно: "
                    "[Платформа](https://demo.invalid) | [Канал](https://t.me/example_channel)"
                ),
                "product_site_url": "https://demo.invalid",
                "max_length": 1000,
            },
        ),
    )

    text = apply_content_factory_post_format(
        factory,
        (
            "Первый абзац.\\n\\nВторой абзац.\\n\\n"
            "[Платформа](https://demo.invalid) | [Канал](https://t.me/example_channel)"
        ),
    )

    assert "\\n" not in text
    assert "Первый абзац.\n\nВторой абзац." in text


@pytest.mark.asyncio
async def test_custom_prompt_exact_footer_beats_platform_fallback(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    exact_footer = "🫆 [Платформа](https://demo.invalid/nc/01) | [Канал](https://t.me/example_channel)"
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Exact footer",
            content_config={
                "prompt": f"Пиши посты о платформе. В конце всегда ставь footer ровно: {exact_footer}",
                "product_site_url": "https://demo.invalid",
                "max_length": 1200,
            },
        ),
    )

    text = apply_content_factory_post_format(factory, "Пост без финальной ссылки.")

    assert text.endswith(exact_footer)
    assert "🤖 **[Платформа](https://demo.invalid)**" not in text


@pytest.mark.asyncio
async def test_content_factory_normalizes_legacy_brand_variants(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Канал про платформу demo-platform",
            content_config={"product_site_url": "https://demo.invalid", "prompt": "Пиши про demo-platform."},
        ),
    )

    text = normalize_content_factory_brand_text(
        factory,
        "В [Demo Platform](https://demo.invalid) и DemoPlatform это называется единым процессом.",
    )

    assert "[demo-platform](https://demo.invalid)" in text
    assert "demo-platform это называется" in text
    assert "Demo Platform" not in text
    assert "DemoPlatform" not in text


def test_content_factory_localizes_ai_quality_notes() -> None:
    notes = normalize_content_factory_quality_notes(
        [
            "Sunday digest tone adapted to available knowledge base and recent history.",
            "Kept within Telegram length budget and included required footer.",
        ]
    )

    assert notes == [
        "Тон дайджеста адаптирован под базу знаний и историю постов.",
        "Пост уложен в лимит Telegram и содержит обязательный footer.",
    ]


@pytest.mark.asyncio
async def test_content_factory_fallback_image_prompt_uses_specific_scene(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Image style", image_config={"enabled": True}),
    )

    prompt = _fallback_image_prompt(
        factory,
        {"title": "Почему draft-first в Telegram спокойнее"},
        "Черновик до публикации",
    )

    assert "post draft" in prompt
    assert "generic blue holographic dashboards" in prompt
    assert "floating UI panels" in prompt


@pytest.mark.asyncio
async def test_content_factory_image_prompt_uses_configured_visual_brief(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Image prompt",
            image_config={
                "enabled": True,
                "prompt": "Use warm documentary product photography with a founder reviewing Telegram content cards.",
            },
        ),
    )

    prompt = _image_prompt_for_factory(
        factory,
        {"title": "Интро demo-platform", "image_prompt": "generic dashboard"},
        "Пост рассказывает, зачем платформа нужна команде.",
    )

    assert "warm documentary product photography" in prompt
    assert "generic dashboard" in prompt
    assert "abstract blue holograms" in prompt


@pytest.mark.asyncio
async def test_content_factory_image_prompt_preserves_requested_knowledge_image(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Knowledge image",
            image_config={"enabled": True, "prompt": "Use editorial photos if no screenshot is needed."},
        ),
    )

    prompt = _image_prompt_for_factory(
        factory,
        {
            "title": "Экран источников",
            "image_prompt": "USE_KNOWLEDGE_IMAGE: sources.png | показываем список источников",
        },
        "Пост разбирает экран источников.",
    )

    assert prompt == "USE_KNOWLEDGE_IMAGE: sources.png | показываем список источников"


@pytest.mark.asyncio
async def test_content_factory_uploads_knowledge_file(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Knowledge files"),
    )
    upload = UploadFile(
        filename="brief.txt",
        file=BytesIO(b"Tone of voice: calm and useful"),
    )

    items = await add_knowledge_files(session, factory, [upload])

    assert len(items) == 1
    assert items[0].title == "brief.txt"
    assert "calm and useful" in (items[0].content_text or "")
    assert items[0].file_path and items[0].file_path.endswith("brief.txt")


@pytest.mark.asyncio
async def test_content_factory_uploads_png_screenshots_as_knowledge_images(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Screenshot knowledge"),
    )
    png_header = (
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big") + b"\x00" * 16
    )
    uploads = [
        UploadFile(filename=f"{index:02d}-platform-screen.png", file=BytesIO(png_header)) for index in range(1, 16)
    ]

    items = await add_knowledge_files(session, factory, uploads)

    assert len(items) == 15
    assert all(item.kind == "image" for item in items)
    assert all("USE_KNOWLEDGE_IMAGE" in (item.content_text or "") for item in items)
    assert items[0].item_metadata["content_type"] == "image/png"
    assert items[0].item_metadata["width"] == 1920
    assert items[0].item_metadata["height"] == 1080


@pytest.mark.asyncio
async def test_content_factory_ignores_macos_resource_fork_uploads(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Clean screenshot knowledge"),
    )
    png_header = (
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big") + b"\x00" * 16
    )
    uploads = [
        UploadFile(filename="01-platform-screen.png", file=BytesIO(png_header)),
        UploadFile(filename="._01-platform-screen.png", file=BytesIO(b"AppleDouble metadata")),
    ]

    items = await add_knowledge_files(session, factory, uploads)

    assert len(items) == 1
    assert items[0].title == "01-platform-screen.png"
    assert items[0].kind == "image"


@pytest.mark.asyncio
async def test_content_factory_collects_product_site_context(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Product context",
            content_config={"product_site_url": "https://example.com"},
        ),
    )

    async def fake_fetch_site_context_pages(site_url: str, *, max_pages: int) -> list[SiteContextPage]:
        assert site_url == "https://example.com"
        assert max_pages == 3
        return [
            SiteContextPage(
                url="https://example.com",
                title="Example Platform",
                text="Example Platform helps marketers manage Telegram campaigns, sources, drafts and publishing.",
                links=[],
            )
        ]

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            "Продукт: платформа для Telegram-маркетинга.\n\nВозможности: источники, черновики, публикации.",
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.fetch_site_context_pages", fake_fetch_site_context_pages)
    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    item = await collect_site_context(session, factory, max_pages=3)

    assert item.kind == "dynamic_site_context"
    assert "платформа для Telegram-маркетинга" in (item.content_text or "")
    assert item.item_metadata["source_url"] == "https://example.com"
    assert factory.content_config["site_context_page_count"] == 1
    stored_items = list((await session.execute(select(ContentFactoryKnowledgeItem))).scalars().all())
    assert len(stored_items) == 1


@pytest.mark.asyncio
async def test_content_factory_manual_source_refs_create_sources(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)

    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Manual sources",
            manual_source_refs=["@openai", "https://t.me/telegram", "@openai"],
        ),
    )

    sources = list((await session.execute(select(TelegramSource))).scalars().all())
    source_usernames = {source.username for source in sources}

    assert factory.name == "Manual sources"
    assert source_usernames == {"openai", "telegram"}
    assert all(source.source_metadata["created_by"] == "content_factory" for source in sources)


@pytest.mark.asyncio
async def test_run_factory_generates_draft_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    source = await _source_with_message(session, workspace)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="AI marketing",
            source_ids=[source.id],
            knowledge_items=[
                ContentFactoryKnowledgeCreate(
                    title="Tone",
                    content_text="Пиши живо, без обещаний отсутствия банов.",
                )
            ],
        ),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Безопасный запуск",
                    "text": (
                        "Автоматизация Telegram начинается с черновиков и лимитов. "
                        "Это снижает риск ошибок, но не гарантирует отсутствие ограничений."
                    ),
                    "image_prompt": None,
                    "source_refs": ["ai_news"],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    run, post = await run_factory(session, factory)

    assert run.status == "ready"
    assert post is not None
    assert post.status == "needs_review"
    assert post.quality_score and post.quality_score >= 55
    assert "черновиков" in post.text
    memory = list((await session.execute(select(ContentMemoryItem))).scalars().all())
    assert len(memory) == 1
    assert memory[0].text_fingerprint == post.post_metadata["text_fingerprint"]
    assert memory[0].performance_snapshot["style_signature"]


@pytest.mark.asyncio
async def test_run_factory_applies_humanizer_to_generated_post(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Humanized factory",
            content_config={
                "goal": "Пиши о продукте живо.",
                "product_site_url": "https://demo.invalid",
                "min_length": 80,
                "max_length": 1000,
            },
            quality_config={"min_quality_score": 1},
        ),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Humanizer",
                    "text": (
                        "«Порядок» в Telegram — это не красивая теория.\n\n"
                        "Таким образом, команда видит, где «готово», а где ещё нужна проверка."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    _run, post = await run_factory(session, factory)

    assert post is not None
    assert "\n\n" in post.text
    assert "—" not in post.text
    assert "–" not in post.text
    assert "«" not in post.text
    assert "»" not in post.text
    assert "Таким образом" not in post.text
    assert "[Платформа](https://demo.invalid)" in post.text


@pytest.mark.asyncio
async def test_generate_post_prompt_allows_flexible_formats(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Product channel",
            image_config={"enabled": True},
            content_config={
                "goal": "Рассказывай про модули платформы простым языком",
                "product_site_url": "https://demo.invalid",
                "max_length": 4000,
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Разбор модуля",
                    "text": (
                        "Сегодня разберём модуль источников tg.demo-platform.\n\n"
                        "Он помогает собрать каналы, из которых контент-завод берёт фактуру для постов."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="manual",
        now_utc=datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
    )
    await generate_post_payload(factory, {"summary": ""}, "База знаний про продукт", slot_brief)

    assert "Обязательная структура" not in captured["user_prompt"]
    assert "Humanizer RU" in captured["system_prompt"]
    assert "humanizer-ru-full-ru-text-self-install-v5-2026-06-12" in captured["system_prompt"]
    assert "no em dash or en dash punctuation in final text" in captured["system_prompt"]
    assert "Обязательная продуктовая привязка" in captured["user_prompt"]
    assert "модуль продукта: Контент-завод" in captured["user_prompt"]
    assert "USE_KNOWLEDGE_IMAGE" in captured["user_prompt"]
    assert "нельзя повторять один и тот же скелет" in captured["user_prompt"].lower()
    assert "поле text целиком не длиннее 1000 символов" in captured["user_prompt"]
    assert "Длина смысловой части до footer" in captured["user_prompt"]
    assert "Обязательный footer ровно такой: 🤖 **[Платформа](https://demo.invalid)**" in captured["user_prompt"]
    assert "не рассчитывай на обрезку после генерации" in captured["user_prompt"]
    assert "отдельный промпт изображения" in captured["user_prompt"].lower()
    assert "конкретную editorial cover-сцену" not in captured["user_prompt"]
    assert "futuristic dashboard" not in captured["user_prompt"]
    assert "floating UI panels" not in captured["user_prompt"]
    assert "UI screenshots" not in captured["user_prompt"]
    assert "Разбор модуля" in captured["system_prompt"]
    assert "Пользовательская ситуация" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_manual_topic_takes_priority_over_scheduled_slot_prompt(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Manual topic",
            content_config={
                "prompt": "Пиши по контент-плану.",
                "content_plan": "Понедельник — только прогрев аккаунтов",
                "max_length": 1200,
            },
            schedule_config={"posts_per_day": 5, "timezone": "Europe/Moscow"},
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Знакомство с платформой",
                    "text": "**Знакомство с платформой**\n\nПост про платформу.",
                    "image_prompt": None,
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="manual_topic",
        now_utc=datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
    )
    topic_prompt = "Сделай вводный пост-знакомство с платформой Demo Platform. Структура: сильный короткий заголовок."
    await generate_post_payload(factory, {"summary": ""}, "База знаний", slot_brief, topic_prompt=topic_prompt)

    assert "Ручное задание пользователя главнее контент-плана" in captured["system_prompt"]
    assert captured["user_prompt"].index("ГЛАВНОЕ ЗАДАНИЕ") < captured["user_prompt"].index("Фоновый редакционный слот")
    assert "Это задание важнее контент-плана, подтемы и слота дня" in captured["user_prompt"]
    assert "не подменяй его подтемой слота" in captured["user_prompt"]


@pytest.mark.asyncio
async def test_generic_traffic_content_plan_does_not_force_content_factory_focus(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Traffic channel",
            content_config={
                "content_plan": (
                    "Четверг — контент и доверие: посты, tone of voice, CTA, "
                    "как контент помогает готовить лидов в Telegram-трафике"
                ),
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Контент как часть трафика",
                    "text": "Пост про Telegram-трафик.",
                    "image_prompt": None,
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="schedule",
        now_utc=datetime(2026, 6, 18, 5, 0, tzinfo=UTC),
    )
    await generate_post_payload(factory, {"summary": ""}, "Справка продукта без явного модуля", slot_brief)

    assert "модуль продукта: Telegram-трафик и лидогенерация" in captured["user_prompt"]
    assert "модуль продукта: Контент-завод" not in captured["user_prompt"]


@pytest.mark.asyncio
async def test_persona_diary_factory_disables_product_grounding(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Example diary",
            target_ref="@example_channel",
            content_config={
                "content_type": "persona_diary",
                "product_grounding_enabled": False,
                "prompt": "Пиши короткие живые подписи от лица Sample Creator.",
                "max_length": 260,
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Example",
                    "text": (
                        "Сегодня я прям влюбилась в этот холодный свет🥹\n\n"
                        "Иногда фото получается лучше настроения, и это даже немного спасает"
                    ),
                    "image_prompt": "USE_KNOWLEDGE_IMAGE: sample_asset_01.png | winter diary",
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="schedule",
        now_utc=datetime(2026, 6, 18, 9, 0, tzinfo=UTC),
    )
    await generate_post_payload(
        factory,
        {"summary": ""},
        "Изображение/скриншот: sample_asset_01.png\nОписание: Sample Creator winter diary.",
        slot_brief,
    )
    score, notes = quality_check(
        factory,
        "Сегодня я прям влюбилась в этот холодный свет🥹\n\n"
        "Иногда фото получается лучше настроения, и это даже немного спасает",
        duplicate=None,
    )

    assert "Режим визуального persona/lifestyle-канала" in captured["user_prompt"]
    assert "короткую живую подпись к фото" in captured["user_prompt"]
    assert "как в личном Telegram-блоге" in captured["user_prompt"]
    assert "не пиши полотна" in captured["user_prompt"]
    assert "модуль продукта:" not in captured["user_prompt"]
    assert "product_module" not in captured["user_prompt"]
    assert "короткий CTA" not in captured["user_prompt"]
    assert "Telegram-трафик и лидогенерация" not in captured["user_prompt"]
    assert "demo-platform" not in captured["user_prompt"].lower()
    assert score >= 40
    assert "Нет ссылки на продукт" not in notes
    assert "Нет нормального разделения на абзацы" not in notes
    assert not any("конкретному модулю" in note for note in notes)

    factory.quality_config = {"hard_stop_words": ["лид"]}
    blocked_score, blocked_notes = quality_check(factory, "красивый пост, но лид уже остыл", duplicate=None)

    assert blocked_score == 0
    assert any("запрещённая тема" in note for note in blocked_notes)


@pytest.mark.asyncio
async def test_manual_intro_post_gets_heading_when_model_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Manual intro",
            content_config={"prompt": "Пиши живо.", "min_length": 80, "max_length": 1000},
            quality_config={"min_quality_score": 1},
        ),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Demo Platform без ручного хаоса",
                    "text": (
                        "Telegram-маркетинг часто ломается не на креативе, а на операционке.\n\n"
                        "Платформа собирает аккаунты, источники, прокси, кампании, "
                        "комментарии, черновики и публикации в один процесс.\n\n"
                        "Сначала контроль и наблюдаемость, потом масштабирование."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    _run, post = await run_factory(
        session,
        factory,
        topic_prompt="Сделай вводный пост-знакомство. Структура: сильный короткий заголовок.",
    )

    assert post is not None
    assert post.text.startswith("**Demo Platform без ручного хаоса**\n\n")


@pytest.mark.asyncio
async def test_run_factory_rewrites_overlong_custom_prompt_post_without_losing_footer(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Custom footer",
            content_config={
                "prompt": "Пиши посты о платформе. В конце всегда ставь footer со ссылкой [Платформа](https://demo.invalid/nc/01).",
                "min_length": 100,
                "max_length": 360,
            },
        ),
    )
    calls: list[str] = []

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        calls.append(user_prompt)
        if "Исходный пост" in user_prompt:
            return (
                json.dumps(
                    {
                        "text": (
                            "**Зачем нужен demo-platform**\n\n"
                            "Платформа собирает Telegram-операционку в один контур: "
                            "аккаунты, источники, черновики, расписание и статусы.\n\n"
                            "Так команда видит не «кажется готово», а реальное состояние процесса.\n\n"
                            "[Платформа](https://demo.invalid/nc/01)"
                        )
                    }
                ),
                {},
            )
        return (
            json.dumps(
                {
                    "title": "Long",
                    "text": (
                        "**Зачем нужен demo-platform**\n\n"
                        + (
                            "Telegram-маркетинг часто разваливается на отдельные таблицы, "
                            "чаты, аккаунты и ручные проверки. "
                        )
                        * 12
                        + "\n\n[Платформа](https://demo.invalid/nc/01)"
                    ),
                    "image_prompt": None,
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    run, post = await run_factory(session, factory)

    assert run.status == "ready"
    assert post is not None
    assert len(post.text) <= 360
    assert post.text.endswith("[Платформа](https://demo.invalid/nc/01)")
    assert not post.text.endswith("…")
    assert any("Сожми пост редакционно" in call for call in calls)


@pytest.mark.asyncio
async def test_generate_post_prompt_respects_custom_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    custom_prompt = (
        "Пиши посты строго по недельному контент-плану. "
        "CTA и footer ставь только как **[Навигация](https://demo.invalid/nc/01)**. "
        "Изображения описывай как реальные редакционные сцены, без абстрактных панелей."
    )
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Custom prompt channel",
            image_config={"enabled": True},
            content_config={
                "prompt": custom_prompt,
                "product_site_url": "https://demo.invalid",
                "max_length": 4000,
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Custom",
                    "text": "**Custom**\n\nТекст по пользовательскому промпту.",
                    "image_prompt": "real editorial scene",
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="manual",
        now_utc=datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
    )
    await generate_post_payload(factory, {"summary": ""}, "База знаний", slot_brief)
    formatted = apply_content_factory_post_format(factory, "**Custom**\n\nТекст без footer.")

    assert custom_prompt in captured["system_prompt"]
    assert "Пользовательский системный промпт главнее" in captured["user_prompt"]
    assert "futuristic dashboard" not in captured["user_prompt"]
    assert "Обязательная продуктовая привязка" in captured["user_prompt"]
    assert "Обязательный footer ровно такой: **[Навигация](https://demo.invalid/nc/01)**" in captured["user_prompt"]
    assert formatted.endswith("**[Навигация](https://demo.invalid/nc/01)**")
    assert "🤖 **[Платформа](https://demo.invalid)**" not in formatted


@pytest.mark.asyncio
async def test_custom_prompt_footer_preserves_existing_custom_footer(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Custom footer",
            content_config={
                "prompt": "Ставь свой footer.",
                "product_site_url": "https://demo.invalid",
                "max_length": 1000,
            },
        ),
    )

    formatted = apply_content_factory_post_format(
        factory,
        "**Пост**\n\nТекст.\n\n[Навигация](https://demo.invalid/nc/01)",
    )

    assert formatted.endswith("[Навигация](https://demo.invalid/nc/01)")
    assert "🤖 **[Платформа](https://demo.invalid)**" not in formatted


@pytest.mark.asyncio
async def test_manual_topic_prompt_guides_single_content_factory_run(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Topic channel",
            content_config={
                "prompt": "Пиши посты про платформу простым экспертным языком.",
                "max_length": 1200,
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Интро платформы",
                    "text": (
                        "**Интро платформы**\n\n"
                        "Сегодня коротко разбираем, зачем продукт нужен команде.\n\n"
                        "Он помогает собрать процесс в одном рабочем контуре."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    run, post = await run_factory(
        session,
        factory,
        topic_prompt="Рассказать интро про нашу платформу и ключевые преимущества",
    )

    assert post is not None
    assert run.trigger_type == "manual_topic"
    assert post.post_metadata["manual_topic_prompt"] == "Рассказать интро про нашу платформу и ключевые преимущества"
    assert run.metrics["topic_prompt"] == "Рассказать интро про нашу платформу и ключевые преимущества"
    assert "ГЛАВНОЕ ЗАДАНИЕ ДЛЯ ЭТОГО РУЧНОГО ПОСТА" in captured["user_prompt"]
    assert "Рассказать интро про нашу платформу" in captured["user_prompt"]
    assert "Это задание важнее контент-плана, подтемы и слота дня" in captured["user_prompt"]


@pytest.mark.asyncio
async def test_regenerate_text_updates_existing_post_without_auto_publish(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Regenerate current post",
            target_ref="@target",
            publisher_account_id=account.id,
            mode="auto_publish",
            content_config={"max_length": 1200},
            quality_config={"min_quality_score": 0},
        ),
    )
    post = ContentPost(factory_id=factory.id, status="approved", title="Old", text="Старый текст")
    session.add(post)
    await session.commit()
    await session.refresh(post)

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Новый текст",
                    "text": "**Новый текст**\n\nПерегенерированная версия текущего поста.",
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    async def fail_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        raise AssertionError("text regeneration must not publish to Telegram")

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)
    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        fail_send_channel_post_with_account,
    )

    updated = await regenerate_text(session, factory, post)
    rows = list((await session.execute(select(ContentPost))).scalars().all())

    assert updated.id == post.id
    assert len(rows) == 1
    assert updated.status == "needs_review"
    assert updated.telegram_message_id is None
    assert updated.published_at is None
    assert "Перегенерированная версия" in updated.text
    assert updated.post_metadata["previous_ai_text"] == "Старый текст"


@pytest.mark.asyncio
async def test_content_factory_generates_premium_emoji_entities_for_premium_publisher(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="premium_publisher",
        status="active",
        import_type="session",
        daily_limits={"profile": {"is_premium": True}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Premium emoji channel",
            publisher_account_id=account.id,
            content_config={
                "prompt": "Пиши посты про платформу.",
                "max_length": 1200,
                "premium_emoji_enabled": True,
                "premium_emoji_catalog_text": "idea | 💡 | 5368324170671202286",
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Премиум emoji",
                    "text": (
                        "[[emoji:idea]] **Премиум emoji в посте**\n\n"
                        "Показываем, как аккуратно выделить смысл.\n\n"
                        "Главное — не переборщить."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    _run, post = await run_factory(session, factory)

    assert post is not None
    assert post.text.startswith("💡 **Премиум emoji")
    assert "[[emoji:" not in post.text
    assert post.post_metadata["custom_emoji_text"] == post.text
    assert post.post_metadata["custom_emoji_entities"][0]["offset"] == 0
    assert post.post_metadata["custom_emoji_entities"][0]["length"] == 2
    assert post.post_metadata["custom_emoji_entities"][0]["custom_emoji_id"] == "5368324170671202286"
    assert "[[emoji:idea]]" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_content_factory_forces_footer_and_body_premium_emoji(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="premium_publisher",
        status="active",
        import_type="session",
        daily_limits={"profile": {"is_premium": True}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Premium footer",
            publisher_account_id=account.id,
            content_config={
                "prompt": (
                    "Пиши посты про платформу. "
                    "В конце всегда ставь footer ровно: 🧠 [Платформа](https://demo.invalid) | [Канал](https://t.me/example_channel)"
                ),
                "product_site_url": "https://demo.invalid",
                "max_length": 1200,
                "premium_emoji_enabled": True,
                "premium_emoji_catalog_text": ("footer | 🧠 | 5226639745106330551\nspark | ✨ | 5201873494798776013"),
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        return (
            json.dumps(
                {
                    "title": "Премиум footer",
                    "text": (
                        "**Пост про demo-platform**\n\n"
                        "Первый абзац без премиум-эмодзи.\n\n"
                        "Второй абзац тоже без плейсхолдера.\n\n"
                        "🧠 [Платформа](https://demo.invalid) | [Канал](https://t.me/example_channel)"
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    _run, post = await run_factory(session, factory)

    assert post is not None
    assert "[[emoji:" not in post.text
    assert post.text.count("🧠") == 1
    assert "✨ Первый абзац" in post.text
    assert post.text.endswith("🧠 [Платформа](https://demo.invalid) | [Канал](https://t.me/example_channel)")
    entities = post.post_metadata["custom_emoji_entities"]
    assert [entity["custom_emoji_id"] for entity in entities] == [
        "5201873494798776013",
        "5226639745106330551",
    ]
    assert "Footer всегда начинай с [[emoji:footer]]" in captured["system_prompt"]


def test_premium_emoji_catalog_converts_plain_emoji_to_custom_entities() -> None:
    from app.modules.content_factory.service import apply_premium_emoji_placeholders, parse_premium_emoji_catalog

    catalog = parse_premium_emoji_catalog(
        "\n".join(
            [
                "fire | 🔥 | 5368324170671202287",
                "magnifier | 🔍 | 5368324170671202288",
                "footer | 🧠 | 5226639745106330551",
            ]
        )
    )

    text, entities = apply_premium_emoji_placeholders(
        "🔥 Первый акцент\n\n🔍 Деталь внутри поста\n\n🧠 [Платформа](https://demo.invalid)",
        catalog,
    )

    assert text.startswith("🔥 Первый акцент")
    assert [entity["emoji"] for entity in entities] == ["🔥", "🔍", "🧠"]
    assert [entity["custom_emoji_id"] for entity in entities] == [
        "5368324170671202287",
        "5368324170671202288",
        "5226639745106330551",
    ]


def test_premium_emoji_body_fallback_rotates_catalog_entries() -> None:
    from app.modules.content_factory.service import (
        ensure_premium_emoji_placeholders_for_post,
        parse_premium_emoji_catalog,
    )

    catalog = parse_premium_emoji_catalog(
        "\n".join(
            [
                "idea | 💡 | 5368324170671202286",
                "fire | 🔥 | 5368324170671202287",
                "rocket | 🚀 | 5368324170671202288",
            ]
        )
    )
    outputs = [
        ensure_premium_emoji_placeholders_for_post(
            f"**Пост {index}**\n\nРазный текст для выбора emoji {index}.",
            catalog,
        )
        for index in range(12)
    ]
    variants = {
        alias for alias in ("idea", "fire", "rocket") if any(f"[[emoji:{alias}]]" in output for output in outputs)
    }

    assert len(variants) > 1


@pytest.mark.asyncio
async def test_generate_post_prompt_includes_author_gender_from_content_config(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Author gender channel",
            content_config={
                "goal": "Пиши посты про Telegram SaaS",
                "author_gender": "masculine",
                "max_length": 1200,
            },
        ),
    )
    captured: dict[str, str] = {}

    async def fake_generate_text_response(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            json.dumps(
                {
                    "title": "Авторский голос",
                    "text": "Короткий пост про настройки авторского голоса.",
                    "image_prompt": None,
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    slot_brief = build_content_factory_slot_brief(
        factory,
        trigger_type="manual",
        now_utc=datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
    )
    await generate_post_payload(factory, {"summary": ""}, "База знаний", slot_brief)

    combined_prompt = f"{captured['system_prompt']}\n{captured['user_prompt']}"
    assert "Авторский род: мужской." in combined_prompt
    assert "Пиши от лица автора-мужчины." in combined_prompt
    assert "написал, готов, уверен" in combined_prompt


@pytest.mark.asyncio
async def test_quality_check_does_not_compare_recent_post_style(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Style memory"),
    )
    text = (
        "Контент-завод помогает вести черновики спокойнее.\n\n"
        "Открываете черновик, правите текст и фото, смотрите ссылку и только потом жмёте approve.\n\n"
        "Так пост не уходит в канал случайно, а проходит понятный рабочий сценарий."
    )

    score, notes = quality_check(
        factory,
        text,
        duplicate=None,
        style_duplicate=ContentMemoryItem(factory_id=factory.id),
    )

    assert score == 82
    assert "Базовая проверка качества пройдена" in notes
    assert not any("Формат слишком похож" in note for note in notes)


@pytest.mark.asyncio
async def test_quality_check_penalizes_ungrounded_product_post(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="demo-platform channel",
            content_config={"product_site_url": "https://demo.invalid", "min_length": 100},
        ),
    )
    text = (
        "Порядок в Telegram важен для команды.\n\n"
        "Платформа помогает видеть процесс целиком и не терять контроль.\n\n"
        "Так работать спокойнее и понятнее.\n\n"
        "[Платформа](https://demo.invalid)"
    )

    score, notes = quality_check(factory, text, duplicate=None)

    assert score < 82
    assert any("конкретному модулю" in note for note in notes)
    assert any("конкретика интерфейса" in note for note in notes)


@pytest.mark.asyncio
async def test_quality_check_accepts_concrete_content_factory_actions(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="demo-platform channel",
            content_config={"product_site_url": "https://demo.invalid", "min_length": 100},
        ),
    )
    text = (
        "В Контент-заводе quality-check нужен до публикации.\n\n"
        "Открываете черновик, правите текст и фото, смотрите ссылку и только потом жмёте approve.\n\n"
        "Так пост проходит понятный сценарий и не улетает в канал случайно.\n\n"
        "[Платформа](https://demo.invalid)"
    )

    score, notes = quality_check(factory, text, duplicate=None)

    assert score == 82
    assert "Базовая проверка качества пройдена" in notes
    assert not any("пользовательского действия" in note for note in notes)


@pytest.mark.asyncio
async def test_content_factory_slot_brief_rotates_editorial_traffic_angles(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Subtopics",
            schedule_config={
                "posts_per_day": 5,
                "publish_time_from": "08:00",
                "publish_time_to": "20:00",
                "timezone": "Europe/Moscow",
            },
            content_config={
                "content_plan": "Пятница — кейсы применения, сравнение до/после, рабочий вывод для команды",
            },
        ),
    )

    briefs = [
        build_content_factory_slot_brief(
            factory,
            trigger_type="schedule",
            now_utc=datetime(2026, 6, 12, hour, 0, tzinfo=UTC),
        )
        for hour in (5, 8, 11, 14, 18)
    ]
    subtopics = [brief.subtopic for brief in briefs]

    assert len(set(subtopics)) == 5
    assert any("Telegram-трафика" in subtopic for subtopic in subtopics)
    assert any("Лидогенерация" in subtopic for subtopic in subtopics)
    assert any("Источники трафика" in subtopic for subtopic in subtopics)


@pytest.mark.asyncio
async def test_manual_content_factory_slot_moves_even_with_one_scheduled_post(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Manual fresh topics",
            schedule_config={"posts_per_day": 1, "timezone": "Europe/Moscow"},
            content_config={
                "content_plan": (
                    "Воскресенье — Telegram-трафик, лидогенерация, источники, комментарии, контент, аналитика"
                ),
            },
        ),
    )
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    session.add(
        ContentPost(
            factory_id=factory.id,
            status="needs_review",
            title="Первый ручной пост",
            text="Пост про Telegram-трафик",
            created_at=now - timedelta(minutes=10),
        )
    )
    await session.commit()

    brief = await build_content_factory_slot_brief_for_run(session, factory, trigger_type="manual", now_utc=now)

    assert brief.posts_per_day == 5
    assert brief.slot_index == 1
    assert "лидогенерация" in brief.subtopic.lower() or "источники" in brief.subtopic.lower()


@pytest.mark.asyncio
async def test_tick_due_content_factories_runs_active_factory(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Scheduled factory",
            activate=True,
            schedule_config={
                "posts_per_day": 5,
                "timezone": "Europe/Moscow",
                "publish_time_from": "00:00",
                "publish_time_to": "23:59",
                "publish_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            },
        ),
    )
    initial_next_run_at = factory.next_run_at
    assert initial_next_run_at is not None
    factory.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Плановый пост",
                    "text": "Плановый пост контент-завода с нормальным текстом и проверкой расписания.",
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    runs = await tick_due_content_factories(session, workspace_id=workspace.id, limit=5)

    assert len(runs) == 1
    assert runs[0].factory_id == factory.id
    assert runs[0].trigger_type == "schedule"
    await session.refresh(factory)
    assert factory.next_run_at is not None
    assert ensure_aware_utc(factory.next_run_at) > datetime.now(UTC)
    posts = list((await session.execute(select(ContentPost))).scalars().all())
    assert len(posts) == 1


def test_content_factory_next_run_uses_future_slot() -> None:
    next_run = content_factory_next_run_at(
        {
            "posts_per_day": 5,
            "timezone": "Europe/Moscow",
            "publish_time_from": "08:00",
            "publish_time_to": "22:00",
            "publish_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        },
        datetime(2026, 6, 5, 9, 8, tzinfo=UTC),
    )

    assert next_run.astimezone(UTC) == datetime(2026, 6, 5, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_auto_publish_waits_for_required_image(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Image required",
            mode="auto_publish",
            image_config={"enabled": True},
            quality_config={"min_quality_score": 1},
        ),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Пост с фото",
                    "text": "Хороший пост должен дождаться изображения, если картинки включены в настройках завода.",
                    "image_prompt": "telegram automation dashboard",
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    async def fake_generate_image_for_post(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)
    monkeypatch.setattr("app.modules.content_factory.service.generate_image_for_post", fake_generate_image_for_post)

    run, post = await run_factory(session, factory)

    assert run.status == "ready"
    assert post is not None
    assert post.status == "needs_review"
    assert post.image_attachment_id is None
    assert post.post_metadata["publish_blocked_reason"] == "image_missing"
    assert any("Автопубликация остановлена" in note for note in post.quality_notes)


@pytest.mark.asyncio
async def test_auto_publish_keeps_post_as_review_when_footer_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Footer required",
            mode="auto_publish",
            content_config={
                "prompt": "Пиши посты и обязательно добавляй footer со ссылкой.",
                "min_length": 120,
                "max_length": 1000,
            },
            quality_config={"min_quality_score": 1},
        ),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Пост без футера",
                    "text": (
                        "Пост объясняет пользу контент-завода для регулярных публикаций.\n\n"
                        "Он разбивает работу на понятные этапы и помогает команде не держать процесс в голове.\n\n"
                        "Но здесь специально нет финальной ссылки."
                    ),
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    run, post = await run_factory(session, factory)

    assert run.status == "ready"
    assert post is not None
    assert post.status == "needs_review"
    assert post.telegram_message_id is None
    assert post.post_metadata["publish_blocked_reason"] == "auto_publish_preflight"
    assert any("footer/CTA" in note for note in post.quality_notes)


@pytest.mark.asyncio
async def test_image_generation_retries_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Image retry", image_config={"enabled": True}),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="needs_review",
        title="Post",
        text="Пост с изображением",
        image_prompt="dashboard screenshot",
        post_metadata={
            "image_status": "failed",
            "image_error": "old temporary DNS error",
            "image_retry_error": "old retry error",
            "image_retry_attempt": 1,
            "image_retry_delay_seconds": 65,
        },
    )
    session.add(post)
    await session.flush()
    calls = 0
    sleeps: list[float] = []

    async def fake_provider(prompt: str) -> tuple[bytes, dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AiProviderError(
                "AI_IMAGE_PROVIDER_HTTP_ERROR: 500 Rate limit reached for gpt-image on input-images per min: "
                "Limit 4000, Used 4000, Requested 1. Please try again in 15ms."
            )
        return b"image-bytes", {"mode": "test"}

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.modules.content_factory.service.generate_profile_avatar_image", fake_provider)
    monkeypatch.setattr("app.modules.content_factory.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "app.modules.content_factory.service._image_artifact_path",
        lambda *_args: tmp_path / "post.jpg",
    )

    await generate_image_for_post(session, factory, post)

    assert calls == 2
    assert sleeps and sleeps[0] >= 65
    assert post.image_attachment_id
    assert post.post_metadata["image_status"] == "ready"
    assert post.post_metadata["image_error"] is None
    assert post.post_metadata["image_retry_error"] is None
    assert post.post_metadata["image_retry_attempt"] is None
    assert post.post_metadata["image_retry_delay_seconds"] is None
    assert post.post_metadata["image_refreshed_at"]
    assert post.image_data == b"image-bytes"
    assert post.image_content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_content_factory_uses_knowledge_screenshot_for_post_image(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Screenshot image factory", image_config={"enabled": True}),
    )
    screenshot_path = tmp_path / "12-platform-content-factory-detail.png"
    screenshot_path.write_bytes(b"png-screenshot-bytes")
    item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=screenshot_path.name,
        kind="image",
        content_text="Скриншот контент-завода",
        file_path=str(screenshot_path),
        item_metadata={"filename": screenshot_path.name, "content_type": "image/png"},
        enabled=True,
    )
    post = ContentPost(
        factory_id=factory.id,
        status="needs_review",
        title="Разбор контент-завода",
        text="Пост про экран контент-завода",
        image_prompt=f"USE_KNOWLEDGE_IMAGE: {screenshot_path.name} | показываем экран завода",
    )
    session.add_all([item, post])
    await session.flush()
    monkeypatch.setattr(
        "app.modules.content_factory.service._image_artifact_path",
        lambda *_args: tmp_path / "post.jpg",
    )

    await generate_image_for_post(session, factory, post)

    assert post.image_data == b"png-screenshot-bytes"
    assert post.image_content_type == "image/png"
    assert post.image_attachment_id and post.image_attachment_id.endswith(".png")
    assert post.post_metadata["image_source"] == "knowledge_image"
    assert post.post_metadata["knowledge_image_title"] == screenshot_path.name


@pytest.mark.asyncio
async def test_content_factory_uses_persisted_knowledge_image_bytes_when_artifact_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Persisted screenshot factory", image_config={"enabled": True}),
    )
    filename = "12-platform-content-factory-detail.png"
    missing_artifact_path = Path("/app/artifacts/content-factory-knowledge") / filename
    persisted_png = b"persisted-png-screenshot-bytes"
    item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=filename,
        kind="image",
        content_text="Скриншот контент-завода",
        file_path=str(missing_artifact_path),
        item_metadata={"filename": filename, "content_type": "image/png"},
        enabled=True,
    )
    if hasattr(ContentFactoryKnowledgeItem, "file_data"):
        item.file_data = persisted_png
    else:
        item.__dict__["file_data"] = persisted_png
    post = ContentPost(
        factory_id=factory.id,
        status="needs_review",
        title="Разбор контент-завода",
        text="Пост про экран контент-завода",
        image_prompt=f"USE_KNOWLEDGE_IMAGE: {filename} | показываем экран завода",
    )
    session.add_all([item, post])
    await session.commit()
    factory_id = factory.id
    post_id = post.id
    session.expunge_all()
    factory = await session.get(type(factory), factory_id)
    post = await session.get(ContentPost, post_id)
    assert factory is not None
    assert post is not None

    async def fail_generated_image_provider(prompt: str) -> tuple[bytes, dict]:
        pytest.fail(f"expected persisted knowledge file bytes, but image generation fallback ran for {prompt!r}")

    monkeypatch.setattr(
        "app.modules.content_factory.service.generate_profile_avatar_image",
        fail_generated_image_provider,
    )
    monkeypatch.setattr(
        "app.modules.content_factory.service._image_artifact_path",
        lambda *_args: tmp_path / "post.jpg",
    )

    await generate_image_for_post(session, factory, post)

    assert post.image_data == persisted_png
    assert post.image_content_type == "image/png"
    assert post.post_metadata["image_source"] == "knowledge_image"


@pytest.mark.asyncio
async def test_content_factory_knowledge_only_image_mode_uses_uploaded_image_without_provider(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Knowledge only images",
            image_config={"enabled": True, "knowledge_only": True},
        ),
    )
    filename = "landing-home.png"
    image_path = tmp_path / filename
    image_path.write_bytes(b"knowledge-only-png")
    item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=filename,
        kind="image",
        content_text="Публичный скриншот лендинга",
        file_path=str(image_path),
        item_metadata={"filename": filename, "content_type": "image/png"},
        enabled=True,
    )
    post = ContentPost(
        factory_id=factory.id,
        status="needs_review",
        title="Интро платформы",
        text="Пост про платформу",
        image_prompt="editorial cover for platform intro",
    )
    session.add_all([item, post])
    await session.flush()

    async def fail_generated_image_provider(prompt: str) -> tuple[bytes, dict]:
        pytest.fail(f"image provider must not run in knowledge-only mode: {prompt!r}")

    monkeypatch.setattr(
        "app.modules.content_factory.service.generate_profile_avatar_image",
        fail_generated_image_provider,
    )
    monkeypatch.setattr(
        "app.modules.content_factory.service._image_artifact_path",
        lambda *_args: tmp_path / "post.jpg",
    )

    await generate_image_for_post(session, factory, post)

    assert post.image_data == b"knowledge-only-png"
    assert post.image_content_type == "image/png"
    assert post.post_metadata["image_source"] == "knowledge_image"
    assert post.post_metadata["knowledge_image_title"] == filename


@pytest.mark.asyncio
async def test_content_factory_knowledge_only_image_fallback_prefers_matching_topic(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Knowledge matching",
            image_config={"enabled": True, "knowledge_only": True},
        ),
    )
    warmup_path = tmp_path / "13-landing-account-warmup.png"
    factory_path = tmp_path / "12-platform-content-factory-detail.png"
    warmup_path.write_bytes(b"warmup")
    factory_path.write_bytes(b"factory")
    session.add_all(
        [
            ContentFactoryKnowledgeItem(
                factory_id=factory.id,
                title=warmup_path.name,
                kind="image",
                content_text="Скриншот прогрева",
                file_path=str(warmup_path),
                item_metadata={"filename": warmup_path.name, "content_type": "image/png"},
                enabled=True,
            ),
            ContentFactoryKnowledgeItem(
                factory_id=factory.id,
                title=factory_path.name,
                kind="image",
                content_text="Скриншот контент-завода",
                file_path=str(factory_path),
                item_metadata={"filename": factory_path.name, "content_type": "image/png"},
                enabled=True,
            ),
        ]
    )
    await session.flush()

    selected = await select_knowledge_image_for_post(
        session,
        factory,
        "Пост про контент-завод, черновики и публикации",
        allow_fallback=True,
    )

    assert selected is not None
    assert selected.title == factory_path.name


@pytest.mark.asyncio
async def test_content_factory_does_not_select_sensitive_admin_knowledge_image(
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Sensitive screenshots", image_config={"enabled": True}),
    )
    item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title="14-platform-operations.png",
        kind="image",
        content_text="Operation Events, Sentry and internal alerts dashboard",
        file_path="/tmp/14-platform-operations.png",
        item_metadata={"filename": "14-platform-operations.png", "content_type": "image/png"},
        enabled=True,
    )
    session.add(item)
    await session.flush()

    selected = await select_knowledge_image_for_post(
        session,
        factory,
        "USE_KNOWLEDGE_IMAGE: 14-platform-operations.png | показываем операции",
    )

    assert selected is None


@pytest.mark.asyncio
async def test_content_factory_allows_explicit_recent_knowledge_image(
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="No repeated screenshots", image_config={"enabled": True}),
    )
    filename = "12-platform-content-factory-detail.png"
    item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=filename,
        kind="image",
        content_text="Скриншот контент-завода",
        file_path=f"/tmp/{filename}",
        item_metadata={"filename": filename, "content_type": "image/png"},
        enabled=True,
    )
    recent_post = ContentPost(
        factory_id=factory.id,
        status="published",
        title="Недавний пост",
        text="Недавний пост с этим скрином",
        post_metadata={"knowledge_image_title": filename},
    )
    session.add_all([item, recent_post])
    await session.flush()

    selected = await select_knowledge_image_for_post(
        session,
        factory,
        f"USE_KNOWLEDGE_IMAGE: {filename} | повторяем экран",
    )

    assert selected is not None
    assert selected.title == filename

    selected_for_generation_mode = await select_knowledge_image_for_post(
        session,
        factory,
        f"USE_KNOWLEDGE_IMAGE: {filename} | повторяем экран",
        allow_recent_requested=False,
    )

    assert selected_for_generation_mode is None


@pytest.mark.asyncio
async def test_content_factory_does_not_repeat_recent_knowledge_image_for_fallback(
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="No repeated fallback screenshots", image_config={"enabled": True}),
    )
    recent_filename = "12-platform-content-factory-detail.png"
    fresh_filename = "08-content-factory.png"
    recent_item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=recent_filename,
        kind="image",
        content_text="Скриншот контент-завода",
        file_path=f"/tmp/{recent_filename}",
        item_metadata={"filename": recent_filename, "content_type": "image/png"},
        enabled=True,
    )
    fresh_item = ContentFactoryKnowledgeItem(
        factory_id=factory.id,
        title=fresh_filename,
        kind="image",
        content_text="Скриншот контент-завода",
        file_path=f"/tmp/{fresh_filename}",
        item_metadata={"filename": fresh_filename, "content_type": "image/png"},
        enabled=True,
    )
    recent_post = ContentPost(
        factory_id=factory.id,
        status="published",
        title="Недавний пост",
        text="Недавний пост с этим скрином",
        post_metadata={"knowledge_image_title": recent_filename},
    )
    session.add_all([recent_item, fresh_item, recent_post])
    await session.flush()

    selected = await select_knowledge_image_for_post(
        session,
        factory,
        "Пост про контент-завод и черновики",
        allow_fallback=True,
    )

    assert selected is not None
    assert selected.title == fresh_filename


@pytest.mark.asyncio
async def test_content_post_image_endpoint_serves_database_image_when_file_is_missing(session: AsyncSession) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Image from db"),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="needs_review",
        title="Post",
        text="Текст",
        image_attachment_id="/tmp/missing-content-factory-image.jpg",
        image_data=b"persisted-image",
        image_content_type="image/jpeg",
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)

    response = await get_content_post_image(factory.id, post.id, session, workspace.id)

    assert response.body == b"persisted-image"
    assert response.media_type == "image/jpeg"


def test_image_retry_delay_ignores_tiny_hint_for_minute_rate_limit() -> None:
    delay = image_retry_delay_seconds(
        "AI_IMAGE_PROVIDER_HTTP_ERROR: 500 Rate limit reached on input-images per min. Please try again in 15ms.",
        1,
    )

    assert delay is not None
    assert delay >= 65
    assert delay <= 66


@pytest.mark.asyncio
async def test_duplicate_fingerprint_does_not_lower_quality_score(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(name="Duplicates"),
    )

    async def fake_generate_text_response(*args: object, **kwargs: object) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "title": "Повтор",
                    "text": "Один и тот же полезный текст про безопасные черновики Telegram.",
                    "image_prompt": None,
                    "source_refs": [],
                    "quality_notes": [],
                    "risk_flags": [],
                }
            ),
            {},
        )

    monkeypatch.setattr("app.modules.content_factory.service.generate_text_response", fake_generate_text_response)

    _, first = await run_factory(session, factory)
    _, second = await run_factory(session, factory)

    assert first is not None
    assert second is not None
    assert first.quality_score == second.quality_score
    assert not any("fingerprint" in note for note in second.quality_notes)


@pytest.mark.asyncio
async def test_publish_real_telegram_mode_uses_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Publish",
            target_ref="@target",
            publisher_account_id=account.id,
        ),
    )
    post = ContentPost(factory_id=factory.id, status="needs_review", title="Post", text="Текст для публикации")
    session.add(post)
    await session.commit()
    await session.refresh(post)
    await approve_post(session, factory, post)

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)

    with pytest.raises(ContentFactoryError, match="TELEGRAM_PUBLISH_FAILED:TELEGRAM_API_NOT_CONFIGURED"):
        await publish_post(session, factory, post)


@pytest.mark.asyncio
async def test_rejected_content_post_can_be_manually_approved_and_published(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Manual override",
            target_ref="@target",
            publisher_account_id=account.id,
        ),
    )
    post = ContentPost(factory_id=factory.id, status="rejected", title="Post", text="Текст после ручной проверки")
    session.add(post)
    await session.commit()
    await session.refresh(post)

    approved = await approve_post(session, factory, post)

    assert approved.status == "approved"
    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: False)

    published = await publish_post(session, factory, approved)

    assert published.status == "published"


@pytest.mark.asyncio
async def test_publish_uses_database_image_when_artifact_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Publish with db image",
            target_ref="@target",
            publisher_account_id=account.id,
            image_config={"enabled": True},
        ),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="approved",
        title="Post",
        text="Текст для публикации",
        image_attachment_id="/tmp/missing-content-factory-image.jpg",
        image_data=b"db-image",
        image_content_type="image/jpeg",
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    captured: dict[str, object] = {}

    async def fake_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        image_path = kwargs["image_path"]
        assert isinstance(image_path, str)
        captured["image_bytes"] = Path(image_path).read_bytes()
        captured["image_path_exists_during_send"] = Path(image_path).exists()
        return TelegramSendResult(tg_message_id="123", raw={"ok": True})

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        fake_send_channel_post_with_account,
    )

    published = await publish_post(session, factory, post)

    assert published.status == "published"
    assert captured["image_bytes"] == b"db-image"
    assert captured["image_path_exists_during_send"] is True


@pytest.mark.asyncio
async def test_publish_blocks_overlong_photo_caption_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Old caption limit",
            target_ref="@target",
            publisher_account_id=account.id,
            image_config={"enabled": True},
            content_config={"product_site_url": "https://demo.invalid", "max_length": 1024},
        ),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="approved",
        title="Post",
        text="x" * 1024,
        image_data=b"db-image",
        image_content_type="image/jpeg",
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)

    async def fake_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        raise AssertionError("overlong image caption must be blocked before Telegram gateway")
        return TelegramSendResult(tg_message_id="123", raw={"action": "send_file"})

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        fake_send_channel_post_with_account,
    )

    with pytest.raises(ContentFactoryError, match="CONTENT_POST_CAPTION_TOO_LONG"):
        await publish_post(session, factory, post)

    await session.refresh(post)
    assert post.status == "approved"
    assert post.post_metadata["publish_blocked_reason"] == "caption_too_long"
    assert post.post_metadata["caption_limit"] == 1000
    assert post.post_metadata["caption_length"] > 1000


@pytest.mark.asyncio
async def test_publish_passes_premium_emoji_entities_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="premium_publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"profile": {"is_premium": True}, "restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Premium publish",
            target_ref="@target",
            publisher_account_id=account.id,
            content_config={
                "premium_emoji_enabled": True,
                "premium_emoji_catalog_text": "idea | 💡 | 5368324170671202286",
            },
        ),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="approved",
        title="Post",
        text="💡 Пост с премиум-эмодзи",
        post_metadata={
            "custom_emoji_text": "💡 Пост с премиум-эмодзи",
            "custom_emoji_entities": [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "5368324170671202286",
                    "alias": "idea",
                    "emoji": "💡",
                }
            ],
        },
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    captured: dict[str, object] = {}

    async def fake_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        captured["formatting_entities"] = kwargs.get("formatting_entities")
        return TelegramSendResult(tg_message_id="123", raw={"custom_emoji_entities": 1})

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        fake_send_channel_post_with_account,
    )

    published = await publish_post(session, factory, post)

    assert published.status == "published"
    assert captured["formatting_entities"] == post.post_metadata["custom_emoji_entities"]


@pytest.mark.asyncio
async def test_publish_rebuilds_stale_premium_emoji_entities(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="premium_publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"profile": {"is_premium": True}, "restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Premium publish fresh catalog",
            target_ref="@target",
            publisher_account_id=account.id,
            content_config={
                "premium_emoji_enabled": True,
            },
        ),
    )
    post = ContentPost(
        factory_id=factory.id,
        status="approved",
        title="Post",
        text="Пост без плейсхолдера.\n\nВторой абзац для акцента.",
        post_metadata={
            "custom_emoji_text": "Пост без плейсхолдера.\n\nВторой абзац для акцента.",
            "custom_emoji_entities": [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "1111111111111111111",
                    "alias": "old",
                    "emoji": "💡",
                }
            ],
        },
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    captured: dict[str, object] = {}

    async def fake_fetch_account_premium_emoji_catalog(
        *args: object,
        **kwargs: object,
    ) -> tuple[bool, list[dict[str, str]]]:
        return (
            True,
            [
                {"alias": "fresh", "emoji": "🔍", "custom_emoji_id": "2222222222222222222"},
                {"alias": "chart", "emoji": "📊", "custom_emoji_id": "3333333333333333333"},
            ],
        )

    async def fake_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        captured["text"] = args[3]
        captured["formatting_entities"] = kwargs.get("formatting_entities")
        return TelegramSendResult(tg_message_id="123", raw={"custom_emoji_entities": 1})

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        "app.modules.content_factory.service.fetch_account_premium_emoji_catalog",
        fake_fetch_account_premium_emoji_catalog,
    )
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        fake_send_channel_post_with_account,
    )

    published = await publish_post(session, factory, post)

    assert published.status == "published"
    entities = captured["formatting_entities"]
    assert isinstance(entities, list)
    assert {entity["custom_emoji_id"] for entity in entities} != {"1111111111111111111"}
    assert any(entity["custom_emoji_id"] in {"2222222222222222222", "3333333333333333333"} for entity in entities)


@pytest.mark.asyncio
async def test_publish_stops_after_first_telegram_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Retry publish",
            target_ref="@target",
            publisher_account_id=account.id,
        ),
    )
    post = ContentPost(factory_id=factory.id, status="approved", title="Post", text="Текст для публикации")
    session.add(post)
    await session.commit()
    await session.refresh(post)
    attempts = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def flaky_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TelegramGatewayError(
                "TELEGRAM_CONNECTION_FAILED",
                "Connection to Telegram failed: transient",
                {"error_type": "ConnectionError"},
            )
        return TelegramSendResult(tg_message_id="123", raw={"ok": True})

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.content_factory.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        flaky_send_channel_post_with_account,
    )

    with pytest.raises(ContentFactoryError, match="TELEGRAM_PUBLISH_FAILED"):
        await publish_post(session, factory, post)
    assert post.status != "published"
    assert post.telegram_message_id is None
    assert attempts == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_publish_stores_clear_proxy_error_on_telegram_failure(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    user, workspace = await _workspace(session)
    account = TelegramAccount(
        workspace_id=workspace.id,
        username="publisher",
        status="active",
        import_type="session",
        proxy_required=False,
        warmup_status="completed",
        daily_limits={"restriction_status": {"status": "clear"}},
    )
    session.add(account)
    await session.flush()
    factory = await create_factory(
        session,
        workspace_id=workspace.id,
        created_by=user.id,
        payload=ContentFactoryCreate(
            name="Proxy failure",
            target_ref="@target",
            publisher_account_id=account.id,
        ),
    )
    post = ContentPost(factory_id=factory.id, status="approved", title="Post", text="Текст для публикации")
    session.add(post)
    await session.commit()
    await session.refresh(post)

    async def fake_sleep(delay: float) -> None:
        return None

    async def failed_send_channel_post_with_account(*args: object, **kwargs: object) -> TelegramSendResult:
        raise TelegramGatewayError(
            "TELEGRAM_CONNECTION_FAILED",
            "Connection to Telegram failed: SOCKS5 authentication failed",
            {"error_type": "ConnectionError", "message": "SOCKS5 authentication failed"},
        )

    monkeypatch.setattr("app.modules.content_factory.service.real_telegram_enabled", lambda: True)
    monkeypatch.setattr("app.modules.content_factory.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "app.modules.content_factory.service.send_channel_post_with_account",
        failed_send_channel_post_with_account,
    )

    with pytest.raises(ContentFactoryError, match="TELEGRAM_PUBLISH_FAILED:TELEGRAM_CONNECTION_FAILED"):
        await publish_post(session, factory, post)

    await session.refresh(post)
    assert post.status == "approved"
    assert post.post_metadata["publish_blocked_reason"] == "telegram_gateway"
    problem = post.post_metadata["last_publish_problem"]
    assert problem["code"] == "TELEGRAM_CONNECTION_FAILED"
    assert "прокси" in problem["message"].lower()
