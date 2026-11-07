from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep
from app.modules.auth.service import ensure_free_plan, ensure_internal_full_access_for_user
from app.modules.plans.catalog import apply_plan_entitlements
from app.modules.workspaces.models import Workspace, WorkspaceMember
from app.modules.workspaces.schemas import WorkspaceCreate, WorkspaceResponse

router = APIRouter()


def _slugify(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    return slug or "workspace"


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(session: SessionDep, current_user: CurrentUserDep) -> list[Workspace]:
    await ensure_internal_full_access_for_user(session, current_user)
    await session.commit()
    result = await session.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id, WorkspaceMember.status == "active")
        .order_by(Workspace.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> Workspace:
    plan = await ensure_free_plan(session)
    workspace = Workspace(
        owner_user_id=current_user.id,
        name=payload.name,
        slug=f"{_slugify(payload.name)}-{current_user.id[:8]}",
        plan_id=plan.id,
    )
    session.add(workspace)
    await session.flush()
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=current_user.id, role="owner"))
    for entitlement in apply_plan_entitlements(workspace.id, plan):
        session.add(entitlement)
    await ensure_internal_full_access_for_user(session, current_user)
    await session.commit()
    await session.refresh(workspace)
    return workspace
