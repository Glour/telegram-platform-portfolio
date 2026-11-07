"""Interactive owner-operated Telegram client; no background polling or campaign admission changes."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

from app.modules.telegram_gateway.service import load_account_session, load_proxy
from sqlalchemy import func, select, text
from telethon import utils
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.messages import (
    GetBotCallbackAnswerRequest,
    GetDialogsRequest,
    GetHistoryRequest,
    SendMessageRequest,
)

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.db.session import AsyncSessionLocal, engine
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
from app.modules.telegram_safety.service import (
    RESTRICTED,
    GuardedTelegramClient,
    _bootstrap_correction_kind,
    _owner_admission_valid,
    aware,
    fingerprint,
    flood_seconds,
    unwrap_request,
    utcnow,
)
from app.modules.workspaces.models import Workspace


def fail(code, message):
    raise TelegramGatewayError(code, message)


def seal(account_id, actor_id, kind, value, ttl=1800):
    return encrypt_text(
        json.dumps(
            dict(
                account=account_id,
                actor=actor_id,
                kind=kind,
                expires=(utcnow() + timedelta(seconds=ttl)).timestamp(),
                value=value,
            )
        )
    )


def unseal(token, account_id, actor_id, kind):
    try:
        data = json.loads(decrypt_text(token))
        if data["account"] != account_id or data["actor"] != actor_id or data["kind"] != kind:
            raise ValueError("Reference binding mismatch")
        if data["expires"] <= utcnow().timestamp():
            raise ValueError("Reference expired")
        return data["value"]
    except (ValueError, TypeError, KeyError, AssertionError):
        fail("MANUAL_REFERENCE_EXPIRED", "Обновите диалог: ссылка устарела или принадлежит другому аккаунту.")


def peer_data(peer):
    if isinstance(peer, types.InputPeerUser):
        return dict(kind="user", id=peer.user_id, hash=peer.access_hash)
    if isinstance(peer, types.InputPeerChannel):
        return dict(kind="channel", id=peer.channel_id, hash=peer.access_hash)
    if isinstance(peer, types.InputPeerChat):
        return dict(kind="chat", id=peer.chat_id)
    raise ValueError("Unsupported peer")


def input_peer(value):
    if value["kind"] == "user":
        return types.InputPeerUser(value["id"], value["hash"])
    if value["kind"] == "channel":
        return types.InputPeerChannel(value["id"], value["hash"])
    if value["kind"] == "chat":
        return types.InputPeerChat(value["id"])
    raise ValueError("Unsupported peer")


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return (
            value if parsed.scheme in {"https", "http", "tg"} and (parsed.hostname or parsed.scheme == "tg") else None
        )
    except ValueError:
        return None


def message_view(message, account_id, actor_id, peer):
    body = getattr(message, "message", "") or ""
    encoded = body.encode("utf-16-le")
    links = []
    for entity in getattr(message, "entities", []) or []:
        label = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le", errors="replace")
        url = getattr(entity, "url", None) or (label if isinstance(entity, types.MessageEntityUrl) else None)
        if url and safe_url(url):
            links.append(dict(text=label, url=url))
    buttons = []
    for row in getattr(getattr(message, "reply_markup", None), "rows", []) or []:
        result = []
        for button in row.buttons:
            name = type(button).__name__
            if name == "KeyboardButtonUrl" and safe_url(button.url):
                result.append(dict(text=button.text, url=button.url))
            elif name in {"KeyboardButton", "KeyboardButtonCallback"}:
                action = dict(
                    peer=peer,
                    message_id=message.id,
                    text=button.text,
                    callback=button.data.hex() if name == "KeyboardButtonCallback" else None,
                )
                result.append(dict(text=button.text, action=seal(account_id, actor_id, "button", action, ttl=120)))
            else:
                result.append(dict(text=button.text, unsupported=True))
        if result:
            buttons.append(result)
    return dict(
        id=message.id,
        text=body,
        outgoing=bool(getattr(message, "out", False)),
        date=message.date.isoformat() if getattr(message, "date", None) else None,
        attachment=type(message.media).__name__ if getattr(message, "media", None) else None,
        links=links,
        buttons=buttons,
    )


class ManualClient(GuardedTelegramClient):
    """A two-minute durable permit bound to an operator, one account and exact RPC payload."""

    async def get_me(self, input_peer=False):
        return None

    async def _check(self, *, bootstrap=False):
        if self._safety_stopped or self._safety_connection is None:
            fail("SAFETY_STOPPED", "Соединение закрыто.")
        pid = (await self._safety_connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await self._safety_connection.commit()
        if pid != self._safety_backend_pid:
            fail("SAFETY_LOCK_LOST", "Блокировка сессии потеряна.")
        async with AsyncSessionLocal() as db:
            permit = await db.get(TelegramOperation, self.permit_id)
            account = await db.get(TelegramAccount, self.safety_account_id)
            workspace = await db.get(Workspace, account.workspace_id) if account else None
            allowed = {x.strip() for x in get_settings().telegram_manual_workspaces.split(",") if x.strip()}
            if (
                not workspace
                or workspace.id not in allowed
                or workspace.owner_user_id != self.actor_id
                or not permit
                or permit.account_id != account.id
                or permit.target != self.actor_id
                or permit.method != "operator.manual_telegram"
                or permit.outcome != "approved"
                or aware(permit.created_at) < utcnow() - timedelta(minutes=2)
            ):
                fail("MANUAL_ACCESS_DENIED", "Ручной клиент недоступен для этого workspace или разрешение истекло.")
            state = await db.get(TelegramSafetyState, account.id)
            if not state:
                fail("SAFETY_STATE_REQUIRED", "Для аккаунта не создано состояние безопасности.")
            if state.fingerprint and state.fingerprint != self.safety_fingerprint:
                fail("SAFETY_SESSION_CHANGED", "Сессия аккаунта изменилась.")
            if state.wait_until and aware(state.wait_until) > utcnow():
                fail("SAFETY_WAIT_ACTIVE", f"Ожидание Telegram до {state.wait_until.isoformat()}.")
            pending = await db.scalar(
                select(TelegramOperation.id)
                .where(
                    TelegramOperation.account_id == account.id,
                    TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                )
                .limit(1)
            )
            if pending and bootstrap:
                initial = await db.get(TelegramOperation, pending)
                count = await db.scalar(
                    select(func.count())
                    .select_from(TelegramOperation)
                    .where(
                        TelegramOperation.account_id == account.id,
                        TelegramOperation.outcome.in_(["pending", "unknown_delivery"]),
                    )
                )
                if (
                    count != 1
                    or initial.outcome != "pending"
                    or initial.outbound
                    or initial.method != f"manual.{self.permit_id}.GetConfigRequest"
                ):
                    fail("SAFETY_UNKNOWN_DELIVERY", "Не завершена другая операция.")
            if pending and not bootstrap:
                fail("SAFETY_UNKNOWN_DELIVERY", "Предыдущая операция требует сверки; повторная отправка отключена.")
            if (
                self.writing
                and not self.support_only
                and (
                    state.manual_hold
                    or account.status in RESTRICTED
                    or account.restriction_status in RESTRICTED
                    or not state.operator_approved_at
                    or (state.restriction_status != "clear" and not _owner_admission_valid(state, account))
                )
            ):
                fail("SAFETY_MANUAL_HOLD", "Отправка недоступна. Для SpamBot используйте подтверждённое обращение.")
            if self.writing and not self.support_only and not _owner_admission_valid(state, account):
                if not state.restriction_checked_at or aware(state.restriction_checked_at) < utcnow() - timedelta(
                    hours=24
                ):
                    fail("SAFETY_BASELINE_STALE", "Проверка ограничения устарела; обновите допуск аккаунта.")

    async def _trip(self, exc, op_id=None, request=None):
        # A manual failed read/connection is diagnostic evidence, not a new campaign hold.
        # Genuine account/session errors and ambiguous writes retain the normal transport policy.
        method = type(unwrap_request(request)).__name__ if request is not None else None
        is_read = not self.writing or method in {None, "GetConfigRequest"}
        transient = isinstance(exc, (OSError, ConnectionError, TimeoutError)) or flood_seconds(exc) is not None
        if not (is_read and transient):
            return await super()._trip(exc, op_id, request)
        self._safety_stopped = True
        async with AsyncSessionLocal.begin() as db:
            state = await db.get(TelegramSafetyState, self.safety_account_id, with_for_update=True)
            seconds = flood_seconds(exc)
            if seconds is not None:
                deadline = utcnow() + timedelta(seconds=seconds + 5)
                state.wait_until = max(aware(state.wait_until) or deadline, deadline)
                state.wait_seconds = seconds
            db.add(
                TelegramOperation(
                    account_id=self.safety_account_id,
                    method="operator.manual_read_failure",
                    outbound=False,
                    target=op_id,
                    outcome="failed",
                    error_code=type(exc).__name__,
                    created_at=utcnow(),
                    finished_at=utcnow(),
                )
            )

    async def _handle_bootstrap_notification(self, message):
        states = self._sender._pop_states(message.obj.bad_msg_id)
        kind = _bootstrap_correction_kind(message, states)
        corrected = getattr(self, "_safety_bootstrap_corrections", set())
        if kind and kind not in corrected:
            try:
                await self._check(bootstrap=True)
                async with AsyncSessionLocal.begin() as db:
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
                        or pending[0].method != f"manual.{self.permit_id}.GetConfigRequest"
                        or pending[0].outbound
                    ):
                        fail("MANUAL_BOOTSTRAP_INVALID", "Требуется единственный начальный запрос конфигурации.")
                    db.add(
                        TelegramOperation(
                            account_id=self.safety_account_id,
                            method=f"manual.{self.permit_id}.bootstrap_correction",
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

    async def _begin_operation(self, request):
        request = unwrap_request(request)
        name = type(request).__name__
        if name != "GetConfigRequest" and bytes(request) != self.expected_request:
            fail("MANUAL_RPC_FORBIDDEN", "Запрос не соответствует действию оператора.")
        if self.writing and name != "GetConfigRequest":
            if not self.support_only:
                self.result_operation_id = await super()._begin_operation(request)
                return self.result_operation_id
            if name != "SendMessageRequest":
                fail(
                    "MANUAL_SUPPORT_ONLY", "Для ограниченного аккаунта доступны только сообщения официальному SpamBot."
                )
            async with AsyncSessionLocal.begin() as db:
                await db.get(TelegramSafetyState, self.safety_account_id, with_for_update=True)
                key = fingerprint(f"{self.safety_account_id}:manual-support:{self._safety_business_intent}".encode())
                previous = await db.scalar(
                    select(TelegramOperation.id).where(TelegramOperation.idempotency_key == key).limit(1)
                )
                if previous:
                    fail(
                        "SAFETY_DUPLICATE_OPERATION",
                        "Это действие уже выполнялось. Обновите диалог перед следующей отправкой.",
                    )
                count = await db.scalar(
                    select(func.count())
                    .select_from(TelegramOperation)
                    .where(
                        TelegramOperation.account_id == self.safety_account_id,
                        TelegramOperation.outbound.is_(True),
                        TelegramOperation.target == str(request.peer.user_id),
                        TelegramOperation.created_at >= utcnow() - timedelta(hours=24),
                    )
                )
                if count >= 8:
                    fail("MANUAL_SUPPORT_BUDGET", "Достигнут лимит восьми действий со SpamBot за 24 часа.")
                op = TelegramOperation(
                    account_id=self.safety_account_id,
                    method=name,
                    target=str(request.peer.user_id),
                    outbound=True,
                    random_id=request.random_id,
                    idempotency_key=key,
                    outcome="pending",
                    created_at=utcnow(),
                )
                db.add(op)
                await db.flush()
                self.result_operation_id = op.id
                return op.id
        async with AsyncSessionLocal.begin() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(TelegramOperation)
                .where(
                    TelegramOperation.account_id == self.safety_account_id,
                    TelegramOperation.method.like(f"manual.{self.permit_id}.%"),
                )
            )
            if count >= 4:
                fail("MANUAL_RPC_BUDGET", "Лимит одного ручного действия исчерпан.")
            op = TelegramOperation(
                account_id=self.safety_account_id,
                method=f"manual.{self.permit_id}.{name}",
                outbound=False,
                outcome="pending",
                created_at=utcnow(),
            )
            db.add(op)
            await db.flush()
            return op.id


@asynccontextmanager
async def _connection(db, account, actor_id, request, *, proxy, writing=False, request_id=None, support_only=False):
    settings = get_settings()
    if account.workspace_id not in {x.strip() for x in settings.telegram_manual_workspaces.split(",")}:
        fail("MANUAL_DISABLED", "Ручной клиент ещё не включён для этого workspace.")
    stored = StringSession(await load_account_session(db, account.id))
    if not stored.auth_key:
        fail("SESSION_INVALID", "Сессия недействительна.")
    permit = TelegramOperation(
        account_id=account.id,
        method="operator.manual_telegram",
        target=actor_id,
        outbound=False,
        outcome="approved",
        created_at=utcnow(),
    )
    db.add(permit)
    await db.commit()
    client = ManualClient(
        stored,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        account_id=account.id,
        lock_fingerprint=fingerprint(stored.auth_key.key),
        proxy=proxy,
        timeout=settings.telegram_request_timeout_seconds,
    )
    client.permit_id = permit.id
    client.actor_id = actor_id
    client.writing = writing
    client.support_only = support_only
    client.expected_request = bytes(request)
    client._safety_business_intent = f"manual:{request_id}" if writing else None
    if writing:
        client._safety_intent = f"manual:{request_id}"
    try:
        await client.connect()
        yield client
    finally:
        await client.disconnect()
        permit.outcome = "completed"
        permit.finished_at = utcnow()
        await db.commit()


@asynccontextmanager
async def connection(db, account, actor_id, request, *, direct=False, **kwargs):
    settings = get_settings()
    if account.workspace_id not in {x.strip() for x in settings.telegram_manual_workspaces.split(",")}:
        fail("MANUAL_DISABLED", "Ручной клиент ещё не включён для этого workspace.")
    # Serialize the route selection as well as the MTProto session; never share a leased proxy.
    key = int.from_bytes(bytes.fromhex(fingerprint(f"manual-route:{account.id}".encode()))[:8], "big", signed=True)
    async with engine.connect() as lock:
        acquired = (await lock.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})).scalar_one()
        await lock.commit()
        if not acquired:
            fail("SAFETY_ACCOUNT_BUSY", "Это подключение уже используется. Дождитесь завершения действия.")
        donor_id = None
        proxy_id = None
        lease_id = None
        try:
            # Recover a lease left by a crashed API process only after owning its account route lock.
            configured = [x.strip() for x in settings.telegram_manual_proxy_donors.split(",") if x.strip()]
            async with AsyncSessionLocal.begin() as route_db:
                stale = list(
                    (
                        await route_db.scalars(
                            select(TelegramOperation)
                            .where(
                                TelegramOperation.account_id == account.id,
                                TelegramOperation.method == "operator.manual_proxy_lease",
                                TelegramOperation.outcome == "approved",
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                if len(stale) > 1:
                    fail("MANUAL_ROUTE_CHANGED", "Нужна сверка незавершённых назначений прокси.")
                if stale:
                    old = stale[0]
                    parts = (old.target or "").split(":")
                    if len(parts) != 2 or parts[0] not in configured:
                        fail("MANUAL_ROUTE_CHANGED", "Нужна сверка прежнего назначения прокси.")
                    donor = await route_db.get(TelegramAccount, parts[0], with_for_update=True)
                    target = await route_db.get(TelegramAccount, account.id, with_for_update=True)
                    if not donor or donor.workspace_id != target.workspace_id:
                        fail("MANUAL_ROUTE_CHANGED", "Владелец маршрута изменился.")
                    if target.proxy_id == parts[1] and donor.proxy_id is None:
                        target.proxy_id = None
                        donor.proxy_id = parts[1]
                    elif target.proxy_id is not None or donor.proxy_id != parts[1]:
                        fail(
                            "MANUAL_ROUTE_CHANGED", "Маршрут изменён извне; автоматическое восстановление остановлено."
                        )
                    old.outcome = "completed"
                    old.finished_at = utcnow()
            await db.refresh(account)
            if not direct and account.proxy_id is None and account.proxy_required:
                from app.modules.campaigns.models import Campaign

                configured = [x.strip() for x in settings.telegram_manual_proxy_donors.split(",") if x.strip()]
                async with AsyncSessionLocal.begin() as route_db:
                    target = await route_db.get(TelegramAccount, account.id, with_for_update=True)
                    if target.proxy_id:
                        fail("SAFETY_ACCOUNT_BUSY", "Маршрут подключения изменился. Обновите окно.")
                    campaigns = list(
                        (
                            await route_db.scalars(
                                select(Campaign).where(
                                    Campaign.workspace_id == account.workspace_id, Campaign.status == "running"
                                )
                            )
                        ).all()
                    )
                    for candidate in configured:
                        if candidate == account.id:
                            continue
                        donor = await route_db.get(TelegramAccount, candidate, with_for_update=True)
                        if (
                            not donor
                            or donor.workspace_id != account.workspace_id
                            or not donor.proxy_id
                            or any(candidate in (c.config or {}).get("account_ids", []) for c in campaigns)
                        ):
                            continue
                        proxy_id = donor.proxy_id
                        donor_id = donor.id
                        donor.proxy_id = None
                        target.proxy_id = proxy_id
                        lease = TelegramOperation(
                            account_id=account.id,
                            method="operator.manual_proxy_lease",
                            outbound=False,
                            target=f"{donor.id}:{proxy_id}",
                            outcome="approved",
                            created_at=utcnow(),
                        )
                        route_db.add(lease)
                        await route_db.flush()
                        lease_id = lease.id
                        break
                    if not donor_id:
                        fail(
                            "PROXY_REQUIRED",
                            "Назначьте прокси в карточке аккаунта или дождитесь свободного прокси ручного подключения.",
                        )
                await db.refresh(account)
            proxy = None if direct else await load_proxy(db, account)
            async with _connection(db, account, actor_id, request, proxy=proxy, **kwargs) as client:
                yield client
        finally:
            try:
                if donor_id:
                    async with AsyncSessionLocal.begin() as route_db:
                        target = await route_db.get(TelegramAccount, account.id, with_for_update=True)
                        donor = await route_db.get(TelegramAccount, donor_id, with_for_update=True)
                        if target.proxy_id != proxy_id or donor.proxy_id is not None:
                            fail(
                                "MANUAL_ROUTE_CHANGED",
                                "Назначение прокси изменилось извне; автоматический возврат остановлен.",
                            )
                        target.proxy_id = None
                        donor.proxy_id = proxy_id
                        lease = await route_db.get(TelegramOperation, lease_id)
                        lease.outcome = "completed"
                        lease.finished_at = utcnow()
                    await db.refresh(account)
            finally:
                await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await lock.commit()


async def dialogs(db, account, actor, cursor=None, direct=False):
    offset = unseal(cursor, account.id, actor, "cursor") if cursor else None
    from datetime import datetime

    request = GetDialogsRequest(
        offset_date=datetime.fromisoformat(offset["date"]) if offset else None,
        offset_id=offset["id"] if offset else 0,
        offset_peer=input_peer(offset["peer"]) if offset else types.InputPeerEmpty(),
        limit=50,
        hash=0,
        exclude_pinned=bool(offset),
    )
    async with connection(db, account, actor, request, direct=direct) as client:
        result = await client(request)
    entities = {utils.get_peer_id(e): e for e in [*result.users, *result.chats]}
    messages = {(utils.get_peer_id(m.peer_id), m.id): m for m in result.messages if getattr(m, "peer_id", None)}
    items = []
    next_cursor = None
    for dialog in result.dialogs:
        entity = entities.get(utils.get_peer_id(dialog.peer))
        if entity is None:
            continue
        try:
            peer = peer_data(utils.get_input_peer(entity))
        except (ValueError, TypeError):
            continue
        peer["spambot"] = isinstance(entity, types.User) and bool(
            entity.bot and entity.verified and (entity.username or "").lower() == "spambot"
        )
        message = messages.get((utils.get_peer_id(dialog.peer), dialog.top_message))
        items.append(
            dict(
                title=utils.get_display_name(entity),
                username=getattr(entity, "username", None),
                peer=seal(account.id, actor, "peer", peer),
                unread=dialog.unread_count,
                preview=(getattr(message, "message", "") or "")[:150],
                spambot=isinstance(entity, types.User)
                and bool(entity.bot and entity.verified and (entity.username or "").lower() == "spambot"),
            )
        )
        if message and message.date:
            next_cursor = seal(
                account.id, actor, "cursor", dict(date=message.date.isoformat(), id=message.id, peer=peer)
            )
    return dict(dialogs=items, next_cursor=next_cursor if len(result.dialogs) == 50 else None)


async def history(db, account, actor, peer_token, before=0, direct=False):
    peer = unseal(peer_token, account.id, actor, "peer")
    request = GetHistoryRequest(
        peer=input_peer(peer), offset_id=before, offset_date=None, add_offset=0, limit=50, max_id=0, min_id=0, hash=0
    )
    async with connection(db, account, actor, request, direct=direct) as client:
        result = await client(request)
    current = max((m.id for m in result.messages if isinstance(m, types.Message) and not m.out), default=0)
    messages = []
    for m in result.messages:
        if not isinstance(m, types.Message):
            continue
        view = message_view(m, account.id, actor, peer)
        if isinstance(m.reply_markup, types.ReplyKeyboardMarkup) and (before or m.id != current):
            for row in view["buttons"]:
                for button in row:
                    button["unsupported"] = True
        messages.append(view)
    return dict(
        messages=list(reversed(messages)),
        next_before=min((m.id for m in result.messages if getattr(m, "id", None)), default=0)
        if len(result.messages) == 50
        else None,
    )


async def send(db, account, actor, payload):
    peer = unseal(payload.peer, account.id, actor, "peer")
    if payload.button:
        button = unseal(payload.button, account.id, actor, "button")
        if button["peer"] != peer:
            fail("MANUAL_PEER_MISMATCH", "Кнопка принадлежит другому диалогу.")
        if button["callback"] is not None:
            request = GetBotCallbackAnswerRequest(
                peer=input_peer(peer), msg_id=button["message_id"], data=bytes.fromhex(button["callback"])
            )
        else:
            request = SendMessageRequest(
                peer=input_peer(peer), message=button["text"], random_id=payload.request_id.int % (2**63)
            )
    else:
        request = SendMessageRequest(
            peer=input_peer(peer), message=payload.text, random_id=payload.request_id.int % (2**63)
        )
    async with connection(
        db,
        account,
        actor,
        request,
        direct=payload.direct,
        writing=True,
        request_id=payload.request_id,
        support_only=bool(peer.get("spambot")),
    ) as client:
        result = await client(request)
    operation = await db.get(TelegramOperation, client.result_operation_id)
    if not operation or operation.outcome != "accepted":
        fail("MANUAL_DELIVERY_UNKNOWN", "Результат действия не подтверждён. Не повторяйте отправку до сверки.")
    return dict(status="accepted", message=getattr(result, "message", None), message_id=operation.message_id)
