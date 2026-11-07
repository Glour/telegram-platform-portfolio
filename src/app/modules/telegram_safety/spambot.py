"""Explicit, one-shot official-bot diagnostics. Never releases campaign holds."""

from datetime import timedelta

from sqlalchemy import func, select, text
from telethon.sessions import StringSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
from app.modules.telegram_safety.service import (
    GuardedTelegramClient,
    _bootstrap_correction_kind,
    aware,
    fingerprint,
    flood_seconds,
    unwrap_request,
    utcnow,
)
from app.modules.workspaces.models import Workspace

PERMIT_METHOD = "operator.spambot_diagnostic"
PREFIX = "diagnostic.spambot."
CAPS = {"GetConfigRequest": 2, "ResolveUsernameRequest": 1, "SendMessageRequest": 1, "GetHistoryRequest": 2}


def diagnostic_execution_allowed(settings, permit_method: str) -> bool:
    return bool(
        settings.telegram_operations_enabled
        or (permit_method == PERMIT_METHOD + ".appeal" and getattr(settings, "spambot_appeals_enabled", False))
    )


def parse_spambot_reply(value: str) -> str:
    text_value = " ".join(value.casefold().replace("ё", "е").split())
    if any(
        s in text_value
        for s in (
            "no limits are currently applied",
            "свободен от каких-либо ограничений",
            "свободен от ограничений",
            "на ваш аккаунт не наложено никаких ограничений",
        )
    ):
        return "clear"
    if any(
        s in text_value
        for s in ("your account was blocked", "your account has been blocked", "ваш аккаунт заблокирован")
    ):
        return "blocked"
    if any(
        s in text_value
        for s in (
            "while the account is limited",
            "while your account is limited",
            "пока аккаунт ограничен",
            "пока ваш аккаунт ограничен",
        )
    ):
        return "limited"
    return "unknown"


async def authorize_diagnostic(account_id: str, actor_id: str) -> str:
    async with AsyncSessionLocal.begin() as db:
        state = await db.get(TelegramSafetyState, account_id, with_for_update=True)
        account = await db.get(TelegramAccount, account_id)
        workspace = await db.get(Workspace, account.workspace_id) if account else None
        if not workspace or workspace.owner_user_id != actor_id or not state or not state.manual_hold:
            raise TelegramGatewayError(
                "DIAGNOSTIC_OWNER_REQUIRED", "Owner authorization and an existing hold required."
            )
        if state.wait_until and aware(state.wait_until) > utcnow():
            raise TelegramGatewayError("SAFETY_WAIT_ACTIVE", "Original Telegram wait remains active.")
        previous = await db.scalar(
            select(TelegramOperation.id)
            .where(
                TelegramOperation.account_id == account_id,
                TelegramOperation.method == PERMIT_METHOD,
                TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
            )
            .limit(1)
        )
        if previous:
            raise TelegramGatewayError("DIAGNOSTIC_ALREADY_REQUESTED", "One explicit diagnostic per rolling 24 hours.")
        permit = TelegramOperation(
            account_id=account_id,
            method=PERMIT_METHOD,
            target=actor_id,
            outbound=False,
            outcome="approved",
            created_at=utcnow(),
        )
        db.add(permit)
        await db.flush()
        return permit.id


async def repair_precommand_diagnostic(account_id: str, actor_id: str) -> str:
    """One audited reconnect after a locally rejected self-profile RPC, before bot lookup/send."""
    async with AsyncSessionLocal.begin() as db:
        state = await db.get(TelegramSafetyState, account_id, with_for_update=True)
        account = await db.get(TelegramAccount, account_id)
        workspace = await db.get(Workspace, account.workspace_id) if account else None
        permits = list(
            (
                await db.scalars(
                    select(TelegramOperation).where(
                        TelegramOperation.account_id == account_id,
                        TelegramOperation.method == PERMIT_METHOD,
                        TelegramOperation.created_at >= utcnow() - timedelta(minutes=10),
                    )
                )
            ).all()
        )
        ops = list(
            (
                await db.scalars(
                    select(TelegramOperation).where(
                        TelegramOperation.account_id == account_id,
                        TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                        TelegramOperation.method.like(PREFIX + "%"),
                    )
                )
            ).all()
        )
        if (
            not workspace
            or workspace.owner_user_id != actor_id
            or not state
            or not state.manual_hold
            or len(permits) != 1
            or permits[0].target != actor_id
            or permits[0].outcome != "completed"
            or not ops
            or any(
                o.outcome != "accepted"
                or o.outbound
                or o.method not in {PREFIX + "GetConfigRequest", PREFIX + "bootstrap_correction"}
                for o in ops
            )
            or sum(o.method == PREFIX + "GetConfigRequest" for o in ops) != 1
        ):
            raise TelegramGatewayError(
                "DIAGNOSTIC_REPAIR_FORBIDDEN", "Only one confirmed pre-command local repair allowed."
            )
        prior = await db.scalar(
            select(TelegramOperation.id).where(
                TelegramOperation.account_id == account_id,
                TelegramOperation.method == "operator.spambot_precommand_repair",
                TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
            )
        )
        if prior:
            raise TelegramGatewayError("DIAGNOSTIC_REPAIR_FORBIDDEN", "Repair already consumed.")
        permits[0].outcome = "approved"
        permits[0].finished_at = None
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.spambot_precommand_repair",
                outbound=False,
                target=actor_id,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return permits[0].id


class SpamBotDiagnosticClient(GuardedTelegramClient):
    def __init__(
        self,
        stored: StringSession,
        api_id: int,
        api_hash: str,
        *,
        account_id: str,
        permit_id: str,
        actor_id: str,
        proxy,
        allow_direct: bool = False,
        appeal_command: str | None = None,
        **kwargs,
    ):
        if (proxy is None and not allow_direct) or not stored.auth_key:
            raise TelegramGatewayError("DIAGNOSTIC_INTAKE_INVALID", "Existing session and assigned proxy required.")
        super().__init__(
            stored,
            api_id,
            api_hash,
            account_id=account_id,
            lock_fingerprint=fingerprint(stored.auth_key.key),
            proxy=proxy,
            **kwargs,
        )
        self.permit_id = permit_id
        self.actor_id = actor_id
        self.permit_method = PERMIT_METHOD + ".appeal" if appeal_command is not None else PERMIT_METHOD
        self.operation_prefix = f"appeal.spambot.{permit_id}." if appeal_command is not None else PREFIX
        self.allowed_command = appeal_command if appeal_command is not None else "/start"
        self.rpc_caps = {**CAPS, "GetHistoryRequest": 3} if appeal_command is not None else CAPS
        self.diagnostic_direct = proxy is None
        self.bot_id = None
        self.start_message_id = None

    async def get_me(self, input_peer=False):
        # Telethon connect otherwise requests GetUsers/GetState. Diagnosis needs neither.
        return None

    async def _permit(self, db):
        permit = await db.get(TelegramOperation, self.permit_id)
        account = await db.get(TelegramAccount, self.safety_account_id)
        workspace = await db.get(Workspace, account.workspace_id) if account else None
        state = await db.get(TelegramSafetyState, self.safety_account_id)
        if self.diagnostic_direct and (not account or account.proxy_required or account.proxy_id is not None):
            raise TelegramGatewayError(
                "DIAGNOSTIC_INTAKE_INVALID", "Direct diagnosis requires explicit proxy-free account configuration."
            )
        if (
            not permit
            or permit.account_id != self.safety_account_id
            or permit.method != self.permit_method
            or permit.outcome != "approved"
            or permit.target != self.actor_id
            or not workspace
            or workspace.owner_user_id != self.actor_id
            or not state
            or not state.manual_hold
            or not utcnow() - timedelta(minutes=10) <= aware(permit.created_at) <= utcnow()
        ):
            raise TelegramGatewayError("DIAGNOSTIC_PERMIT_INVALID", "Diagnostic permit expired or identity changed.")
        if state.wait_until and aware(state.wait_until) > utcnow():
            raise TelegramGatewayError("SAFETY_WAIT_ACTIVE", "Original Telegram wait remains active.")
        if self.permit_method == PERMIT_METHOD + ".appeal" and permit.idempotency_key != fingerprint(
            f"appeal-permit:{self.permit_id}:{self.allowed_command}".encode()
        ):
            raise TelegramGatewayError("DIAGNOSTIC_PERMIT_INVALID", "Appeal command differs from owner-approved step.")
        if state.fingerprint and state.fingerprint != self.safety_fingerprint:
            raise TelegramGatewayError("SAFETY_SESSION_CHANGED", "Session identity changed.")

    async def _check(self):
        settings = get_settings()
        enabled = diagnostic_execution_allowed(settings, self.permit_method)
        if not enabled or self._safety_stopped or self._safety_connection is None:
            raise TelegramGatewayError("SAFETY_STOPPED", "Diagnostic execution stopped.")
        pid = (await self._safety_connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        if pid != self._safety_backend_pid:
            self._safety_stopped = True
            raise TelegramGatewayError("SAFETY_LOCK_LOST", "Session lock connection changed.")
        await self._safety_connection.commit()
        async with AsyncSessionLocal() as db:
            await self._permit(db)
            pending = await db.scalar(
                select(TelegramOperation.id)
                .where(
                    TelegramOperation.account_id == self.safety_account_id,
                    TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                )
                .limit(1)
            )
            if pending:
                raise TelegramGatewayError("SAFETY_UNKNOWN_DELIVERY", "Unreconciled operation prevents diagnosis.")

    def _validate_request(self, request):
        request = unwrap_request(request)
        method = type(request).__name__
        if method not in self.rpc_caps:
            raise TelegramGatewayError("DIAGNOSTIC_RPC_FORBIDDEN", "Only official-bot diagnostic RPCs allowed.")
        if method == "ResolveUsernameRequest" and request.username.casefold() != "spambot":
            raise TelegramGatewayError("DIAGNOSTIC_TARGET_FORBIDDEN", "Only SpamBot can be resolved.")
        if method in {"SendMessageRequest", "GetHistoryRequest"}:
            if not self.bot_id or getattr(request.peer, "user_id", None) != self.bot_id:
                raise TelegramGatewayError("DIAGNOSTIC_TARGET_FORBIDDEN", "Peer must be the verified official bot.")
        if method == "SendMessageRequest" and (
            request.message != self.allowed_command or request.reply_to or request.schedule_date
        ):
            raise TelegramGatewayError("DIAGNOSTIC_COMMAND_FORBIDDEN", "Exactly one unscheduled /start allowed.")
        if method == "GetHistoryRequest" and (
            not self.start_message_id or request.min_id != self.start_message_id or not 1 <= request.limit <= 4
        ):
            raise TelegramGatewayError("DIAGNOSTIC_READ_FORBIDDEN", "Read only fresh replies after this request.")
        return method

    async def _begin_operation(self, request):
        method = self._validate_request(request)
        async with AsyncSessionLocal.begin() as db:
            await self._permit(db)
            await db.get(TelegramSafetyState, self.safety_account_id, with_for_update=True)
            count = await db.scalar(
                select(func.count())
                .select_from(TelegramOperation)
                .where(
                    TelegramOperation.account_id == self.safety_account_id,
                    TelegramOperation.method == self.operation_prefix + method,
                    TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                )
            )
            if count >= self.rpc_caps[method]:
                raise TelegramGatewayError("DIAGNOSTIC_BUDGET", "No repeated diagnostic commands.")
            op = TelegramOperation(
                account_id=self.safety_account_id,
                method=self.operation_prefix + method,
                outbound=method == "SendMessageRequest",
                target=str(self.bot_id) if self.bot_id else "@SpamBot",
                random_id=getattr(request, "random_id", None),
                idempotency_key=fingerprint(f"diagnostic:{self.permit_id}:{method}:{count}".encode()),
                outcome="pending",
                created_at=utcnow(),
            )
            db.add(op)
            await db.flush()
            return op.id

    async def _trip(self, exc, op_id=None, request=None):
        # A diagnostic transport failure must not erase the original write denial.
        self._safety_stopped = True
        async with AsyncSessionLocal.begin() as db:
            state = await db.get(TelegramSafetyState, self.safety_account_id, with_for_update=True)
            state.manual_hold = True
            seconds = flood_seconds(exc)
            if seconds is not None:
                state.wait_seconds = seconds
                deadline = utcnow() + timedelta(seconds=seconds + 5)
                state.wait_until = max(aware(state.wait_until) or deadline, deadline)
            if type(exc).__name__ == "PeerFloodError" and state.restriction_status not in {
                "limited",
                "blocked",
                "frozen",
            }:
                state.restriction_status = "limited"
            db.add(
                TelegramOperation(
                    account_id=self.safety_account_id,
                    method="operator.spambot_diagnostic_failure",
                    target=op_id,
                    outbound=False,
                    outcome="failed",
                    error_code=type(exc).__name__,
                    created_at=utcnow(),
                    finished_at=utcnow(),
                )
            )

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        method = self._validate_request(request)
        result = await super()._call(sender, request, ordered, flood_sleep_threshold)
        if method == "ResolveUsernameRequest":
            bot = next(
                (
                    u
                    for u in result.users
                    if u.id == getattr(result.peer, "user_id", None)
                    and getattr(u, "bot", False)
                    and getattr(u, "verified", False)
                    and (u.username or "").casefold() == "spambot"
                ),
                None,
            )
            if not bot:
                self._safety_stopped = True
                raise TelegramGatewayError("DIAGNOSTIC_BOT_UNVERIFIED", "Official verified SpamBot not found.")
            self.bot_id = bot.id
        elif method == "SendMessageRequest":
            message_id = getattr(result, "id", None)
            if not message_id:
                message_id = next(
                    (
                        u.id
                        for u in getattr(result, "updates", [])
                        if type(u).__name__ == "UpdateMessageID"
                        and u.random_id == getattr(unwrap_request(request), "random_id", None)
                    ),
                    None,
                )
            if not message_id:
                message_id = next(
                    (
                        u.message.id
                        for u in getattr(result, "updates", [])
                        if type(u).__name__ == "UpdateNewMessage" and u.message.out
                    ),
                    None,
                )
            if not message_id:
                self._safety_stopped = True
                raise TelegramGatewayError(
                    "DIAGNOSTIC_SEND_ID_MISSING", "Accepted diagnostic has no message ID; no repeat."
                )
            self.start_message_id = message_id
        return result

    async def _handle_bootstrap_notification(self, message):
        states = self._sender._pop_states(message.obj.bad_msg_id)
        kind = _bootstrap_correction_kind(message, states)
        corrected = getattr(self, "_safety_bootstrap_corrections", set())
        if kind and kind not in corrected:
            try:
                async with AsyncSessionLocal.begin() as db:
                    await self._permit(db)
                    pending = list(
                        (
                            await db.scalars(
                                select(TelegramOperation).where(
                                    TelegramOperation.account_id == self.safety_account_id,
                                    TelegramOperation.outcome == "pending",
                                )
                            )
                        ).all()
                    )
                    if (
                        len(pending) != 1
                        or pending[0].method != self.operation_prefix + "GetConfigRequest"
                        or pending[0].outbound
                    ):
                        raise TelegramGatewayError(
                            "DIAGNOSTIC_BOOTSTRAP_INVALID", "One pending bootstrap read required."
                        )
                    db.add(
                        TelegramOperation(
                            account_id=self.safety_account_id,
                            method=self.operation_prefix + "bootstrap_correction",
                            outbound=False,
                            target=kind,
                            outcome="accepted",
                            created_at=utcnow(),
                            finished_at=utcnow(),
                        )
                    )
                self._safety_bootstrap_corrections = {*corrected, kind}
                if kind == "server_salt":
                    self._sender._state.salt = message.obj.new_server_salt
                else:
                    self._sender._state.update_time_offset(correct_msg_id=message.msg_id)
                self._sender._send_queue.extend(states)
                return
            except Exception:
                pass
        await self._reject_protocol_states(states)
