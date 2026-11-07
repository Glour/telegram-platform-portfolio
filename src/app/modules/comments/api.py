from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.modules.comments.models import TelegramComment
from app.modules.comments.schemas import TelegramCommentResponse

router = APIRouter()


@router.get("", response_model=list[TelegramCommentResponse])
async def list_comments(session: SessionDep, workspace_id: WorkspaceIdDep) -> list[TelegramComment]:
    result = await session.execute(
        select(TelegramComment)
        .where(TelegramComment.workspace_id == workspace_id)
        .order_by(TelegramComment.created_at.desc())
        .limit(100)
    )
    return list(result.scalars().all())
