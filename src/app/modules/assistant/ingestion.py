from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal, engine
from app.modules.assistant.models import AssistantKnowledgeChunk, AssistantKnowledgeSource
from app.modules.billing.products import READY_PRODUCTS
from app.modules.plans.catalog import BUSINESS_MODEL

ROOT = Path(__file__).resolve().parents[4]
HELP_TS = ROOT / "frontend" / "src" / "content" / "help.ts"
DOCS = [
    ROOT / "docs" / "landing-help-center-instructions-tz-2026-06-08.md",
    ROOT / "docs" / "gramgpt-parsing-audit-tz-2026-06-03.md",
    ROOT / "docs" / "gramgpt-reactions-audit-tz-2026-06-03.md",
    ROOT / "docs" / "gramgpt-neurochatting-audit-2026-06-12.md",
]
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
SECRET_RE = re.compile(r"(token|secret|bearer|ssh|password|api[_-]?key)", re.IGNORECASE)


@dataclass(frozen=True)
class KnowledgeDocument:
    source_type: str
    title: str
    path: str
    public_url: str | None
    visibility: str
    content: str
    metadata: dict


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_checksum(document: KnowledgeDocument) -> str:
    payload = {
        "content": document.content,
        "metadata": document.metadata,
        "public_url": document.public_url,
        "title": document.title,
        "visibility": document.visibility,
    }
    return checksum(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def clean_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def chunk_text(text: str, *, max_words: int = 170, overlap: int = 25) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = max(1, max_words - overlap)
    for start in range(0, len(words), step):
        part = words[start : start + max_words]
        if len(part) < 18 and chunks:
            break
        chunks.append(" ".join(part))
    return chunks


def keywords_for(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яё0-9-]{4,}", text.lower())
    stop = {"который", "чтобы", "можно", "если", "этого", "после", "через", "платформы"}
    result: list[str] = []
    for word in words:
        if word in stop or word in result:
            continue
        result.append(word)
        if len(result) >= 20:
            break
    return result


def extract_help_documents() -> list[KnowledgeDocument]:
    if not HELP_TS.exists():
        return []
    raw = HELP_TS.read_text(encoding="utf-8")
    documents: list[KnowledgeDocument] = []
    for block in raw.split("article({")[1:]:
        item = block.split("}),", 1)[0]
        slug = _field(item, "slug") or "help"
        title = _field(item, "title") or slug
        strings = [_decode_ts_string(value) for value in STRING_RE.findall(item)]
        content = clean_text(" ".join(strings))
        documents.append(
            KnowledgeDocument(
                source_type="help_article",
                title=title,
                path=f"frontend/src/content/help.ts#{slug}",
                public_url=f"/help/{slug}",
                visibility="public",
                content=content,
                metadata={"slug": slug, "modules": _help_slug_modules(slug, content)},
            )
        )
    return documents


def extract_markdown_documents() -> list[KnowledgeDocument]:
    documents: list[KnowledgeDocument] = []
    for path in DOCS:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        safe_lines = [line for line in raw.splitlines() if not SECRET_RE.search(line)]
        title = next((line.lstrip("# ").strip() for line in safe_lines if line.startswith("#")), path.stem)
        content = clean_text("\n".join(safe_lines))
        documents.append(
            KnowledgeDocument(
                source_type="markdown_doc",
                title=title,
                path=str(path.relative_to(ROOT)),
                public_url="/help",
                visibility="authenticated",
                content=content,
                metadata={"modules": _modules(content)},
            )
        )
    return documents


def extract_pricing_documents() -> list[KnowledgeDocument]:
    lines = ["Тарифы demo-platform:"]
    for plan in BUSINESS_MODEL.get("self_serve_plans", []):
        lines.append(
            f"{plan.get('name')}: {int(plan.get('monthly_price') or 0)} ₽/мес, "
            f"{int(plan.get('yearly_price') or 0)} ₽/год. {plan.get('description') or ''}"
        )
    for code, product in READY_PRODUCTS.items():
        if code in {"free", "start", "core", "agency"}:
            lines.append(
                f"Пакет {product.name}: {product.amount} {product.currency}, "
                f"длительность {product.duration_days} дней, лимиты {product.limits}."
            )
    return [
        KnowledgeDocument(
            source_type="pricing_catalog",
            title="Тарифы и лимиты demo-platform",
            path="src/app/modules/plans/catalog.py",
            public_url="/pricing",
            visibility="public",
            content=clean_text(" ".join(lines)),
            metadata={"modules": ["billing"]},
        )
    ]


def extract_documents(source: str) -> list[KnowledgeDocument]:
    documents: list[KnowledgeDocument] = []
    if source in {"all", "help"}:
        documents.extend(extract_help_documents())
    if source in {"all", "docs"}:
        documents.extend(extract_markdown_documents())
    if source in {"all", "pricing"}:
        documents.extend(extract_pricing_documents())
    return documents


async def reindex_knowledge(session: AsyncSession, source: str = "all") -> tuple[int, int]:
    documents = extract_documents(source)
    source_count = 0
    chunk_count = 0
    now = datetime.now(UTC)
    for document in documents:
        digest = document_checksum(document)
        existing = (
            await session.execute(
                select(AssistantKnowledgeSource).where(AssistantKnowledgeSource.path == document.path)
            )
        ).scalar_one_or_none()
        if existing and existing.checksum == digest and existing.status == "active":
            source_count += 1
            chunk_count += int(
                await session.scalar(
                    select(func.count(AssistantKnowledgeChunk.id)).where(
                        AssistantKnowledgeChunk.source_id == existing.id
                    )
                )
                or 0
            )
            continue
        source_row = existing or AssistantKnowledgeSource(path=document.path)
        source_row.source_type = document.source_type
        source_row.title = document.title
        source_row.public_url = document.public_url
        source_row.visibility = document.visibility
        source_row.checksum = digest
        source_row.status = "active"
        source_row.last_indexed_at = now
        source_row.source_metadata = document.metadata
        session.add(source_row)
        await session.flush()
        await session.execute(delete(AssistantKnowledgeChunk).where(AssistantKnowledgeChunk.source_id == source_row.id))
        for index, chunk in enumerate(chunk_text(document.content)):
            keywords = keywords_for(f"{document.title} {chunk}")
            session.add(
                AssistantKnowledgeChunk(
                    source_id=source_row.id,
                    title=document.title,
                    section_path=document.path,
                    content=chunk,
                    chunk_index=index,
                    token_count=len(chunk.split()),
                    keywords=keywords,
                    visibility=document.visibility,
                    search_text=clean_text(f"{document.title} {' '.join(keywords)} {chunk}"),
                    chunk_metadata=document.metadata,
                )
            )
            chunk_count += 1
        source_count += 1
    return source_count, chunk_count


def _field(block: str, name: str) -> str | None:
    match = re.search(rf'{name}:\s*"((?:[^"\\]|\\.)*)"', block)
    if not match:
        return None
    return _decode_ts_string(match.group(1))


def _decode_ts_string(value: str) -> str:
    try:
        return ast.literal_eval(f'"{value}"')
    except (SyntaxError, ValueError):
        return value


def _modules(text: str) -> list[str]:
    normalized = text.lower()
    modules: list[str] = []
    mapping = {
        "neurocommenting": ["нейрокоммент", "комментар"],
        "neurochatting": ["нейрочат", "ai-чат", "групп"],
        "reactions": ["реакц", "emoji"],
        "parsing": ["парсинг", "аудитор"],
        "warmup": ["прогрев"],
        "billing": ["тариф", "оплат", "цена"],
        "proxy": ["прокси"],
        "accounts": ["аккаунт", "session", "tdata"],
        "content_factory": ["контент-завод", "контент"],
    }
    for module, needles in mapping.items():
        if any(needle in normalized for needle in needles):
            modules.append(module)
    return modules


def _help_slug_modules(slug: str, content: str) -> list[str]:
    slug_modules = {
        "getting-started": ["neurocommenting", "proxy", "accounts", "warmup"],
        "accounts": ["accounts"],
        "proxies": ["proxy"],
        "sources": ["accounts"],
        "channel-base": ["channel_base"],
        "parsing": ["parsing"],
        "warmup": ["warmup"],
        "ai-comments": ["neurocommenting"],
        "message-broadcasts": ["message_broadcast"],
        "ai-chat": ["neurochatting"],
        "ai-dialogs": ["neurodialogs"],
        "ai-reactions": ["reactions"],
        "content-factory": ["content_factory"],
        "safety": ["accounts", "proxy", "warmup"],
        "billing": ["billing"],
    }
    return slug_modules.get(slug, _modules(content))


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["all", "docs", "help", "pricing"], default="all")
    args = parser.parse_args()
    try:
        async with AsyncSessionLocal() as session:
            sources, chunks = await reindex_knowledge(session, args.source)
            await session.commit()
            print({"status": "ok", "sources": sources, "chunks": chunks})
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
