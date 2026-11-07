from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentUserDep, SessionDep, WorkspaceIdDep
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety.api import _authorize_account
from app.modules.workspaces.models import Workspace

router = APIRouter()


class ReadRequest(BaseModel):
    direct: bool = False
    peer: str | None = Field(default=None, max_length=12000)
    cursor: str | None = Field(default=None, max_length=12000)
    before: int = Field(default=0, ge=0)


class SendRequest(BaseModel):
    direct: bool = False
    peer: str = Field(max_length=12000)
    text: str = Field(default="", max_length=4096)
    button: str | None = Field(default=None, max_length=20000)
    request_id: UUID

    @model_validator(mode="after")
    def content(self):
        if bool(self.text.strip()) == bool(self.button):
            raise ValueError("Укажите текст или одну кнопку.")
        return self


async def authorize(db, workspace_id, actor, account_id):
    await _authorize_account(db, workspace_id, account_id)
    workspace = await db.get(Workspace, workspace_id)
    if not workspace or workspace.owner_user_id != actor:
        raise HTTPException(403, detail="Ручной Telegram доступен владельцу workspace.")
    return await db.get(TelegramAccount, account_id)


async def execute(method, *args):
    from app.modules.telegram_manual import service

    try:
        return await getattr(service, method)(*args)
    except TelegramGatewayError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        # Never expose a session, request payload or private Telegram URL in errors.
        raise HTTPException(
            409,
            detail={
                "code": "MANUAL_TELEGRAM_ERROR",
                "message": f"Не удалось выполнить действие ({type(exc).__name__}). Отправку не повторяйте до сверки.",
            },
        ) from exc


@router.post("/{account_id}/dialogs")
async def dialogs(
    account_id: str,
    payload: ReadRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
):
    account = await authorize(session, workspace_id, current_user.id, account_id)
    return await execute("dialogs", session, account, current_user.id, payload.cursor, payload.direct)


@router.post("/{account_id}/history")
async def history(
    account_id: str,
    payload: ReadRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
):
    account = await authorize(session, workspace_id, current_user.id, account_id)
    if not payload.peer:
        raise HTTPException(422, detail="Выберите диалог.")
    return await execute("history", session, account, current_user.id, payload.peer, payload.before, payload.direct)


@router.post("/{account_id}/send")
async def send(
    account_id: str,
    payload: SendRequest,
    session: SessionDep,
    workspace_id: WorkspaceIdDep,
    current_user: CurrentUserDep,
):
    account = await authorize(session, workspace_id, current_user.id, account_id)
    return await execute("send", session, account, current_user.id, payload)
