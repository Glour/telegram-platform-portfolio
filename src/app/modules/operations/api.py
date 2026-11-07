from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.modules.auth.service import is_internal_full_access_user
from app.modules.operations.models import OperationalEvent
from app.modules.operations.schemas import (
    OperationalEventResponse,
    OperationsHealthResponse,
    OperationsMonitorRunResponse,
)
from app.modules.operations.service import build_operations_health, record_operational_event, run_operations_monitor

router = APIRouter()


@router.get("/events", response_model=list[OperationalEventResponse])
async def list_operational_events(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
    severity: str | None = None,
    category: str | None = None,
    problem_only: bool = False,
    unresolved_only: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[OperationalEvent]:
    _ensure_internal_user(current_user)
    statement = select(OperationalEvent).where(
        (OperationalEvent.workspace_id == workspace_id) | (OperationalEvent.workspace_id.is_(None)),
    )
    if severity:
        statement = statement.where(OperationalEvent.severity == severity)
    if problem_only:
        statement = statement.where(OperationalEvent.severity.in_(["warning", "error", "critical"]))
    if unresolved_only:
        statement = statement.where(OperationalEvent.resolved_at.is_(None))
    if category:
        statement = statement.where(OperationalEvent.category == category)
    statement = statement.order_by(OperationalEvent.created_at.desc()).limit(limit)
    result = await session.execute(statement)
    return list(result.scalars().all())


@router.get("/health", response_model=OperationsHealthResponse)
async def get_operations_health(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> OperationsHealthResponse:
    _ensure_internal_user(current_user)
    return await build_operations_health(session, workspace_id)


@router.post("/monitor/run", response_model=OperationsMonitorRunResponse)
async def run_operations_monitor_now(
    session: SessionDep,
    current_user: CurrentUserDep,
) -> dict:
    _ensure_internal_user(current_user)
    return await run_operations_monitor(session)


@router.post("/alerts/test", response_model=OperationalEventResponse)
async def send_test_alert(
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> OperationalEvent:
    _ensure_internal_user(current_user)
    return await record_operational_event(
        session,
        workspace_id=workspace_id,
        severity="info",
        category="operations",
        event_type="test_alert",
        title="Тестовый алерт Demo Platform",
        message="Админка, Operation Events и Telegram-алерты подключены.",
        source="operations.api",
        dedupe_key=f"test_alert:{workspace_id}:{datetime.now(UTC).date().isoformat()}",
        payload={"workspace_id": workspace_id},
        alert=True,
        commit=True,
    )


def _ensure_internal_user(current_user) -> None:
    if not is_internal_full_access_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer admin access only")
