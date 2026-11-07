# ruff: noqa: E501
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.errors import not_found
from app.modules.content_factory.models import (
    ContentFactory,
    ContentFactoryEvent,
    ContentFactoryKnowledgeItem,
    ContentFactoryRubric,
    ContentFactoryRun,
    ContentFactorySource,
    ContentPost,
)
from app.modules.content_factory.schemas import (
    ContentFactoryCreate,
    ContentFactoryDetailResponse,
    ContentFactoryEventResponse,
    ContentFactoryKnowledgeCreate,
    ContentFactoryKnowledgeResponse,
    ContentFactoryResponse,
    ContentFactoryRubricResponse,
    ContentFactoryRunRequest,
    ContentFactoryRunResponse,
    ContentFactoryRunResult,
    ContentFactorySiteContextRequest,
    ContentFactorySourceCreate,
    ContentFactorySourcePostResponse,
    ContentFactorySourceResponse,
    ContentFactoryUpdate,
    ContentPostResponse,
    ContentPostUpdate,
)
from app.modules.content_factory.service import (
    ContentFactoryError,
    add_knowledge,
    add_knowledge_files,
    add_source,
    approve_post,
    collect_site_context,
    create_factory,
    dedupe_content_factory_source_links,
    ensure_content_factory_source_posts,
    factory_summary_counts,
    knowledge_item_content_type,
    knowledge_item_filename,
    pause_factory,
    publish_post,
    regenerate_image,
    regenerate_text,
    resume_factory,
    run_factory,
    update_factory,
    update_post,
)
from app.modules.sources.models import TelegramMessage, TelegramSource

router = APIRouter()


@router.get("", response_model=list[ContentFactoryResponse])
async def list_content_factories(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[ContentFactoryResponse]:
    result = await session.execute(
        select(ContentFactory)
        .where(ContentFactory.workspace_id == workspace_id, ContentFactory.status != "archived")
        .order_by(ContentFactory.created_at.desc())
    )
    factories = result.scalars().all()
    counts = await factory_summary_counts(session, [factory.id for factory in factories])
    return [_factory_response(factory, counts.get(factory.id, {})) for factory in factories]


@router.post("", response_model=ContentFactoryResponse, status_code=status.HTTP_201_CREATED)
async def create_content_factory(
    payload: ContentFactoryCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> ContentFactoryResponse:
    factory = await create_factory(session, workspace_id=workspace_id, created_by=current_user.id, payload=payload)
    return _factory_response(factory, {})


@router.get("/{factory_id}", response_model=ContentFactoryDetailResponse)
async def get_content_factory(
    factory_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentFactoryDetailResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    counts = await factory_summary_counts(session, [factory.id])
    sources = dedupe_content_factory_source_links(
        (await session.execute(select(ContentFactorySource).where(ContentFactorySource.factory_id == factory.id)))
        .scalars()
        .all()
    )
    await ensure_content_factory_source_posts(session, factory, sources, None)
    source_ids = [item.source_id for item in sources if item.source_id]
    source_posts: list[ContentFactorySourcePostResponse] = []
    if source_ids:
        rows = (
            await session.execute(
                select(TelegramMessage, TelegramSource)
                .join(TelegramSource, TelegramSource.id == TelegramMessage.source_id)
                .where(
                    TelegramMessage.workspace_id == workspace_id,
                    TelegramMessage.source_id.in_(source_ids),
                )
                .order_by(TelegramMessage.published_at.desc().nullslast(), TelegramMessage.created_at.desc())
                .limit(50)
            )
        ).all()
        source_posts = [
            ContentFactorySourcePostResponse(
                id=message.id,
                source_id=message.source_id,
                source_title=source.title,
                source_username=source.username,
                source_url=source.url,
                tg_message_id=message.tg_message_id,
                text=message.text,
                published_at=message.published_at,
                views_count=message.views_count,
                forwards_count=message.forwards_count,
                replies_count=message.replies_count,
                created_at=message.created_at,
            )
            for message, source in rows
        ]
    knowledge = (
        (
            await session.execute(
                select(ContentFactoryKnowledgeItem).where(ContentFactoryKnowledgeItem.factory_id == factory.id)
            )
        )
        .scalars()
        .all()
    )
    rubrics = (
        (await session.execute(select(ContentFactoryRubric).where(ContentFactoryRubric.factory_id == factory.id)))
        .scalars()
        .all()
    )
    runs = (
        (
            await session.execute(
                select(ContentFactoryRun)
                .where(ContentFactoryRun.factory_id == factory.id)
                .order_by(ContentFactoryRun.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(ContentFactoryEvent)
                .where(ContentFactoryEvent.factory_id == factory.id)
                .order_by(ContentFactoryEvent.created_at.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    response = ContentFactoryDetailResponse(
        **_factory_response(factory, counts.get(factory.id, {})).model_dump(),
        sources=[ContentFactorySourceResponse.model_validate(item) for item in sources],
        knowledge_items=[ContentFactoryKnowledgeResponse.model_validate(item) for item in knowledge],
        rubrics=[ContentFactoryRubricResponse.model_validate(item) for item in rubrics],
        recent_runs=[ContentFactoryRunResponse.model_validate(item) for item in runs],
        recent_events=[ContentFactoryEventResponse.model_validate(item) for item in events],
        source_posts=source_posts,
    )
    return response


@router.patch("/{factory_id}", response_model=ContentFactoryResponse)
async def patch_content_factory(
    factory_id: str,
    payload: ContentFactoryUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ContentFactoryResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    updated = await update_factory(session, factory, payload)
    counts = await factory_summary_counts(session, [updated.id])
    return _factory_response(updated, counts.get(updated.id, {}))


@router.post("/{factory_id}/pause", response_model=ContentFactoryResponse)
async def pause_content_factory(
    factory_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentFactoryResponse:
    factory = await pause_factory(session, await _get_factory(session, workspace_id, factory_id))
    counts = await factory_summary_counts(session, [factory.id])
    return _factory_response(factory, counts.get(factory.id, {}))


@router.post("/{factory_id}/resume", response_model=ContentFactoryResponse)
async def resume_content_factory(
    factory_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentFactoryResponse:
    factory = await resume_factory(session, await _get_factory(session, workspace_id, factory_id))
    counts = await factory_summary_counts(session, [factory.id])
    return _factory_response(factory, counts.get(factory.id, {}))


@router.post("/{factory_id}/run", response_model=ContentFactoryRunResult)
async def run_content_factory(
    factory_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    payload: ContentFactoryRunRequest | None = None,
) -> ContentFactoryRunResult:
    factory = await _get_factory(session, workspace_id, factory_id)
    try:
        topic_prompt = payload.topic_prompt if payload else None
        run, post = await run_factory(session, factory, topic_prompt=topic_prompt)
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ContentFactoryRunResult(
        run=ContentFactoryRunResponse.model_validate(run),
        post=ContentPostResponse.model_validate(post) if post else None,
    )


@router.get("/{factory_id}/posts", response_model=list[ContentPostResponse])
async def list_content_posts(
    factory_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[ContentPostResponse]:
    factory = await _get_factory(session, workspace_id, factory_id)
    query = select(ContentPost).where(ContentPost.factory_id == factory.id)
    if status_filter:
        query = query.where(ContentPost.status == status_filter)
    result = await session.execute(query.order_by(ContentPost.created_at.desc()).limit(100))
    return [ContentPostResponse.model_validate(item) for item in result.scalars().all()]


@router.patch("/{factory_id}/posts/{post_id}", response_model=ContentPostResponse)
async def patch_content_post(
    factory_id: str,
    post_id: str,
    payload: ContentPostUpdate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ContentPostResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    return ContentPostResponse.model_validate(await update_post(session, factory, post, payload))


@router.post("/{factory_id}/posts/{post_id}/approve", response_model=ContentPostResponse)
async def approve_content_post(
    factory_id: str, post_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentPostResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    try:
        return ContentPostResponse.model_validate(await approve_post(session, factory, post))
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{factory_id}/posts/{post_id}/publish", response_model=ContentPostResponse)
async def publish_content_post(
    factory_id: str, post_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentPostResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    try:
        return ContentPostResponse.model_validate(await publish_post(session, factory, post))
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{factory_id}/posts/{post_id}/regenerate-text", response_model=ContentPostResponse)
async def regenerate_content_post_text(
    factory_id: str, post_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentPostResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    try:
        return ContentPostResponse.model_validate(await regenerate_text(session, factory, post))
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{factory_id}/posts/{post_id}/regenerate-image", response_model=ContentPostResponse)
async def regenerate_content_post_image(
    factory_id: str, post_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> ContentPostResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    try:
        return ContentPostResponse.model_validate(await regenerate_image(session, factory, post))
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{factory_id}/posts/{post_id}/image")
async def get_content_post_image(
    factory_id: str, post_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> FileResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    post = await _get_post(session, factory.id, post_id)
    if post.image_attachment_id:
        path = Path(post.image_attachment_id).resolve()
        if path.exists() and path.is_file():
            expected_root = Path(__file__).resolve().parents[4] / "artifacts" / "content-factory-images" / factory.id
            try:
                path.relative_to(expected_root.resolve())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Post image path is outside allowed storage"
                ) from exc
            return FileResponse(path, media_type=post.image_content_type or "image/jpeg", filename=f"{post.id}.jpg")
    if post.image_data:
        return Response(content=post.image_data, media_type=post.image_content_type or "image/jpeg")
    raise not_found("Post image not found")


@router.post("/{factory_id}/sources", response_model=ContentFactorySourceResponse, status_code=status.HTTP_201_CREATED)
async def add_content_factory_source(
    factory_id: str,
    payload: ContentFactorySourceCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ContentFactorySourceResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    return ContentFactorySourceResponse.model_validate(await add_source(session, factory, payload, workspace_id))


@router.delete("/{factory_id}/sources/{source_link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_content_factory_source(
    factory_id: str, source_link_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> None:
    factory = await _get_factory(session, workspace_id, factory_id)
    item = await session.get(ContentFactorySource, source_link_id)
    if not item or item.factory_id != factory.id:
        raise not_found("Source link not found")
    await session.delete(item)
    await session.commit()


@router.post(
    "/{factory_id}/knowledge", response_model=ContentFactoryKnowledgeResponse, status_code=status.HTTP_201_CREATED
)
async def add_content_factory_knowledge(
    factory_id: str,
    payload: ContentFactoryKnowledgeCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ContentFactoryKnowledgeResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    return ContentFactoryKnowledgeResponse.model_validate(await add_knowledge(session, factory, payload))


@router.post(
    "/{factory_id}/knowledge/files",
    response_model=list[ContentFactoryKnowledgeResponse],
    status_code=status.HTTP_201_CREATED,
)
async def add_content_factory_knowledge_files(
    factory_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    files: Annotated[list[UploadFile], File(...)],
) -> list[ContentFactoryKnowledgeResponse]:
    factory = await _get_factory(session, workspace_id, factory_id)
    try:
        items = await add_knowledge_files(session, factory, files)
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [ContentFactoryKnowledgeResponse.model_validate(item) for item in items]


@router.post("/{factory_id}/site-context", response_model=ContentFactoryKnowledgeResponse)
async def collect_content_factory_site_context(
    factory_id: str,
    payload: ContentFactorySiteContextRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> ContentFactoryKnowledgeResponse:
    factory = await _get_factory(session, workspace_id, factory_id)
    try:
        item = await collect_site_context(session, factory, site_url=payload.site_url, max_pages=payload.max_pages)
    except ContentFactoryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ContentFactoryKnowledgeResponse.model_validate(item)


@router.get("/{factory_id}/knowledge/{item_id}/file")
async def get_content_factory_knowledge_file(
    factory_id: str, item_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> Response:
    factory = await _get_factory(session, workspace_id, factory_id)
    item = await session.get(ContentFactoryKnowledgeItem, item_id)
    if not item or item.factory_id != factory.id:
        raise not_found("Knowledge file not found")
    filename = knowledge_item_filename(item)
    content_type = knowledge_item_content_type(item)
    if item.file_path:
        path = Path(item.file_path).resolve()
        if path.exists() and path.is_file():
            expected_root = Path(__file__).resolve().parents[4] / "artifacts" / "content-factory-knowledge" / factory.id
            try:
                path.relative_to(expected_root.resolve())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Knowledge file path is outside allowed storage"
                ) from exc
            return FileResponse(path, media_type=content_type, filename=filename)
    if item.file_data is not None:
        return Response(
            content=bytes(item.file_data),
            media_type=content_type,
            headers={"Content-Disposition": _download_content_disposition(filename)},
        )
    raise not_found("Knowledge file not found")


@router.delete("/{factory_id}/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_content_factory_knowledge(
    factory_id: str, item_id: str, session: SessionDep, workspace_id: WorkspaceIdDep
) -> None:
    factory = await _get_factory(session, workspace_id, factory_id)
    item = await session.get(ContentFactoryKnowledgeItem, item_id)
    if not item or item.factory_id != factory.id:
        raise not_found("Knowledge item not found")
    await session.delete(item)
    await session.commit()


async def _get_factory(session: SessionDep, workspace_id: str, factory_id: str) -> ContentFactory:
    factory = await session.get(ContentFactory, factory_id)
    if not factory or factory.workspace_id != workspace_id or factory.status == "archived":
        raise not_found("Content factory not found")
    return factory


async def _get_post(session: SessionDep, factory_id: str, post_id: str) -> ContentPost:
    post = await session.get(ContentPost, post_id)
    if not post or post.factory_id != factory_id:
        raise not_found("Content post not found")
    return post


def _factory_response(factory: ContentFactory, counts: dict) -> ContentFactoryResponse:
    return ContentFactoryResponse(
        id=factory.id,
        workspace_id=factory.workspace_id,
        name=factory.name,
        status=factory.status,
        target_type=factory.target_type,
        target_ref=factory.target_ref,
        publisher_account_id=factory.publisher_account_id,
        mode=factory.mode,
        schedule_config=factory.schedule_config,
        content_config=factory.content_config,
        image_config=factory.image_config,
        quality_config=factory.quality_config,
        drafts_count=int(counts.get("drafts_count", 0) or 0),
        published_24h=int(counts.get("published_24h", 0) or 0),
        published_7d=int(counts.get("published_7d", 0) or 0),
        errors_count=int(counts.get("errors_count", 0) or 0),
        next_run_at=factory.next_run_at,
        last_successful_run_at=counts.get("last_successful_run_at"),
        created_at=factory.created_at,
        updated_at=factory.updated_at,
    )


def _download_content_disposition(filename: str) -> str:
    ascii_filename = "".join(char if char.isascii() and char not in {'"', "\\"} else "_" for char in filename).strip()
    ascii_filename = ascii_filename or "knowledge-file"
    return f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"
