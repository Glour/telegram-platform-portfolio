from app.modules.telegram_gateway.service import TelegramGatewayError
from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.core.errors import bad_request
from app.modules.datasets.models import CommentDatasetItem
from app.modules.datasets.schemas import (
    CommentDatasetCollectRequest,
    CommentDatasetCollectResponse,
    CommentDatasetDiscoverCollectRequest,
    CommentDatasetDiscoverCollectResponse,
    CommentDatasetItemResponse,
)
from app.modules.datasets.service import collect_comment_dataset, discover_and_collect_comment_dataset

router = APIRouter()


@router.get("/comment-examples", response_model=list[CommentDatasetItemResponse])
async def list_comment_examples(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    limit: int = 50,
) -> list[CommentDatasetItem]:
    result = await session.execute(
        select(CommentDatasetItem)
        .where(CommentDatasetItem.workspace_id == workspace_id)
        .order_by(CommentDatasetItem.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return list(result.scalars().all())


@router.post("/comment-examples/collect", response_model=CommentDatasetCollectResponse, status_code=201)
async def collect_comment_examples(
    payload: CommentDatasetCollectRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CommentDatasetCollectResponse:
    try:
        return await collect_comment_dataset(session, workspace_id, payload)
    except TelegramGatewayError as exc:
        raise bad_request(f"{exc.code}: {exc}") from exc
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post(
    "/comment-examples/discover-and-collect",
    response_model=CommentDatasetDiscoverCollectResponse,
    status_code=201,
)
async def discover_and_collect_comment_examples(
    payload: CommentDatasetDiscoverCollectRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
) -> CommentDatasetDiscoverCollectResponse:
    try:
        return await discover_and_collect_comment_dataset(session, workspace_id, payload)
    except TelegramGatewayError as exc:
        raise bad_request(f"{exc.code}: {exc}") from exc
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
