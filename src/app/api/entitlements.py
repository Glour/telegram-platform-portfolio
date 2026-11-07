from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import SessionDep, WorkspaceIdDep
from app.modules.auth.service import expire_workspace_trial_if_needed
from app.modules.plans.models import Entitlement


def require_entitlement(feature_code: str) -> Callable:
    async def _dependency(session: SessionDep, workspace_id: WorkspaceIdDep) -> None:
        await expire_workspace_trial_if_needed(session, workspace_id)

        result = await session.execute(
            select(Entitlement).where(
                Entitlement.workspace_id == workspace_id,
                Entitlement.feature_code == feature_code,
            )
        )
        entitlement = result.scalar_one_or_none()
        if not entitlement or entitlement.limit_value <= 0:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Модуль не подключен: {feature_code}",
            )

    return Depends(_dependency)
