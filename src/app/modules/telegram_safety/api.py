from importlib import import_module
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.core.errors import AppError, forbidden, not_found
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety.models import TelegramOperation
from app.modules.telegram_safety.schemas import (
    OwnerAttestationRequest,
    SafetyAdmissionRequest,
    SafetyOperationResolutionRequest,
    SafetyStateResponse,
)
from app.modules.workspaces.models import Workspace, WorkspaceMember

router = APIRouter()


class SpamBotAppealStepRequest(BaseModel):
    action: Literal["start", "automatic", "button", "explanation"]
    text: str = Field(default="", max_length=2000)
    confirmed: bool = False
    previous_id: str | None = None


@router.get("/{account_id}/spambot-appeal")
async def get_spambot_appeal(
    account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep, current_user: CurrentUserDep
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    await _authorize_operator(session, workspace_id, current_user.id)
    from app.modules.telegram_safety.appeals import AUTOMATIC_EXPLANATION, latest_step, public_step, worker_available

    return {
        "step": public_step(await latest_step(session, account_id)),
        "worker_available": await worker_available(workspace_id),
        "automatic_explanation": AUTOMATIC_EXPLANATION,
    }


@router.post("/{account_id}/spambot-appeal", status_code=202)
async def request_spambot_appeal(
    account_id: str,
    payload: SpamBotAppealStepRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    from app.modules.telegram_safety.appeals import queue_step

    account = await session.get(TelegramAccount, account_id)
    try:
        step = await queue_step(
            session, account, current_user.id, payload.action, payload.text, payload.confirmed, payload.previous_id
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"step": step}


async def _authorize_account(session: SessionDep, workspace_id: str, account_id: str) -> None:
    account = await session.get(TelegramAccount, account_id)
    if not account or account.workspace_id != workspace_id:
        raise not_found("Telegram account not found")


async def _call_service(method: str, *args: object) -> dict:
    # Lazy loading keeps model discovery independent of the guarded Telegram runtime.
    service = import_module("app.modules.telegram_safety.service")
    try:
        return await getattr(service, method)(*args)
    except (AppError, TelegramGatewayError) as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc


async def _authorize_operator(session: SessionDep, workspace_id: str, actor_id: str) -> None:
    workspace = await session.get(Workspace, workspace_id)
    if workspace and workspace.owner_user_id == actor_id:
        return
    member = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == actor_id,
                WorkspaceMember.status == "active",
                WorkspaceMember.role.in_(["owner", "admin"]),
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise forbidden("Safety mutations require workspace owner or admin access")


@router.get("")
async def list_accounts(session: SessionDep, workspace_id: WorkspaceIdDep, current_user: CurrentUserDep) -> dict:
    from app.modules.telegram_safety.management import workspace_overview

    return await workspace_overview(session, workspace_id, current_user.id)


@router.post("/{account_id}/pause", response_model=SafetyStateResponse)
async def pause(
    account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep, current_user: CurrentUserDep
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    await _authorize_operator(session, workspace_id, current_user.id)
    return await _call_service(
        "pause_account", account_id, current_user.id, "Operator pause from Security account table"
    )


@router.get("/{account_id}", response_model=SafetyStateResponse)
async def get_state(account_id: str, session: SessionDep, workspace_id: WorkspaceIdDep) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    return await _call_service("get_safety_state", account_id)


@router.post("/{account_id}/resume", response_model=SafetyStateResponse)
async def resume(
    account_id: str,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    await _authorize_operator(session, workspace_id, current_user.id)
    return await _call_service("resume_account", account_id, current_user.id)


@router.post("/{account_id}/admit", response_model=SafetyStateResponse)
async def admit(
    account_id: str,
    payload: SafetyAdmissionRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    await _authorize_operator(session, workspace_id, current_user.id)
    return await _call_service("approve_account", account_id, current_user.id, payload.checked_at, payload.evidence)


@router.post("/{account_id}/operations/{operation_id}/resolve", response_model=SafetyStateResponse)
async def resolve(
    account_id: str,
    operation_id: str,
    payload: SafetyOperationResolutionRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    await _authorize_operator(session, workspace_id, current_user.id)
    operation = await session.get(TelegramOperation, operation_id)
    if not operation or operation.account_id != account_id:
        raise not_found("Telegram operation not found")
    return await _call_service(
        "resolve_operation",
        account_id,
        operation_id,
        current_user.id,
        payload.outcome,
        payload.evidence,
        payload.message_id,
    )


@router.post("/{account_id}/owner-attest", response_model=SafetyStateResponse)
async def owner_attest(
    account_id: str,
    payload: OwnerAttestationRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
) -> dict:
    await _authorize_account(session, workspace_id, account_id)
    return await _call_service("approve_owner_attestation", account_id, current_user.id, payload.evidence)
