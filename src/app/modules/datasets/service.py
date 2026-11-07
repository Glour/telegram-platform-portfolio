from app.modules.telegram_gateway.service import (
    TelegramDialogSourceInfo,
    TelegramGatewayError,
    discover_comment_sources_with_account,
    fetch_post_comments_with_account,
    source_ref_from_fields,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.datasets.models import CommentDatasetItem
from app.modules.datasets.schemas import (
    CommentDatasetCollectRequest,
    CommentDatasetCollectResponse,
    CommentDatasetDiscoverCollectRequest,
    CommentDatasetDiscoverCollectResponse,
)
from app.modules.parsing.service import fetch_real_posts
from app.modules.sources.models import TelegramMessage, TelegramSource
from app.modules.telegram_accounts.models import TelegramAccount


async def collect_comment_dataset(
    session: AsyncSession,
    workspace_id: str,
    payload: CommentDatasetCollectRequest,
) -> CommentDatasetCollectResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    sources = await _resolve_sources(session, workspace_id, payload.source_ids)

    posts_checked = 0
    comments_seen = 0
    created = 0
    skipped = 0
    examples: list[CommentDatasetItem] = []

    for source in sources:
        if created >= payload.max_items:
            break

        await fetch_real_posts(
            session=session,
            workspace_id=workspace_id,
            source=source,
            account=account,
            limit=payload.posts_per_source,
        )
        messages = await _latest_messages(session, workspace_id, source.id, payload.posts_per_source)
        source_ref = source_ref_from_fields(source.username, source.url, source.tg_id)

        for message in messages:
            if created >= payload.max_items:
                break
            if message.replies_count <= 0:
                skipped += 1
                continue
            posts_checked += 1
            try:
                comments = await fetch_post_comments_with_account(
                    session=session,
                    account=account,
                    source_ref=source_ref,
                    post_tg_message_id=message.tg_message_id,
                    limit=payload.comments_per_post,
                )
            except TelegramGatewayError:
                skipped += 1
                continue
            comments_seen += len(comments)

            for comment in comments:
                if created >= payload.max_items:
                    break
                if _should_skip_comment(comment.text, payload.min_comment_length):
                    skipped += 1
                    continue
                if await _dataset_item_exists(session, source.id, comment.tg_comment_message_id):
                    skipped += 1
                    continue

                item = CommentDatasetItem(
                    workspace_id=workspace_id,
                    source_id=source.id,
                    post_message_id=message.id,
                    tg_comment_message_id=comment.tg_comment_message_id,
                    author_tg_id=comment.author_tg_id,
                    post_text=message.text,
                    comment_text=comment.text,
                    quality_score=_score_comment(comment.text),
                    status="collected",
                    published_at=comment.published_at,
                    raw=comment.raw,
                )
                session.add(item)
                await session.flush()
                examples.append(item)
                created += 1

    await session.commit()
    return CommentDatasetCollectResponse(
        sources_checked=len(sources),
        posts_checked=posts_checked,
        comments_seen=comments_seen,
        created=created,
        skipped=skipped,
        examples=examples[:20],
    )


async def discover_and_collect_comment_dataset(
    session: AsyncSession,
    workspace_id: str,
    payload: CommentDatasetDiscoverCollectRequest,
) -> CommentDatasetDiscoverCollectResponse:
    account = await _resolve_account(session, workspace_id, payload.telegram_account_id)
    discovered = await discover_comment_sources_with_account(
        session,
        account,
        dialog_limit=payload.dialog_limit,
        max_sources=payload.max_sources,
    )

    sources: list[TelegramSource] = []
    for source_info in discovered:
        source = await _upsert_discovered_source(session, workspace_id, source_info)
        sources.append(source)
    await session.commit()

    collect_payload = CommentDatasetCollectRequest(
        telegram_account_id=account.id,
        source_ids=[source.id for source in sources],
        posts_per_source=payload.posts_per_source,
        comments_per_post=payload.comments_per_post,
        min_comment_length=payload.min_comment_length,
        max_items=payload.max_items,
    )
    collected = await collect_comment_dataset(session, workspace_id, collect_payload)
    return CommentDatasetDiscoverCollectResponse(
        sources_checked=collected.sources_checked,
        posts_checked=collected.posts_checked,
        comments_seen=collected.comments_seen,
        created=collected.created,
        skipped=collected.skipped,
        examples=collected.examples,
        discovered_sources=len(sources),
        sources=sources[:50],
    )


async def load_style_examples(
    session: AsyncSession,
    workspace_id: str,
    source_id: str | None,
    limit: int = 12,
) -> list[CommentDatasetItem]:
    base_filters = [
        CommentDatasetItem.workspace_id == workspace_id,
        CommentDatasetItem.status == "collected",
        CommentDatasetItem.language == "ru",
        CommentDatasetItem.quality_score >= 65,
    ]
    items: list[CommentDatasetItem] = []

    if source_id:
        result = await session.execute(
            select(CommentDatasetItem)
            .where(*base_filters, CommentDatasetItem.source_id == source_id)
            .order_by(CommentDatasetItem.quality_score.desc(), CommentDatasetItem.created_at.desc())
            .limit(limit)
        )
        items = list(result.scalars().all())

    if len(items) >= limit:
        return items

    seen_ids = {item.id for item in items}
    fallback_filters = list(base_filters)
    if seen_ids:
        fallback_filters.append(CommentDatasetItem.id.notin_(seen_ids))
    result = await session.execute(
        select(CommentDatasetItem)
        .where(*fallback_filters)
        .order_by(CommentDatasetItem.quality_score.desc(), CommentDatasetItem.created_at.desc())
        .limit(limit - len(items))
    )
    items.extend(result.scalars().all())
    return items


async def _upsert_discovered_source(
    session: AsyncSession,
    workspace_id: str,
    source_info: TelegramDialogSourceInfo,
) -> TelegramSource:
    tg_id = str(getattr(source_info, "tg_id", "") or "")
    username = getattr(source_info, "username", None)

    identity_filters = []
    if tg_id:
        identity_filters.append(TelegramSource.tg_id == tg_id)
    if username:
        identity_filters.append(TelegramSource.username == username)
    if not identity_filters:
        identity_filters.append(TelegramSource.title == getattr(source_info, "title", None))

    result = await session.execute(
        select(TelegramSource).where(TelegramSource.workspace_id == workspace_id, or_(*identity_filters)).limit(1)
    )
    source = result.scalar_one_or_none()
    if not source:
        source = TelegramSource(
            workspace_id=workspace_id,
            type=getattr(source_info, "source_type", "channel") or "channel",
        )
        session.add(source)

    source.type = getattr(source_info, "source_type", source.type) or source.type
    source.tg_id = tg_id or source.tg_id
    source.username = username
    source.title = getattr(source_info, "title", None) or username or source.title
    source.url = f"https://t.me/{username}" if username else source.url
    source.comments_enabled = bool(getattr(source_info, "comments_enabled", False))
    linked_id = getattr(source_info, "linked_discussion_tg_id", None)
    source.linked_discussion_tg_id = str(linked_id) if linked_id else None
    source.linked_discussion_username = getattr(source_info, "linked_discussion_username", None)
    source.access_status = "available"
    source.source_metadata = getattr(source_info, "metadata", {}) or {}
    await session.flush()
    return source


async def _resolve_account(
    session: AsyncSession,
    workspace_id: str,
    account_id: str | None,
) -> TelegramAccount:
    if account_id:
        account = await session.get(TelegramAccount, account_id)
        if account and account.workspace_id == workspace_id:
            return account
        raise ValueError("Telegram account not found")

    result = await session.execute(
        select(TelegramAccount)
        .where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.status.in_(["active", "warming", "new"]))
        .order_by(TelegramAccount.created_at.desc())
        .limit(1)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise ValueError("Telegram account is required")
    return account


async def _resolve_sources(
    session: AsyncSession,
    workspace_id: str,
    source_ids: list[str] | None,
) -> list[TelegramSource]:
    query = select(TelegramSource).where(TelegramSource.workspace_id == workspace_id)
    if source_ids:
        query = query.where(TelegramSource.id.in_(source_ids))
    else:
        query = query.where(TelegramSource.comments_enabled.is_(True))
    result = await session.execute(query.order_by(TelegramSource.created_at.desc()))
    sources = list(result.scalars().all())
    if not sources:
        raise ValueError("No Telegram sources with comments enabled")
    return sources


async def _latest_messages(
    session: AsyncSession,
    workspace_id: str,
    source_id: str,
    limit: int,
) -> list[TelegramMessage]:
    result = await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.workspace_id == workspace_id, TelegramMessage.source_id == source_id)
        .order_by(TelegramMessage.published_at.desc(), TelegramMessage.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _dataset_item_exists(session: AsyncSession, source_id: str, tg_comment_message_id: str) -> bool:
    result = await session.execute(
        select(CommentDatasetItem.id)
        .where(
            CommentDatasetItem.source_id == source_id,
            CommentDatasetItem.tg_comment_message_id == tg_comment_message_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _score_comment(text: str) -> int:
    compact = " ".join(text.split())
    score = 50
    if 35 <= len(compact) <= 220:
        score += 20
    if "?" in compact:
        score += 5
    if any(word in compact.lower() for word in ["ахах", "лол", "жиза", "кринж", "серьезно", "вот это"]):
        score += 5
    if len(compact) > 350:
        score -= 15
    if compact.count("!") > 1:
        score -= 10
    if _contains_link_or_mention(compact):
        score -= 25
    if _contains_unsafe_marker(compact):
        score -= 40
    return max(1, min(100, score))


def _should_skip_comment(text: str, min_comment_length: int) -> bool:
    compact = " ".join(text.split())
    if len(compact) < min_comment_length:
        return True
    if _contains_link_or_mention(compact):
        return True
    if _contains_unsafe_marker(compact):
        return True
    return False


def _contains_link_or_mention(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ["http://", "https://", "t.me/", "telegram.me/"])


def _contains_unsafe_marker(text: str) -> bool:
    lowered = text.lower()
    unsafe_markers = [
        "убить",
        "убью",
        "докс",
        "узкоглаз",
        "хохол",
        "чурк",
        "пидор",
        "педик",
        "нигер",
        "жид",
    ]
    return any(marker in lowered for marker in unsafe_markers)
