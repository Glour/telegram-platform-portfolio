import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from celery import current_task
from sqlalchemy import func, select, text
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl.functions import InvokeWithoutUpdatesRequest
from telethon.tl.types import BadMsgNotification, BadServerSalt

from app.core.config import get_settings
from app.core.crypto import decrypt_text
from app.db.session import AsyncSessionLocal, engine
from app.modules.telegram_accounts.models import TelegramAccount, TelegramSession
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety.admission import (
    ATTESTABLE_REASONS,
    owner_attested,
    owner_join_limits,
    replacement_join_limits,
)
from app.modules.telegram_safety.error_catalog import classify_error
from app.modules.telegram_safety.models import TelegramLoginSafety, TelegramOperation, TelegramSafetyState

RESTRICTED = {"limited", "blocked", "frozen", "spam_blocked"}
AUTH_CHALLENGES = {
    "SessionPasswordNeededError",
    "PhoneCodeInvalidError",
    "PhoneCodeExpiredError",
    "PasswordHashInvalidError",
}
READ_METHODS = {
    "GetUsersRequest",
    "GetFullUserRequest",
    "GetStateRequest",
    "GetDifferenceRequest",
    "GetConfigRequest",
    "GetDialogsRequest",
    "GetHistoryRequest",
    "GetMessagesRequest",
    "GetFullChannelRequest",
    "GetChannelsRequest",
    "GetFullChatRequest",
    "GetParticipantRequest",
    "GetParticipantsRequest",
    "GetContactsRequest",
    "GetUserPhotosRequest",
    "GetFileRequest",
    "GetPeerDialogsRequest",
    "GetDiscussionMessageRequest",
    "GetRepliesRequest",
    "GetPasswordRequest",
    "GetStickerSetRequest",
    "GetFeaturedEmojiStickersRequest",
    "GetExportedChatInviteRequest",
    "GetStoriesByIDRequest",
    "GetPeerStoriesRequest",
    "ResolveUsernameRequest",
    "CheckChatInviteRequest",
    "SearchRequest",
    "PingRequest",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def fingerprint(value: bytes) -> str:
    key = get_settings().session_encryption_key or get_settings().secret_key
    return hmac.new(key.encode(), value, hashlib.sha256).hexdigest()


def flood_seconds(exc: Exception) -> int | None:
    value = getattr(exc, "seconds", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def is_outbound(request: Any) -> bool:
    return type(request).__name__ not in READ_METHODS


def unwrap_request(request):
    while type(request).__name__ in {"InvokeWithLayerRequest", "InitConnectionRequest", "InvokeWithoutUpdatesRequest"}:
        request = request.query
    return request


def _bootstrap_correction_kind(message, states) -> str | None:
    if len(states) != 1 or type(unwrap_request(getattr(states[0], "request", None))).__name__ != "GetConfigRequest":
        return None
    if isinstance(message.obj, BadServerSalt) and message.obj.error_code == 48:
        return "server_salt"
    if isinstance(message.obj, BadMsgNotification) and message.obj.error_code in {16, 17}:
        return "time_offset"
    return None


def operation_payload(request: Any) -> str:
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k != "random_id"}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return hashlib.sha256(json.dumps(clean(request.to_dict()), sort_keys=True, default=str).encode()).hexdigest()


def state_dict(state: TelegramSafetyState) -> dict[str, Any]:
    fields = (
        "account_id",
        "manual_hold",
        "reason",
        "received_at",
        "wait_seconds",
        "wait_until",
        "restriction_status",
        "restriction_checked_at",
        "operator_approved_at",
        "scope",
        "operation_id",
        "first_seen_at",
        "outbound_attempt_limit",
        "rpc_attempt_limit",
        "actor_id",
    )
    return {
        **{key: getattr(state, key) for key in fields},
        "execution_enabled": get_settings().telegram_operations_enabled,
    }


async def _state(db, account_id: str) -> TelegramSafetyState:
    state = await db.get(TelegramSafetyState, account_id)
    if state is None:
        account = await db.get(TelegramAccount, account_id)
        if account is None:
            raise TelegramGatewayError("ACCOUNT_NOT_FOUND", "Account no longer exists.")
        status = account.restriction_status or "unknown"
        state = TelegramSafetyState(
            account_id=account_id,
            manual_hold=True,
            reason="admission_required",
            restriction_status=status,
            first_seen_at=account.created_at or utcnow(),
            outbound_attempt_limit=1,
        )
        db.add(state)
        await db.flush()
    return state


async def get_safety_state(account_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        result = state_dict(state)
        last = (
            await db.execute(
                select(TelegramOperation)
                .where(TelegramOperation.account_id == account_id)
                .order_by(TelegramOperation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        result["last_operation"] = (
            {
                "id": last.id,
                "method": last.method,
                "outcome": last.outcome,
                "error_code": last.error_code,
                "created_at": last.created_at,
                "finished_at": last.finished_at,
                "message_id": last.message_id,
            }
            if last
            else None
        )
        blocking = (
            await db.get(TelegramOperation, state.operation_id) if state.manual_hold and state.operation_id else None
        )
        result["blocking_operation"] = (
            {
                "id": blocking.id,
                "method": blocking.method,
                "outcome": blocking.outcome,
                "error_code": blocking.error_code,
                "target": blocking.target,
                "outbound": blocking.outbound,
                "created_at": blocking.created_at,
                "finished_at": blocking.finished_at,
                "message_id": blocking.message_id,
            }
            if blocking
            else None
        )
        result["diagnosis"] = None
        if blocking and blocking.error_code:
            targets = 1
            if blocking.error_code == "UserBannedInChannelError":
                targets = (
                    await db.scalar(
                        select(func.count(func.distinct(TelegramOperation.target))).where(
                            TelegramOperation.account_id == account_id,
                            TelegramOperation.error_code == "UserBannedInChannelError",
                            TelegramOperation.outbound.is_(True),
                            TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                        )
                    )
                    or 1
                )
            result["diagnosis"] = classify_error(blocking.error_code, blocking.method, distinct_targets=targets)
        if state.manual_hold and state.restriction_status in {"limited", "blocked"}:
            account = await db.get(TelegramAccount, account_id)
            confirmation = (account.daily_limits or {}).get("restriction_status", {}) if account else {}
            if (
                confirmation.get("source") == "verified_spambot"
                and confirmation.get("status") == state.restriction_status
            ):
                result["diagnosis"] = {
                    **(result["diagnosis"] or {}),
                    "label": "Спам-блок подтверждён SpamBot",
                    "category": "spam_restriction",
                    "spam_signal": "confirmed",
                    "stop_scope": "account",
                    "automatic_retry": False,
                    "meaning": "Официальный SpamBot подтвердил ограничение; "
                    "аккаунт остановлен. Срок не предполагается.",
                    "confirmation_source": "verified_spambot",
                    "confirmed_at": confirmation.get("checked_at"),
                }
        return result


async def _assert_resume(db, state: TelegramSafetyState) -> None:
    now = utcnow()
    if state.wait_until and aware(state.wait_until) > now:
        raise TelegramGatewayError("SAFETY_WAIT_ACTIVE", "The original Telegram wait has not expired.")
    account = await db.get(TelegramAccount, state.account_id)
    if state.restriction_status in RESTRICTED or account.restriction_status in RESTRICTED:
        raise TelegramGatewayError("SAFETY_RESTRICTED", "Confirmed restrictions cannot be cleared by resume.")
    unknown = (
        await db.execute(
            select(TelegramOperation.id)
            .where(
                TelegramOperation.account_id == state.account_id,
                TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if unknown:
        raise TelegramGatewayError("SAFETY_UNKNOWN_DELIVERY", "Resolve the outstanding operation before resume.")


async def approve_account(account_id: str, actor_id: str, checked_at: datetime, evidence: str) -> dict[str, Any]:
    checked_at = aware(checked_at)
    if not evidence.strip() or not utcnow() - timedelta(hours=24) <= checked_at <= utcnow():
        raise TelegramGatewayError(
            "SAFETY_EVIDENCE_REQUIRED", "A dated clear baseline from the last 24 hours is required."
        )
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        state.restriction_status = "clear"
        state.restriction_checked_at = checked_at
        state.operator_approved_at = utcnow()
        state.actor_id = actor_id
        state.evidence = evidence.strip()
        state.manual_hold = False
        state.reason = "operator_admission"
        account = await db.get(TelegramAccount, account_id)
        limits = dict(account.daily_limits or {})
        limits["restriction_status"] = {
            "status": "clear",
            "checked_at": checked_at.isoformat(),
            "label": "Operator evidence",
            "evidence_source": "operator_admission",
        }
        account.daily_limits = limits
        if account.status in {"new", "held", "quarantined"}:
            account.status = "active"
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.admit",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def approve_owner_attestation(account_id: str, actor_id: str, evidence: str) -> dict[str, Any]:
    """Explicit owner exception; preserves unknown restriction and its check timestamp."""
    if not evidence.strip():
        raise TelegramGatewayError("SAFETY_EVIDENCE_REQUIRED", "Owner attestation is required.")
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if not workspace or workspace.owner_user_id != actor_id:
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Only the workspace owner can attest.")
        if state.reason not in ATTESTABLE_REASONS:
            raise TelegramGatewayError("SAFETY_REVIEW_REQUIRED", "An operational hold requires separate review.")
        if account.status in {
            "blocked",
            "banned",
            "frozen",
            "spam_blocked",
            "quarantined",
            "invalid",
            "deleted",
            "archived",
        }:
            raise TelegramGatewayError("SAFETY_RESTRICTED", "Account status prevents admission.")
        now = utcnow()
        previous_admission = dict((account.daily_limits or {}).get("owner_admission", {}))
        limits = dict(account.daily_limits or {})
        limits["owner_admission"] = {
            "basis": "owner_attestation",
            "active": True,
            "account_id": account.id,
            "workspace_id": account.workspace_id,
            "actor_id": actor_id,
            "approved_at": now.isoformat(),
            "evidence": evidence.strip(),
            "telegram_check_performed": False,
            "warmed_by_owner": True,
            "modules": ["auto_join", "neurocommenting"],
            "comment_attempt_limit": 5,
            "join_hour_limit": 2,
            "min_join_interval_seconds": 1801,
        }
        account.daily_limits = limits
        if account.status in {"new", "held"}:
            account.status = "active"
        # An admitted owner account uses the working runtime, not intake's one-write pilot.
        # Keep an explicit owner limit override when renewing the same account's admission.
        override = previous_admission.get("comment_limit_override", {})
        if (
            override.get("basis") == "owner_request"
            and override.get("actor_id") == actor_id
            and override.get("account_id") == account.id
            and override.get("workspace_id") == account.workspace_id
        ):
            limits["owner_admission"]["comment_attempt_limit"] = int(override["limit"])
            limits["owner_admission"]["comment_limit_override"] = override
            account.daily_limits = limits
        state.outbound_attempt_limit = max(
            state.outbound_attempt_limit, 48 + limits["owner_admission"]["comment_attempt_limit"]
        )
        state.rpc_attempt_limit = max(state.rpc_attempt_limit, 10000)
        state.operator_approved_at = now
        state.actor_id = actor_id
        state.evidence = evidence.strip()
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.owner_attest",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=now,
                finished_at=now,
            )
        )
        return state_dict(state)


def _owner_admission_valid(state, account) -> bool:
    return bool(
        owner_attested(account)
        and state.reason == "owner_attested_admission"
        and state.operator_approved_at
        and state.actor_id
        and state.actor_id == account.daily_limits["owner_admission"]["actor_id"]
    )


async def resume_owner_after_join_request_classification(account_id: str, actor_id: str) -> dict[str, Any]:
    """Repair only the old misclassification of a successfully submitted join request."""
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "unknown_rpc"
            or not op
            or op.account_id != account_id
            or op.method != "JoinChannelRequest"
            or op.error_code != "InviteRequestSentError"
            or op.outcome != "failed"
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Only the misclassified join approval response can be repaired."
            )
        op.outcome = "approval_pending"
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = (
            "Owner requested replacement of approval-required sources; join request response reclassified, no repeat."
        )
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.classify_join_request",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_elapsed_read_wait(account_id: str, actor_id: str) -> dict[str, Any]:
    """Owner-requested continuation after a recorded read-only wait fully expires."""
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "flood_wait"
            or not state.wait_until
            or aware(state.wait_until) > utcnow()
            or not op
            or op.outbound
            or op.account_id != account_id
            or op.outcome != "failed"
            or op.error_code != "FloodWaitError"
            or op.method not in READ_METHODS | {"ResolveUsernameRequest"}
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Only a fully elapsed, recorded read wait can be resumed."
            )
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = "Owner requested launch continuation; recorded read wait elapsed; no request replay."
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resume_elapsed_read_wait",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_source_read_denial(account_id: str, actor_id: str) -> dict[str, Any]:
    """Repair a source-specific read denial, preserving the inaccessible target evidence."""
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "unknown_rpc"
            or not op
            or op.outbound
            or op.account_id != account_id
            or op.outcome != "failed"
            or op.error_code != "ChannelPrivateError"
            or op.method not in {"GetHistoryRequest", "GetFullChannelRequest"}
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Only a source-specific read denial can be reclassified."
            )
        op.outcome = "source_denied"
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = "Owner requested open source replacements; inaccessible source excluded, no read replay."
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.classify_source_read_denial",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_bootstrap_repair(account_id: str, actor_id: str, evidence: str) -> dict[str, Any]:
    """Manual repair of a failed initial read; never resumes an attempted write."""
    if not evidence.strip():
        raise TelegramGatewayError("SAFETY_EVIDENCE_REQUIRED", "Repair evidence required.")
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        await _assert_resume(db, state)
        operations = list(
            (
                await db.scalars(
                    select(TelegramOperation).where(
                        TelegramOperation.account_id == account_id,
                        TelegramOperation.method.not_like("operator.%"),
                    )
                )
            ).all()
        )
        if (
            state.reason != "transport_error"
            or not operations
            or any(
                op.outbound or op.method != "GetConfigRequest" or op.outcome not in {"failed", "not_delivered"}
                for op in operations
            )
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED",
                "Repair resume is limited to failed initial GetConfig reads with no attempted writes.",
            )
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = evidence.strip()
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resume_owner_after_bootstrap_repair",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_preconnect_repair(account_id: str, actor_id: str, evidence: str) -> dict[str, Any]:
    """Reviewed connection failure before RPC dispatch, with the same proxy retained."""
    if not evidence.strip():
        raise TelegramGatewayError("SAFETY_EVIDENCE_REQUIRED", "Connection repair evidence required.")
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.campaigns.models import Campaign, CampaignAction
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        if not state.manual_hold or state.reason != "transport_error" or state.operation_id or not state.received_at:
            raise TelegramGatewayError("SAFETY_REVIEW_REQUIRED", "Only a pre-dispatch connection hold can be resumed.")
        later = await db.scalar(
            select(TelegramOperation.id)
            .where(
                TelegramOperation.account_id == account_id,
                TelegramOperation.created_at >= state.received_at,
                TelegramOperation.method.not_like("operator.%"),
            )
            .limit(1)
        )
        failure = await db.scalar(
            select(CampaignAction.id)
            .where(
                CampaignAction.telegram_account_id == account_id,
                CampaignAction.action_type.in_(["parse_source", "generate_and_send_comment"]),
                CampaignAction.error_code == "TELEGRAM_CONNECTION_FAILED",
                CampaignAction.created_at >= state.received_at,
                CampaignAction.created_at <= aware(state.received_at) + timedelta(seconds=30),
            )
            .limit(1)
        )
        if not failure:
            # Scoped connection setup runs before the engine can create an action.
            # An explicit recovery poll with zero RPCs is a durable pre-dispatch proof.
            poll = await db.scalar(
                select(TelegramOperation)
                .where(
                    TelegramOperation.account_id == account_id,
                    TelegramOperation.method == "operator.request_recovery_poll",
                    TelegramOperation.outcome == "approved",
                    TelegramOperation.created_at <= state.received_at,
                    TelegramOperation.created_at >= aware(state.received_at) - timedelta(minutes=30),
                )
                .order_by(TelegramOperation.created_at.desc())
                .limit(1)
            )
            campaign = await db.get(Campaign, poll.target) if poll else None
            rpc_since_poll = (
                await db.scalar(
                    select(TelegramOperation.id)
                    .where(
                        TelegramOperation.account_id == account_id,
                        TelegramOperation.method.not_like("operator.%"),
                        TelegramOperation.created_at >= poll.created_at,
                    )
                    .limit(1)
                )
                if poll
                else True
            )
            failure = bool(
                campaign
                and campaign.workspace_id == account.workspace_id
                and account_id in campaign.config.get("account_ids", [])
                and not rpc_since_poll
            )
        if later or not failure:
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Requires recorded pre-dispatch connection failure and no later RPC."
            )
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = evidence.strip()
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resume_preconnect_repair",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_read_transport_review(account_id: str, actor_id: str, evidence: str) -> dict[str, Any]:
    """Explicit continuation after a failed read; never resumes unknown writes."""
    if not evidence.strip():
        raise TelegramGatewayError("SAFETY_EVIDENCE_REQUIRED", "Transport review evidence required.")
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "transport_error"
            or not op
            or op.outbound
            or op.account_id != account_id
            or op.method not in READ_METHODS
            or op.outcome != "failed"
            or op.error_code not in {"ConnectionError", "TimeoutError", "OSError"}
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Only a recorded failed read transport can be resumed."
            )
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = evidence.strip()
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resume_read_transport_review",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_owner_after_excluded_discussion(account_id: str, actor_id: str) -> dict[str, Any]:
    """Legacy entry point: exclusion alone must never release a rejected write."""
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "source_denied"
            or not op
            or not op.outbound
            or op.account_id != account_id
            or op.method != "SendMessageRequest"
            or op.outcome != "failed"
            or op.error_code != "UserBannedInChannelError"
            or not op.target
            or state.scope != f"account_target:{op.target}"
        ):
            raise TelegramGatewayError("SAFETY_REVIEW_REQUIRED", "Requires a confirmed source-specific rejected write.")
        raise TelegramGatewayError(
            "SAFETY_SPAM_REVIEW_REQUIRED",
            "A rejected write may indicate an account restriction. Removing its source cannot resume the account.",
        )


async def resume_owner_after_invalid_post_read(account_id: str, actor_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        account = await db.get(TelegramAccount, account_id)
        from app.modules.workspaces.models import Workspace

        workspace = await db.get(Workspace, account.workspace_id)
        if workspace.owner_user_id != actor_id or not owner_attested(account):
            raise TelegramGatewayError("SAFETY_OWNER_REQUIRED", "Existing owner admission required.")
        op = await db.get(TelegramOperation, state.operation_id) if state.operation_id else None
        if (
            not state.manual_hold
            or state.reason != "unknown_rpc"
            or not op
            or op.outbound
            or op.account_id != account_id
            or op.method != "GetDiscussionMessageRequest"
            or op.outcome != "failed"
            or op.error_code != "MsgIdInvalidError"
        ):
            raise TelegramGatewayError(
                "SAFETY_REVIEW_REQUIRED", "Only a missing post's discussion read can be reclassified."
            )
        op.outcome = "post_unavailable"
        state.manual_hold = False
        state.reason = "owner_attested_admission"
        state.actor_id = actor_id
        state.evidence = (
            "Owner requested completion; invalid post read reclassified. No write or failed-comment replay."
        )
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.classify_invalid_post_read",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resume_account(account_id: str, actor_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        await _assert_resume(db, state)
        if (
            state.restriction_status != "clear"
            or not state.restriction_checked_at
            or aware(state.restriction_checked_at) < utcnow() - timedelta(hours=24)
        ):
            raise TelegramGatewayError("SAFETY_EVIDENCE_REQUIRED", "A fresh clear baseline is required.")
        if state.reason in {
            "session_invalid",
            "unknown_rpc",
            "transport_error",
            "source_denied",
            "spam_suspected",
            "spam_restricted",
            "flood_unknown",
        }:
            raise TelegramGatewayError("SAFETY_REVIEW_REQUIRED", "New evidence and explicit admission are required.")
        state.manual_hold = False
        state.reason = "operator_resume"
        state.actor_id = actor_id
        state.operator_approved_at = utcnow()
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resume",
                target=actor_id,
                outbound=False,
                outcome="approved",
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


async def resolve_operation(account_id, operation_id, actor_id, outcome, evidence, message_id=None):
    if (
        outcome not in {"accepted", "not_delivered"}
        or not evidence.strip()
        or (outcome == "accepted" and not message_id)
    ):
        raise TelegramGatewayError("SAFETY_RESOLUTION_EVIDENCE", "Delivery evidence is required.")
    async with AsyncSessionLocal.begin() as db:
        op = await db.get(TelegramOperation, operation_id, with_for_update=True)
        if op is None or op.account_id != account_id or op.outcome not in {"pending", "unknown_delivery"}:
            raise TelegramGatewayError("SAFETY_RESOLUTION_INVALID", "Operation is not awaiting reconciliation.")
        op.outcome = outcome
        op.message_id = message_id
        op.finished_at = utcnow()
        state = await _state(db, account_id)
        state.evidence = evidence.strip()
        state.actor_id = actor_id
        db.add(
            TelegramOperation(
                account_id=account_id,
                method="operator.resolve",
                target=actor_id,
                outbound=False,
                outcome=outcome,
                created_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        return state_dict(state)


class GuardedTelegramClient(TelegramClient):
    def __init__(self, *args, account_id: str | None, lock_fingerprint: str, login_fingerprint=None, **kwargs):
        kwargs.update(
            request_retries=0,
            flood_sleep_threshold=0,
            connection_retries=0,
            auto_reconnect=False,
            receive_updates=False,
            raise_last_call_error=True,
        )
        super().__init__(*args, **kwargs)
        self.safety_account_id = account_id
        self.safety_fingerprint = lock_fingerprint
        self.safety_login_fingerprint = login_fingerprint or lock_fingerprint
        self._safety_connection = None
        self._safety_backend_pid = None
        self._safety_rpc_lock = asyncio.Lock()
        self._safety_stopped = False
        self._safety_business_intent = None
        self._safety_protocol_failure_task = None
        task = current_task._get_current_object()
        self._safety_intent = getattr(getattr(task, "request", None), "id", None) or str(uuid4())
        self._safety_raw_sender = SimpleNamespace(send=self._sender.send)
        self._safety_request_timeout = max(1, float(kwargs.get("timeout", 25)))
        self._sender._handlers[BadServerSalt.CONSTRUCTOR_ID] = self._handle_bootstrap_notification
        self._sender._handlers[BadMsgNotification.CONSTRUCTOR_ID] = self._handle_bootstrap_notification
        self._sender.send = lambda request, ordered=False: asyncio.ensure_future(
            self._call(self._safety_raw_sender, request, ordered=ordered)
        )

    async def _handle_bootstrap_notification(self, message):
        """Bounded MTProto salt/time correction for initial GetConfig only; never writes."""
        states = self._sender._pop_states(message.obj.bad_msg_id)
        kind = _bootstrap_correction_kind(message, states)
        corrections = getattr(self, "_safety_bootstrap_corrections", set())
        if kind and kind not in corrections and self.safety_account_id is not None:
            try:
                async with AsyncSessionLocal.begin() as db:
                    state = await _state(db, self.safety_account_id)
                    await db.refresh(state, with_for_update=True)
                    account = await db.get(TelegramAccount, self.safety_account_id)
                    if (
                        state.manual_hold
                        or state.restriction_status in RESTRICTED
                        or not (_owner_admission_valid(state, account) or state.restriction_status == "clear")
                    ):
                        raise TelegramGatewayError("SAFETY_MANUAL_HOLD", "Bootstrap correction is held.")
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
                    if len(pending) != 1 or pending[0].method != "GetConfigRequest" or pending[0].outbound:
                        raise TelegramGatewayError(
                            "SAFETY_PROTOCOL_REVIEW", "Correction requires exactly one pending read-only bootstrap."
                        )
                    total = await db.scalar(
                        select(func.count())
                        .select_from(TelegramOperation)
                        .where(
                            TelegramOperation.account_id == self.safety_account_id,
                            TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                            TelegramOperation.method.not_like("operator.%"),
                        )
                    )
                    if total >= state.rpc_attempt_limit:
                        raise TelegramGatewayError("SAFETY_RPC_BUDGET", "Read correction budget exhausted.")
                    db.add(
                        TelegramOperation(
                            account_id=self.safety_account_id,
                            method="protocol.bootstrap_correction",
                            outbound=False,
                            target=kind,
                            outcome="accepted",
                            created_at=utcnow(),
                            finished_at=utcnow(),
                        )
                    )
                self._safety_bootstrap_corrections = {*corrections, kind}
                if kind == "server_salt":
                    self._sender._state.salt = message.obj.new_server_salt
                else:
                    self._sender._state.update_time_offset(correct_msg_id=message.msg_id)
                self._sender._send_queue.extend(states)
                return
            except Exception:
                pass
        await self._reject_protocol_states(states)

    async def _reject_protocol_states(self, states):
        self._safety_stopped = True
        for state in states:
            if not state.future.done():
                state.future.set_exception(ConnectionError("MTProto retry rejected; manual transport review required."))
        if getattr(self, "_safety_protocol_failure_task", None) is None:
            self._safety_protocol_failure_task = asyncio.create_task(self._finalize_protocol_failure())

    async def _reject_protocol_retry(self, message):
        await self._reject_protocol_states(self._sender._pop_states(message.obj.bad_msg_id))

    async def _finalize_protocol_failure(self):
        try:
            await self._trip(ConnectionError("MTProto protocol rejection"))
        finally:
            await super().disconnect()

    def _login_key(self):
        return getattr(self, "safety_login_fingerprint", self.safety_fingerprint)

    async def complete_login(self):
        async with AsyncSessionLocal.begin() as db:
            keys = {self._login_key(), self.safety_fingerprint}
            auth_key = getattr(self.session, "auth_key", None)
            if auth_key:
                keys.add(fingerprint(auth_key.key))
            for key in keys:
                state = await db.get(TelegramLoginSafety, key, with_for_update=True)
                if state is None:
                    state = TelegramLoginSafety(fingerprint=key)
                    db.add(state)
                state.manual_hold = True
                state.reason = "login_completed"

    async def _check(self) -> None:
        if not get_settings().telegram_operations_enabled:
            raise TelegramGatewayError(
                "SAFETY_EXECUTION_DISABLED", "Telegram execution is disabled by the release gate."
            )
        if self._safety_stopped or self._safety_connection is None:
            raise TelegramGatewayError("SAFETY_STOPPED", "Telegram operation is stopped.")
        try:
            await self._safety_connection.execute(text("SELECT 1"))
            pid = (await self._safety_connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            if getattr(self, "_safety_backend_pid", None) is None or pid != self._safety_backend_pid:
                self._safety_stopped = True
                await super().disconnect()
                raise TelegramGatewayError("SAFETY_LOCK_LOST", "Database connection no longer owns the session lock.")
            await self._safety_connection.commit()
            async with AsyncSessionLocal.begin() as db:
                if self.safety_account_id is None:
                    for key in {self._login_key(), self.safety_fingerprint}:
                        state = await db.get(TelegramLoginSafety, key)
                        if state and state.manual_hold:
                            raise TelegramGatewayError("SAFETY_LOGIN_HOLD", "Login requires manual review.")
                    registered = (await db.execute(select(TelegramSession))).scalars().all()
                    for saved in registered:
                        try:
                            stored = StringSession(decrypt_text(saved.encrypted_session_blob) or "")
                        except (ValueError, TypeError):
                            continue
                        if stored.auth_key and fingerprint(stored.auth_key.key) == self.safety_fingerprint:
                            raise TelegramGatewayError(
                                "SAFETY_LOGIN_COMPLETED",
                                "Imported sessions cannot reuse login.",
                            )
                    return
                state = await _state(db, self.safety_account_id)
                await db.refresh(state, with_for_update=True)
                account = await db.get(TelegramAccount, self.safety_account_id)
                if account.restriction_status in RESTRICTED or account.status in RESTRICTED:
                    raise TelegramGatewayError("SAFETY_RESTRICTED", "Account has a confirmed restriction.")
                if state.fingerprint and state.fingerprint != self.safety_fingerprint:
                    raise TelegramGatewayError("SAFETY_SESSION_CHANGED", "Session changed; new intake is required.")
                state.fingerprint = self.safety_fingerprint
                if state.wait_until and aware(state.wait_until) > utcnow():
                    raise TelegramGatewayError("SAFETY_WAIT_ACTIVE", "The original Telegram wait has not expired.")
                owner_exception = _owner_admission_valid(state, account)
                if (
                    state.manual_hold
                    or (state.restriction_status != "clear" and not owner_exception)
                    or not state.operator_approved_at
                ):
                    raise TelegramGatewayError("SAFETY_MANUAL_HOLD", "Account requires explicit operator admission.")
                if not owner_exception and (
                    not state.restriction_checked_at
                    or aware(state.restriction_checked_at) < utcnow() - timedelta(hours=24)
                ):
                    raise TelegramGatewayError("SAFETY_BASELINE_STALE", "Restriction baseline is stale.")
                pending = (
                    await db.execute(
                        select(TelegramOperation.id)
                        .where(
                            TelegramOperation.account_id == self.safety_account_id,
                            TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if pending:
                    raise TelegramGatewayError("SAFETY_UNKNOWN_DELIVERY", "A previous operation needs reconciliation.")
        except TelegramGatewayError:
            raise
        except Exception as exc:
            self._safety_stopped = True
            raise TelegramGatewayError(
                "SAFETY_STORAGE_UNAVAILABLE", "Cannot verify the persistent safety barrier."
            ) from exc

    async def connect(self):
        if self._safety_connection is not None:
            return
        try:
            self._safety_connection = await engine.connect()
            lock_id = int.from_bytes(bytes.fromhex(self.safety_fingerprint)[:8], "big", signed=True)
            pid, acquired = (
                await self._safety_connection.execute(
                    text("SELECT pg_backend_pid(), pg_try_advisory_lock(:key)"), {"key": lock_id}
                )
            ).one()
            if acquired:
                self._safety_backend_pid = pid
            await self._safety_connection.commit()
            if not acquired:
                raise TelegramGatewayError("SAFETY_ACCOUNT_BUSY", "Another process owns this Telegram session.")
            await self._check()
            return await asyncio.wait_for(super().connect(), timeout=getattr(self, "_safety_request_timeout", 25))
        except BaseException as exc:
            try:
                if not isinstance(exc, TelegramGatewayError) and not self._safety_stopped:
                    safe_exc = exc if isinstance(exc, Exception) else ConnectionError("Connection interrupted")
                    await asyncio.shield(self._trip(safe_exc))
            finally:
                await self.disconnect()
            raise

    async def disconnect(self):
        try:
            await asyncio.wait_for(super().disconnect(), timeout=10)
        finally:
            if self._safety_connection is not None:
                connection, self._safety_connection = self._safety_connection, None
                try:
                    lock_id = int.from_bytes(bytes.fromhex(self.safety_fingerprint)[:8], "big", signed=True)
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
                    await connection.commit()
                except Exception:
                    await connection.invalidate()
                finally:
                    await connection.close()

    async def _borrow_exported_sender(self, dc_id):
        raise TelegramGatewayError("SAFETY_EXPORTED_SENDER_DISABLED", "Cross-DC transfers require a guarded transport.")

    async def _switch_dc(self, new_dc):
        raise TelegramGatewayError("SAFETY_DC_MIGRATION_REVIEW", "DC migration requires explicit intake review.")

    async def _begin_operation(self, request):
        if self.safety_account_id is None:
            async with AsyncSessionLocal.begin() as db:
                state = await db.get(TelegramLoginSafety, self._login_key(), with_for_update=True)
                if state is None:
                    state = TelegramLoginSafety(fingerprint=self._login_key())
                    db.add(state)
                if state.manual_hold:
                    raise TelegramGatewayError("SAFETY_LOGIN_HOLD", "Login requires manual review.")
                # A crash or failed result commit must leave a durable login barrier.
                state.manual_hold = True
                state.reason = f"login_pending:{self._safety_intent}"
            return None
        method = type(request).__name__
        outbound = is_outbound(request)
        random_id = getattr(request, "random_id", None)
        if not isinstance(random_id, int):
            random_id = None
        business_intent = getattr(self, "_safety_business_intent", None)
        if method in {"SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest", "ForwardMessagesRequest"}:
            if not business_intent:
                raise TelegramGatewayError("SAFETY_DELIVERY_INTENT_REQUIRED", "A stable business intent is required.")
            intent_payload = f"{self.safety_account_id}:delivery:business:{business_intent}"
        else:
            intent_payload = f"{self.safety_account_id}:{self._safety_intent}:{method}:{operation_payload(request)}"
        key = fingerprint(intent_payload.encode()) if outbound else None
        async with AsyncSessionLocal.begin() as db:
            state = await _state(db, self.safety_account_id)
            await db.refresh(state, with_for_update=True)
            account = await db.get(TelegramAccount, self.safety_account_id)
            if state.manual_hold or (
                state.restriction_status != "clear" and not _owner_admission_valid(state, account)
            ):
                raise TelegramGatewayError("SAFETY_MANUAL_HOLD", "Account admission changed before the operation.")
            admission = (account.daily_limits or {}).get("owner_admission", {})
            if outbound:
                now = utcnow()
                if method == "JoinChannelRequest" and (
                    _owner_admission_valid(state, account) or replacement_join_limits(account, now)
                ):
                    attempts = list(
                        (
                            await db.scalars(
                                select(TelegramOperation).where(
                                    TelegramOperation.account_id == self.safety_account_id,
                                    TelegramOperation.method == method,
                                    TelegramOperation.created_at > now - timedelta(hours=1),
                                )
                            )
                        ).all()
                    )
                    hour_limit, min_interval = owner_join_limits(account, now)
                    if len(attempts) >= hour_limit or any(
                        aware(op.created_at) > now - timedelta(seconds=min_interval) for op in attempts
                    ):
                        raise TelegramGatewayError(
                            "SAFETY_JOIN_RATE",
                            f"Maximum {hour_limit} joins per rolling hour, at least {min_interval} seconds apart.",
                        )
            if _owner_admission_valid(state, account) and outbound:
                if method in {
                    "SendMessageRequest",
                    "SendMediaRequest",
                    "SendMultiMediaRequest",
                    "ForwardMessagesRequest",
                }:
                    sent = (
                        await db.scalar(
                            select(func.count())
                            .select_from(TelegramOperation)
                            .where(
                                TelegramOperation.account_id == self.safety_account_id,
                                TelegramOperation.method.in_(
                                    [
                                        "SendMessageRequest",
                                        "SendMediaRequest",
                                        "SendMultiMediaRequest",
                                        "ForwardMessagesRequest",
                                    ]
                                ),
                                TelegramOperation.created_at >= now - timedelta(hours=24),
                            )
                        )
                    ) or 0
                    if sent >= int(admission.get("comment_attempt_limit", 5)):
                        raise TelegramGatewayError(
                            "SAFETY_COMMENT_BUDGET", "Rolling 24-hour comment attempt budget exhausted."
                        )
                    if admission.get("comment_delivery_limit"):
                        delivered = (
                            await db.scalar(
                                select(func.count())
                                .select_from(TelegramOperation)
                                .where(
                                    TelegramOperation.account_id == self.safety_account_id,
                                    TelegramOperation.method.in_(
                                        [
                                            "SendMessageRequest",
                                            "SendMediaRequest",
                                            "SendMultiMediaRequest",
                                            "ForwardMessagesRequest",
                                        ]
                                    ),
                                    TelegramOperation.outcome == "accepted",
                                    TelegramOperation.created_at >= now - timedelta(hours=24),
                                )
                            )
                            or 0
                        )
                        if delivered >= int(admission["comment_delivery_limit"]):
                            raise TelegramGatewayError(
                                "SAFETY_COMMENT_BUDGET", "Rolling 24-hour delivered comment limit exhausted."
                            )
            total = (
                await db.execute(
                    select(func.count())
                    .select_from(TelegramOperation)
                    .where(
                        TelegramOperation.account_id == self.safety_account_id,
                        TelegramOperation.method.not_like("operator.%"),
                        TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                    )
                )
            ).scalar_one()
            if total >= state.rpc_attempt_limit:
                raise TelegramGatewayError("SAFETY_RPC_BUDGET", "Rolling 24-hour RPC attempt budget exhausted.")
            if outbound:
                count = (
                    await db.execute(
                        select(func.count())
                        .select_from(TelegramOperation)
                        .where(
                            TelegramOperation.account_id == self.safety_account_id,
                            TelegramOperation.outbound.is_(True),
                            TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                        )
                    )
                ).scalar_one()
                if count >= state.outbound_attempt_limit:
                    raise TelegramGatewayError("SAFETY_ATTEMPT_BUDGET", "Rolling 24-hour attempt budget exhausted.")
                previous = (
                    await db.execute(select(TelegramOperation).where(TelegramOperation.idempotency_key == key).limit(1))
                ).scalar_one_or_none()
                if previous:
                    raise TelegramGatewayError(
                        "SAFETY_DUPLICATE_OPERATION",
                        "Operation already attempted; reconcile before continuing.",
                        {"operation_id": previous.id, "outcome": previous.outcome, "message_id": previous.message_id},
                    )
            target = getattr(request, "peer", None) or getattr(request, "channel", None)
            # Never persist request bodies, phone numbers, or credentials.
            target_id = (
                getattr(target, "channel_id", None)
                or getattr(target, "chat_id", None)
                or getattr(target, "user_id", None)
            )
            op = TelegramOperation(
                account_id=self.safety_account_id,
                method=method,
                outbound=outbound,
                target=str(target_id) if target_id else None,
                random_id=random_id,
                idempotency_key=key,
                outcome="pending",
                created_at=utcnow(),
            )
            db.add(op)
            await db.flush()
            return op.id

    async def _finish(self, op_id, outcome: str, exc: Exception | None = None, result=None):
        if op_id is None:
            if self.safety_account_id is None and outcome in {"accepted", "authentication_challenge"}:
                async with AsyncSessionLocal.begin() as db:
                    state = await db.get(TelegramLoginSafety, self._login_key(), with_for_update=True)
                    if state and state.reason == f"login_pending:{self._safety_intent}":
                        state.manual_hold = False
                        state.reason = None
            return
        async with AsyncSessionLocal.begin() as db:
            op = await db.get(TelegramOperation, op_id)
            op.outcome = outcome
            op.finished_at = utcnow()
            op.error_code = type(exc).__name__ if exc else None
            op.wait_seconds = flood_seconds(exc) if exc else None
            if result is not None:
                message_id = getattr(result, "id", None)
                for update in getattr(result, "updates", []):
                    message_id = (
                        message_id
                        or getattr(getattr(update, "message", None), "id", None)
                        or getattr(update, "id", None)
                    )
                op.message_id = str(message_id) if message_id else None
                if (
                    op.method
                    in {"SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest", "ForwardMessagesRequest"}
                    and not message_id
                ):
                    op.outcome = "unknown_delivery"
                    state = await _state(db, self.safety_account_id)
                    state.manual_hold = True
                    state.reason = "delivery_not_verified"
                    state.operation_id = op.id
                    self._safety_stopped = True

    async def _trip(self, exc: Exception, op_id=None, request=None):
        self._safety_stopped = True
        received = utcnow()
        seconds = flood_seconds(exc)
        code = type(exc).__name__
        diagnosis = classify_error(code, type(request).__name__ if request else None)
        reason = diagnosis["reason"]
        if reason == "flood_wait" and seconds is None:
            reason = "flood_unknown"
        if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
            reason = "transport_error"
        async with AsyncSessionLocal.begin() as db:
            if self.safety_account_id is None:
                state = await db.get(TelegramLoginSafety, self._login_key())
                if state is None:
                    state = TelegramLoginSafety(fingerprint=self._login_key())
                    db.add(state)
            else:
                state = await _state(db, self.safety_account_id)
                state.operation_id = op_id
                target = getattr(request, "peer", None) or getattr(request, "channel", None)
                target_id = getattr(target, "channel_id", None) or getattr(target, "chat_id", None)
                state.scope = (
                    f"account_target:{target_id or 'unknown'}"
                    if reason in {"source_denied", "spam_suspected"}
                    else "account"
                )
                if diagnosis["restriction_status"] and state.restriction_status not in RESTRICTED:
                    state.restriction_status = diagnosis["restriction_status"]
            # A known failed read (or pre-dispatch connection failure) cannot have
            # delivered a message. Keep admission and schedule a fresh connection,
            # while unknown writes, invalid sessions and confirmed restrictions hold.
            read_transient = (
                (
                    self.safety_account_id is not None
                    and reason == "transport_error"
                    and (request is None or not is_outbound(request))
                )
                or (self.safety_account_id is not None and reason == "flood_wait" and seconds is not None)
                or (
                    self.safety_account_id is not None
                    and type(request).__name__ in READ_METHODS
                    and code in {"BadRequestError", "RpcCallFailError", "ServerError"}
                )
            )
            if read_transient:
                account = await db.get(TelegramAccount, self.safety_account_id)
                read_transient = owner_attested(account) and state.restriction_status not in RESTRICTED
            if read_transient:
                unsafe = await db.scalar(
                    select(TelegramOperation.id)
                    .where(
                        TelegramOperation.account_id == self.safety_account_id,
                        TelegramOperation.outbound.is_(True),
                        TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                    )
                    .limit(1)
                )
                read_transient = not unsafe
            state.manual_hold = not read_transient
            state.reason = "owner_attested_admission" if read_transient else reason
            state.received_at = received
            if read_transient:
                state.evidence = "Transient failed read/connection; fresh polling after backoff, no write replay."
                state.wait_until = max(aware(state.wait_until) or received, received + timedelta(seconds=300))
            state.wait_seconds = seconds
            if seconds is not None:
                deadline = received + timedelta(seconds=seconds + 5)
                state.wait_until = max(aware(state.wait_until) or deadline, deadline)

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        requests = list(request) if utils.is_list_like(request) else [request]
        if len(requests) != 1:
            raise TelegramGatewayError("SAFETY_BATCH_DISABLED", "RPC batching is disabled; execute serially.")
        request = requests[0]
        ledger_request = unwrap_request(request)
        # A background ping must wait for this client's in-flight RPC before
        # checking the durable pending ledger. Foreign pending RPCs still block.
        async with self._safety_rpc_lock:
            await self._check()
        await request.resolve(self, utils)
        async with self._safety_rpc_lock:
            await self._check()
            op_id = await self._begin_operation(ledger_request)
            accepted = False
            try:
                dispatch = self._safety_raw_sender if sender is getattr(self, "_sender", None) else sender
                wire_request = request
                if getattr(self, "_no_updates", False) and type(request).__name__ not in {
                    "InvokeWithLayerRequest",
                    "InitConnectionRequest",
                    "InvokeWithoutUpdatesRequest",
                    # MTProto service pings are not API queries. Wrapping them
                    # produces INPUT_METHOD_INVALID and tears down a healthy read session.
                    "PingRequest",
                    "PingDelayDisconnectRequest",
                }:
                    wire_request = InvokeWithoutUpdatesRequest(request)
                result = await asyncio.wait_for(
                    dispatch.send(wire_request, ordered=ordered), timeout=getattr(self, "_safety_request_timeout", 25)
                )
                accepted = True
                await utils.maybe_async(self.session.process_entities(result))
                await self._finish(op_id, "accepted", result=result)
                return result
            except BaseException as exc:
                diagnosis = classify_error(type(exc).__name__, type(ledger_request).__name__)
                if diagnosis["reason"] == "post_unavailable":
                    await asyncio.shield(self._finish(op_id, "post_unavailable", exc))
                    raise TelegramGatewayError(
                        "COMMENT_POST_INVALID",
                        "Post has no available discussion message.",
                        {"operation_id": op_id, "rpc_error": "MsgIdInvalidError"},
                    ) from exc
                if diagnosis["reason"] == "source_denied" and diagnosis["stop_scope"] == "source":
                    await asyncio.shield(self._finish(op_id, "source_denied", exc))
                    raise TelegramGatewayError(
                        "SOURCE_READ_DENIED" if not is_outbound(ledger_request) else "SOURCE_ACCESS_DENIED",
                        "Source is inaccessible for this account.",
                        {"operation_id": op_id, "rpc_error": type(exc).__name__},
                    ) from exc
                if (
                    diagnosis["category"] == "approval_pending"
                    and type(ledger_request).__name__ == "JoinChannelRequest"
                ):
                    await asyncio.shield(self._finish(op_id, "approval_pending", exc))
                    raise TelegramGatewayError(
                        "JOIN_APPROVAL_PENDING",
                        "Join request submitted; membership awaits approval.",
                        {"operation_id": op_id},
                    ) from exc
                if (
                    isinstance(exc, Exception)
                    and self.safety_account_id is None
                    and type(exc).__name__ in AUTH_CHALLENGES
                ):
                    await self._finish(op_id, "authentication_challenge")
                    raise
                unknown = accepted or isinstance(exc, (OSError, ConnectionError, TimeoutError, asyncio.CancelledError))
                safe_exc = exc if isinstance(exc, Exception) else ConnectionError("Operation interrupted")
                # Commit the hold before exposing the error to broad caller handlers.
                try:
                    await asyncio.shield(self._trip(safe_exc, op_id, ledger_request))
                    # Persist delivery evidence before cleanup, which may itself fail.
                    await asyncio.shield(
                        self._finish(
                            op_id, "unknown_delivery" if unknown and is_outbound(ledger_request) else "failed", safe_exc
                        )
                    )
                finally:
                    await asyncio.shield(asyncio.wait_for(super().disconnect(), timeout=10))
                # A cancelled sender future is a transport failure, not a shutdown
                # request for the caller or the other accounts in its controller.
                if isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling():
                    raise
                raise TelegramGatewayError(
                    "FLOOD_WAIT" if flood_seconds(safe_exc) is not None else "SAFETY_RPC_STOPPED",
                    "Telegram operation stopped; manual review required.",
                    {"rpc_error": type(safe_exc).__name__, "seconds": flood_seconds(safe_exc), "operation_id": op_id},
                ) from exc


def create_account_client(
    session_blob: str,
    api_id: int,
    api_hash: str,
    account_id: str,
    proxy,
    *,
    allow_direct: bool = False,
    **kwargs,
):
    if proxy is None and not allow_direct:
        raise TelegramGatewayError("PROXY_REQUIRED", "Account operations require an assigned proxy.")
    stored = StringSession(session_blob)
    if stored.auth_key is None:
        raise TelegramGatewayError("SESSION_INVALID", "Imported session has no auth key.")
    return GuardedTelegramClient(
        stored,
        api_id,
        api_hash,
        account_id=account_id,
        lock_fingerprint=fingerprint(stored.auth_key.key),
        proxy=proxy,
        **kwargs,
    )


def create_login_client(session_blob: str, api_id: int, api_hash: str, phone: str, proxy, **kwargs):
    if proxy is None:
        raise TelegramGatewayError("PROXY_REQUIRED", "Login requires an assigned proxy.")
    stored = StringSession(session_blob)
    login_key = fingerprint(("login:" + "".join(char for char in phone if char in "0123456789")).encode())
    lock_key = fingerprint(stored.auth_key.key) if stored.auth_key else login_key
    return GuardedTelegramClient(
        stored,
        api_id,
        api_hash,
        account_id=None,
        lock_fingerprint=lock_key,
        login_fingerprint=login_key,
        proxy=proxy,
        **kwargs,
    )


async def pause_account(account_id: str, actor_id: str, evidence: str) -> dict[str, Any]:
    """Explicit operator pause; retain Telegram restriction and unresolved-operation evidence."""
    async with AsyncSessionLocal.begin() as db:
        state = await _state(db, account_id)
        await db.refresh(state, with_for_update=True)
        if not state.manual_hold:
            state.manual_hold = True
            state.reason = "operator_paused"
            db.add(
                TelegramOperation(
                    account_id=account_id,
                    method="operator.pause",
                    target=actor_id,
                    outbound=False,
                    outcome="approved",
                    created_at=utcnow(),
                    finished_at=utcnow(),
                )
            )
        # Do not overwrite an existing diagnostic hold or its operation/evidence.
        return state_dict(state)
