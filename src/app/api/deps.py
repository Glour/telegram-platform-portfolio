from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_session
from app.modules.auth.models import User
from app.modules.workspaces.models import WorkspaceMember

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User:
    try:
        payload = decode_access_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing subject")

    user = await session.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is not active")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_workspace_id(
    session: SessionDep,
    current_user: CurrentUserDep,
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> str:
    if not x_workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Workspace-Id header is required")

    result = await session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == x_workspace_id,
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.status == "active",
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace access denied")
    return x_workspace_id


WorkspaceIdDep = Annotated[str, Depends(get_workspace_id)]
