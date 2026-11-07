from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.errors import not_found
from app.modules.ai.models import AiProfile, CommentPromptTemplate
from app.modules.ai.schemas import (
    AiGenerationResponse,
    AiProfileCreate,
    AiProfileResponse,
    CommentPreviewRequest,
    CommentPromptCreate,
    CommentPromptResponse,
)
from app.modules.ai.service import AiProviderError, create_generation

router = APIRouter()


@router.post("/profiles", response_model=AiProfileResponse, status_code=201)
async def create_profile(payload: AiProfileCreate, session: SessionDep, workspace_id: WorkspaceIdDep) -> AiProfile:
    profile = AiProfile(workspace_id=workspace_id, **payload.model_dump())
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


@router.post("/comment-prompts", response_model=CommentPromptResponse, status_code=201)
async def create_prompt(
    payload: CommentPromptCreate,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> CommentPromptTemplate:
    prompt = CommentPromptTemplate(workspace_id=workspace_id, created_by=current_user.id, **payload.model_dump())
    session.add(prompt)
    await session.commit()
    await session.refresh(prompt)
    return prompt


@router.get("/comment-prompts", response_model=list[CommentPromptResponse])
async def list_prompts(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[CommentPromptTemplate]:
    result = await session.execute(
        select(CommentPromptTemplate)
        .where(CommentPromptTemplate.workspace_id == workspace_id, CommentPromptTemplate.status == "active")
        .order_by(CommentPromptTemplate.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/comments/generate-preview", response_model=AiGenerationResponse)
async def generate_preview(
    payload: CommentPreviewRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> object:
    prompt = await session.get(CommentPromptTemplate, payload.comment_prompt_template_id)
    if not prompt or prompt.workspace_id != workspace_id:
        raise not_found("Comment prompt not found")
    try:
        return await create_generation(
            session=session,
            workspace_id=workspace_id,
            prompt=prompt,
            post_text=payload.post_text,
            channel_title=payload.channel_title,
            profile_id=payload.profile_id,
        )
    except AiProviderError as exc:
        raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail=str(exc)) from exc
