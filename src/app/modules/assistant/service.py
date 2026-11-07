from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.service import AiProviderError, generate_text_response
from app.modules.assistant.models import (
    AssistantConversation,
    AssistantFeedback,
    AssistantMessage,
    AssistantSupportRequest,
)
from app.modules.assistant.prompts import (
    LOW_CONFIDENCE_ANSWER,
    SYSTEM_PROMPT,
    build_context_prompt,
    citations_from_chunks,
    is_how_to_question,
)
from app.modules.assistant.retriever import retrieve_relevant_chunks
from app.modules.assistant.schemas import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConversationCreate,
    AssistantFeedbackRequest,
    AssistantSupportRequestCreate,
)
from app.modules.auth.models import User
from app.modules.contact.schemas import ContactRequest
from app.modules.contact.service import send_contact_request
from app.modules.operations.alerts import send_telegram_alert

WELCOME_MESSAGE = "Привет! Я помогу разобраться с demo-platform, модулями, тарифами и безопасным запуском."
SUGGESTED_QUESTIONS = [
    "Как запустить нейрокомментинг?",
    "Как импортировать Telegram-аккаунты?",
    "Какие лимиты поставить на старте?",
]


async def create_conversation(
    session: AsyncSession,
    payload: AssistantConversationCreate,
    *,
    user: User | None = None,
    workspace_id: str | None = None,
) -> AssistantConversation:
    conversation = AssistantConversation(
        workspace_id=workspace_id,
        user_id=user.id if user else None,
        visitor_id=payload.visitor_id,
        surface=payload.surface,
        initial_url=payload.initial_url,
        last_message_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def load_conversation_for_actor(
    session: AsyncSession,
    conversation_id: str,
    *,
    user: User | None,
    workspace_id: str | None,
    visitor_id: str | None,
) -> AssistantConversation:
    conversation = await session.get(AssistantConversation, conversation_id)
    if not conversation or conversation.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.user_id:
        if not user or conversation.user_id != user.id or conversation.workspace_id != workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conversation access denied")
    elif conversation.visitor_id and visitor_id and conversation.visitor_id != visitor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conversation access denied")
    return conversation


async def ensure_assistant_rate_limit(
    session: AsyncSession,
    *,
    request: Request,
    user: User | None,
    visitor_id: str | None,
) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    ip = request.client.host if request.client else "unknown"
    identity_limit = 50 if user else 10
    identity_filter = (
        AssistantConversation.user_id == user.id if user else AssistantConversation.visitor_id == visitor_id
    )
    if user or visitor_id:
        identity_count = await session.scalar(
            select(func.count(AssistantMessage.id))
            .join(AssistantConversation, AssistantConversation.id == AssistantMessage.conversation_id)
            .where(identity_filter)
            .where(AssistantMessage.role == "user")
            .where(AssistantMessage.created_at >= since)
        )
        if int(identity_count or 0) >= identity_limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Assistant daily limit exceeded")

    ip_count = await session.scalar(
        select(func.count(AssistantMessage.id))
        .where(AssistantMessage.role == "user")
        .where(AssistantMessage.created_at >= since)
        .where(AssistantMessage.ip_address == ip)
    )
    if int(ip_count or 0) >= 60:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Assistant IP daily limit exceeded")


async def answer_message(
    session: AsyncSession,
    payload: AssistantChatRequest,
    *,
    request: Request,
    user: User | None,
    workspace_id: str | None,
) -> AssistantChatResponse:
    visitor_id = payload.visitor_id
    conversation = await load_conversation_for_actor(
        session,
        payload.conversation_id,
        user=user,
        workspace_id=workspace_id,
        visitor_id=visitor_id,
    )
    await ensure_assistant_rate_limit(session, request=request, user=user, visitor_id=visitor_id)

    now = datetime.now(UTC)
    user_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content=payload.message.strip(),
        retrieval_debug={
            "surface": payload.surface,
            "page_url": payload.page_url,
            "ip": request.client.host if request.client else None,
        },
        ip_address=request.client.host if request.client else None,
    )
    session.add(user_message)
    visibility = ["public", "authenticated"] if user else ["public"]
    retrieval = await retrieve_relevant_chunks(session, question=payload.message, visibility=visibility, limit=10)
    detailed_answer = is_how_to_question(payload.message)

    citations = citations_from_chunks(retrieval.chunks[:6])
    token_usage: dict = {}
    model: str | None = None
    if retrieval.confidence < 0.35 or not retrieval.chunks:
        answer = LOW_CONFIDENCE_ANSWER
    else:
        try:
            answer, token_usage = await generate_text_response(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_context_prompt(
                    payload.message,
                    retrieval.chunks[:8],
                    detailed=detailed_answer,
                ),
                temperature=0.35,
                max_output_tokens=1400 if detailed_answer else 900,
            )
            answer = (answer or "").strip() or LOW_CONFIDENCE_ANSWER
            model = str(token_usage.get("model") or "") or None
        except AiProviderError:
            answer = (
                "Я нашел релевантные материалы, но сейчас не смог получить ответ от AI-модели. "
                "Попробуйте еще раз чуть позже."
            )

    assistant_message = AssistantMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=answer[:3200] if detailed_answer else answer[:1800],
        citations=[item.model_dump() for item in citations],
        retrieval_debug={
            "confidence": retrieval.confidence,
            "chunk_ids": [chunk.id for chunk, _score in retrieval.chunks[:10]],
        },
        token_usage=token_usage,
        model=model,
    )
    conversation.surface = payload.surface
    conversation.last_message_at = now
    session.add(assistant_message)
    await session.flush()
    return AssistantChatResponse(
        message_id=assistant_message.id,
        answer=assistant_message.content,
        citations=citations,
        suggested_questions=SUGGESTED_QUESTIONS,
    )


async def create_feedback(
    session: AsyncSession,
    message_id: str,
    payload: AssistantFeedbackRequest,
) -> AssistantFeedback:
    message = await session.get(AssistantMessage, message_id)
    if not message or message.role != "assistant":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant message not found")
    feedback = AssistantFeedback(
        message_id=message.id,
        conversation_id=message.conversation_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    session.add(feedback)
    await session.flush()
    return feedback


async def create_support_request(
    session: AsyncSession,
    payload: AssistantSupportRequestCreate,
    *,
    request: Request,
    user: User | None,
    workspace_id: str | None,
) -> AssistantSupportRequest:
    conversation = await load_conversation_for_actor(
        session,
        payload.conversation_id,
        user=user,
        workspace_id=workspace_id,
        visitor_id=payload.visitor_id,
    )
    transcript = await _conversation_transcript(session, conversation.id)
    support_request = AssistantSupportRequest(
        conversation_id=conversation.id,
        workspace_id=conversation.workspace_id or workspace_id,
        user_id=conversation.user_id or (user.id if user else None),
        visitor_id=conversation.visitor_id or payload.visitor_id,
        surface=payload.surface,
        contact=payload.contact.strip(),
        message=payload.message.strip(),
        page_url=payload.page_url,
        transcript=transcript,
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
    )
    session.add(support_request)
    await session.flush()
    await _notify_support_request(support_request, user=user)
    return support_request


async def _conversation_transcript(session: AsyncSession, conversation_id: str) -> list[dict[str, str]]:
    messages = (
        (
            await session.execute(
                select(AssistantMessage)
                .where(AssistantMessage.conversation_id == conversation_id)
                .where(AssistantMessage.role.in_(["user", "assistant"]))
                .order_by(AssistantMessage.created_at.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "role": message.role,
            "content": message.content[:1200],
            "created_at": message.created_at.isoformat(),
        }
        for message in reversed(messages)
    ]


async def _notify_support_request(support_request: AssistantSupportRequest, *, user: User | None) -> None:
    transcript_tail = "\n".join(f"{item['role']}: {item['content'][:500]}" for item in support_request.transcript[-4:])
    message = "\n".join(
        [
            f"Контакт: {support_request.contact}",
            f"Пользователь: {user.email if user else support_request.visitor_id or 'anonymous'}",
            f"Страница: {support_request.page_url or '-'}",
            "",
            "Запрос:",
            support_request.message,
            "",
            "Последние сообщения:",
            transcript_tail or "-",
        ]
    )
    try:
        await send_telegram_alert(
            severity="info",
            title="Новая заявка в поддержку demo-platform",
            message=message,
            payload={
                "support_request_id": support_request.id,
                "conversation_id": support_request.conversation_id,
                "workspace_id": support_request.workspace_id,
                "contact": support_request.contact,
            },
        )
    except Exception:
        pass

    try:
        await send_contact_request(
            ContactRequest(
                name=(user.full_name or user.email) if user else "AI-помощник demo-platform",
                contact=support_request.contact,
                page_url=support_request.page_url,
                message=message,
            )
        )
    except Exception:
        pass
