from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep
from app.core.security import decode_access_token
from app.modules.assistant.ingestion import reindex_knowledge
from app.modules.assistant.models import AssistantKnowledgeSource, AssistantMessage
from app.modules.assistant.retriever import retrieve_relevant_chunks
from app.modules.assistant.schemas import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConversationCreate,
    AssistantConversationCreateResponse,
    AssistantFeedbackRequest,
    AssistantKnowledgeSearchResponse,
    AssistantKnowledgeSourceResponse,
    AssistantMessageResponse,
    AssistantReindexResponse,
    AssistantSupportRequestCreate,
    AssistantSupportRequestResponse,
)
from app.modules.assistant.service import (
    WELCOME_MESSAGE,
    answer_message,
    create_conversation,
    create_feedback,
    create_support_request,
    load_conversation_for_actor,
)
from app.modules.auth.models import User
from app.modules.auth.service import is_internal_full_access_user
from app.modules.workspaces.models import WorkspaceMember

router = APIRouter()
optional_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def require_assistant_admin(current_user: CurrentUserDep) -> User:
    if not is_internal_full_access_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer admin access only")
    return current_user


async def optional_user(
    session: SessionDep,
    token: Annotated[str | None, Depends(optional_oauth2)] = None,
) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = await session.get(User, user_id)
    if not user or user.status != "active":
        return None
    return user


async def optional_workspace_id(
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)],
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> str | None:
    if not user or not x_workspace_id:
        return None
    result = await session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == x_workspace_id,
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.status == "active",
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace access denied")
    return x_workspace_id


@router.post("/conversations", response_model=AssistantConversationCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_assistant_conversation(
    payload: AssistantConversationCreate,
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)],
    workspace_id: Annotated[str | None, Depends(optional_workspace_id)],
) -> AssistantConversationCreateResponse:
    conversation = await create_conversation(session, payload, user=user, workspace_id=workspace_id)
    await session.commit()
    return AssistantConversationCreateResponse(conversation_id=conversation.id, welcome_message=WELCOME_MESSAGE)


@router.get("/conversations/{conversation_id}/messages", response_model=list[AssistantMessageResponse])
async def list_assistant_messages(
    conversation_id: str,
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)],
    workspace_id: Annotated[str | None, Depends(optional_workspace_id)],
    visitor_id: str | None = Query(default=None, max_length=120),
) -> list[AssistantMessageResponse]:
    conversation = await load_conversation_for_actor(
        session,
        conversation_id,
        user=user,
        workspace_id=workspace_id,
        visitor_id=visitor_id,
    )
    messages = (
        (
            await session.execute(
                select(AssistantMessage)
                .where(AssistantMessage.conversation_id == conversation.id)
                .where(AssistantMessage.role.in_(["user", "assistant"]))
                .order_by(AssistantMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        AssistantMessageResponse(
            id=message.id,
            role=message.role,
            content=message.content,
            citations=message.citations or [],
            created_at=message.created_at.isoformat(),
        )
        for message in messages
    ]


@router.post("/chat", response_model=AssistantChatResponse)
async def assistant_chat(
    payload: AssistantChatRequest,
    request: Request,
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)],
    workspace_id: Annotated[str | None, Depends(optional_workspace_id)],
) -> AssistantChatResponse:
    response = await answer_message(session, payload, request=request, user=user, workspace_id=workspace_id)
    await session.commit()
    return response


@router.post(
    "/support-requests",
    response_model=AssistantSupportRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assistant_support_request(
    payload: AssistantSupportRequestCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)],
    workspace_id: Annotated[str | None, Depends(optional_workspace_id)],
) -> AssistantSupportRequestResponse:
    support_request = await create_support_request(
        session,
        payload,
        request=request,
        user=user,
        workspace_id=workspace_id,
    )
    await session.commit()
    return AssistantSupportRequestResponse(
        request_id=support_request.id,
        status=support_request.status,
        message="Запрос передан в поддержку. Мы свяжемся по указанному контакту.",
    )


@router.post("/messages/{message_id}/feedback")
async def assistant_feedback(
    message_id: str,
    payload: AssistantFeedbackRequest,
    session: SessionDep,
) -> dict[str, str]:
    await create_feedback(session, message_id, payload)
    await session.commit()
    return {"status": "ok"}


@router.post("/knowledge/reindex", response_model=AssistantReindexResponse)
async def assistant_reindex(
    session: SessionDep,
    _admin: Annotated[User, Depends(require_assistant_admin)],
    source: str = Query(default="all", pattern="^(all|docs|help|pricing)$"),
) -> AssistantReindexResponse:
    sources, chunks = await reindex_knowledge(session, source)
    await session.commit()
    return AssistantReindexResponse(status="ok", sources=sources, chunks=chunks)


@router.get("/knowledge/sources", response_model=list[AssistantKnowledgeSourceResponse])
async def assistant_sources(
    session: SessionDep,
    _admin: Annotated[User, Depends(require_assistant_admin)],
) -> list[AssistantKnowledgeSourceResponse]:
    sources = (
        (await session.execute(select(AssistantKnowledgeSource).order_by(AssistantKnowledgeSource.title.asc())))
        .scalars()
        .all()
    )
    return [
        AssistantKnowledgeSourceResponse(
            id=source.id,
            source_type=source.source_type,
            title=source.title,
            path=source.path,
            public_url=source.public_url,
            visibility=source.visibility,
            checksum=source.checksum,
            status=source.status,
            last_indexed_at=source.last_indexed_at.isoformat() if source.last_indexed_at else None,
        )
        for source in sources
    ]


@router.get("/knowledge/search", response_model=list[AssistantKnowledgeSearchResponse])
async def assistant_search(
    session: SessionDep,
    _admin: Annotated[User, Depends(require_assistant_admin)],
    q: str = Query(min_length=2, max_length=500),
    authenticated: bool = False,
) -> list[AssistantKnowledgeSearchResponse]:
    visibility = ["public", "authenticated"] if authenticated else ["public"]
    result = await retrieve_relevant_chunks(session, question=q, visibility=visibility, limit=10)
    return [
        AssistantKnowledgeSearchResponse(
            chunk_id=chunk.id,
            source_id=chunk.source_id,
            title=chunk.title,
            url=(chunk.chunk_metadata or {}).get("public_url"),
            content=chunk.content,
            score=score,
        )
        for chunk, score in result.chunks
    ]
