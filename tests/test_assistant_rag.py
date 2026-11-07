from __future__ import annotations

from app.modules.assistant.ingestion import (
    KnowledgeDocument,
    chunk_text,
    clean_text,
    document_checksum,
    extract_help_documents,
    keywords_for,
)
from app.modules.assistant.prompts import LOW_CONFIDENCE_ANSWER, build_context_prompt, is_how_to_question
from app.modules.assistant.retriever import detect_modules, tokenize
from app.modules.assistant.schemas import AssistantSupportRequestCreate


def test_chunk_text_uses_overlap() -> None:
    text = " ".join(f"word{i}" for i in range(80))
    chunks = chunk_text(text, max_words=30, overlap=5)

    assert len(chunks) >= 3
    assert "word25" in chunks[0]
    assert "word25" in chunks[1]


def test_clean_text_removes_code_blocks_and_noise() -> None:
    cleaned = clean_text("Заголовок\n```secret token\nabc\n```\n<p>Текст</p>")

    assert "secret" not in cleaned
    assert "token" not in cleaned
    assert "Текст" in cleaned


def test_keywords_are_limited_and_unique() -> None:
    keywords = keywords_for("Нейрокомментинг нейрокомментинг аккаунты прокси тарифы Telegram")

    assert keywords.count("нейрокомментинг") == 1
    assert "аккаунты" in keywords
    assert len(keywords) <= 20


def test_help_article_modules_follow_slug_not_broad_text_matches() -> None:
    by_slug = {item.metadata["slug"]: item.metadata["modules"] for item in extract_help_documents()}

    assert by_slug["ai-comments"] == ["neurocommenting"]
    assert by_slug["parsing"] == ["parsing"]
    assert "neurocommenting" not in by_slug["parsing"]


def test_document_checksum_changes_when_metadata_changes() -> None:
    base = KnowledgeDocument(
        source_type="help_article",
        title="Title",
        path="path",
        public_url="/help/a",
        visibility="public",
        content="Same content",
        metadata={"modules": ["parsing"]},
    )
    changed = KnowledgeDocument(
        source_type=base.source_type,
        title=base.title,
        path=base.path,
        public_url=base.public_url,
        visibility=base.visibility,
        content=base.content,
        metadata={"modules": ["neurocommenting"]},
    )

    assert document_checksum(base) != document_checksum(changed)


def test_retriever_detects_module_synonyms() -> None:
    assert "neurocommenting" in detect_modules("Как запустить нейрокомментинг?")
    assert "billing" in detect_modules("Сколько стоит тариф Core?")
    assert "warmup" in detect_modules("Как настроить прогрев аккаунта?")
    assert "proxy" in detect_modules("Почему прокси не работает?")


def test_prompt_contains_question_and_context() -> None:
    class Chunk:
        id = "chunk-1"
        source_id = "source-1"
        title = "Тарифы"
        content = "Start стоит 4990 рублей."
        chunk_metadata = {"public_url": "/pricing"}

    prompt = build_context_prompt("Сколько стоит Start?", [(Chunk(), 2.0)])

    assert "Сколько стоит Start?" in prompt
    assert "Start стоит 4990 рублей" in prompt
    assert "отвечай только по найденным фрагментам" in prompt


def test_how_to_prompt_requests_detailed_onboarding_instruction() -> None:
    class Chunk:
        id = "chunk-2"
        source_id = "source-2"
        title = "AI-комментарии"
        content = "Откройте раздел AI-комментарии, создайте кампанию, выберите аккаунты и источники."
        chunk_metadata = {"public_url": "/help/ai-comments"}

    assert is_how_to_question("Как запустить нейрокомментинг?")
    prompt = build_context_prompt("Как запустить нейрокомментинг?", [(Chunk(), 3.0)])

    assert "подробный пошаговый ответ" in prompt
    assert "6-12 конкретных шагов" in prompt
    assert "Markdown-ссылкой" in prompt
    assert "/help/ai-comments" in prompt


def test_low_confidence_answer_is_honest() -> None:
    assert "не нашел точную информацию" in LOW_CONFIDENCE_ANSWER
    assert tokenize("AI-комментарии и тарифы")


def test_support_request_payload_keeps_contact_and_message() -> None:
    payload = AssistantSupportRequestCreate(
        conversation_id="conversation-1",
        surface="landing",
        contact="@alex",
        message="Нужна помощь с первым запуском нейрокомментинга",
        page_url="/help/ai-comments",
        visitor_id="anon_1",
    )

    assert payload.contact == "@alex"
    assert payload.message.startswith("Нужна помощь")
