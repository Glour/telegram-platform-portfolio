from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assistant.models import AssistantKnowledgeChunk, AssistantKnowledgeSource

WORD_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
MODULE_SYNONYMS: dict[str, set[str]] = {
    "neurocommenting": {"нейрокомментинг", "комментарии", "ai-комментарии", "комменты"},
    "neurochatting": {"нейрочат", "ai-чат", "группы", "чат"},
    "reactions": {"реакции", "нейрореакции", "лайки", "emoji"},
    "parsing": {"парсинг", "аудитория", "сбор", "users", "посты"},
    "warmup": {"прогрев", "warming", "лимиты"},
    "billing": {"тариф", "цена", "оплата", "стоимость", "аккаунтов"},
    "proxy": {"прокси", "proxy"},
    "accounts": {"аккаунт", "session", "tdata", "импорт"},
}


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list[tuple[AssistantKnowledgeChunk, float]]
    confidence: float


def tokenize(text: str) -> set[str]:
    return {match.group(0).lower().replace("ё", "е") for match in WORD_RE.finditer(text)}


def detect_modules(question: str) -> set[str]:
    normalized = question.lower().replace("ё", "е")
    found: set[str] = set()
    for module, words in MODULE_SYNONYMS.items():
        if any(word.replace("ё", "е") in normalized for word in words):
            found.add(module)
    return found


async def retrieve_relevant_chunks(
    session: AsyncSession,
    *,
    question: str,
    visibility: list[str],
    limit: int = 10,
) -> RetrievalResult:
    question_terms = tokenize(question)
    if not question_terms:
        return RetrievalResult(chunks=[], confidence=0.0)

    stmt = (
        select(AssistantKnowledgeChunk, AssistantKnowledgeSource)
        .join(AssistantKnowledgeSource, AssistantKnowledgeSource.id == AssistantKnowledgeChunk.source_id)
        .where(AssistantKnowledgeChunk.visibility.in_(visibility))
        .where(AssistantKnowledgeSource.status == "active")
        .limit(300)
    )
    rows = (await session.execute(stmt)).all()
    modules = detect_modules(question)
    scored: list[tuple[AssistantKnowledgeChunk, float]] = []
    for chunk, source in rows:
        haystack = f"{chunk.title} {chunk.section_path or ''} {' '.join(chunk.keywords or [])} {chunk.content}"
        terms = tokenize(haystack)
        overlap = len(question_terms & terms)
        if overlap <= 0:
            continue
        title_terms = tokenize(chunk.title)
        keyword_terms = tokenize(" ".join(chunk.keywords or []))
        module_score = 0.0
        metadata = chunk.chunk_metadata or {}
        source_modules = set(metadata.get("modules") or [])
        if modules & source_modules:
            module_score = 6.0
        elif modules and source_modules:
            module_score = -3.0
        score = (
            overlap + len(question_terms & title_terms) * 1.8 + len(question_terms & keyword_terms) * 1.2 + module_score
        )
        if source.public_url:
            chunk.chunk_metadata = {**metadata, "public_url": source.public_url}
        scored.append((chunk, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[:limit]
    confidence = top[0][1] / max(len(question_terms), 1) if top else 0.0
    return RetrievalResult(chunks=top, confidence=confidence)
