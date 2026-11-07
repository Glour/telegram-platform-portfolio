"""Owner-approved appeals with bounded steps. Never releases a campaign."""

from datetime import UTC, datetime, timedelta

EVENT = "spambot_appeal_step"
AUTOMATIC_EXPLANATION = "Please review this account restriction."


def automatic_next(stage: int, reply: dict) -> tuple[str, str] | None:
    """Only known dialog transitions; never guess a response to a new question."""
    if reply.get("classification") == "clear" or stage >= 4:
        return None
    choices = {
        0: {"this is a mistake", "это ошибка", "нет, это ошибка"},
        1: {"yes", "да", "yes, i want to submit a complaint", "да, хочу подать жалобу"},
        2: {
            "no, i'll never do any of this!",
            "no, i’ll never do any of this!",
            "нет, я никогда не буду делать ничего подобного!",
            "нет, ничего подобного не было",
        },
    }
    buttons = reply.get("buttons") or []
    text = (reply.get("reply") or "").casefold()
    if stage == 3:
        if not buttons and any(word in text for word in ("explain", "details", "объясн", "подроб")):
            return "explanation", AUTOMATIC_EXPLANATION
        raise ValueError("SpamBot не запросил объяснение. Автоматический сценарий остановлен.")
    if stage == 1 and not any(word in text for word in ("complaint", "appeal", "жалоб", "обжал")):
        raise ValueError("SpamBot не предложил подать жалобу. Автоматический сценарий остановлен.")
    if stage == 2 and not any(word in text for word in ("spam", "unwanted", "спам", "нежелател")):
        raise ValueError("Неизвестный вопрос подтверждения SpamBot. Автоматический сценарий остановлен.")
    matching = [button for button in buttons if button.strip().casefold() in choices[stage]]
    if len(matching) != 1:
        raise ValueError("Кнопки SpamBot изменились. Автоматический сценарий остановлен.")
    return "button", matching[0]


async def worker_available(workspace_id: str) -> bool:
    from redis.asyncio import Redis

    from app.core.config import get_settings

    try:
        async with Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2) as redis:
            return bool(await redis.get(f"spambot-appeals:worker:{workspace_id}"))
    except Exception:
        return False


def validate_step(action: str, text: str, previous: dict | None, confirmed: bool) -> str:
    if not confirmed:
        raise ValueError("Подтвердите отправку и достоверность сведений.")
    if action == "automatic":
        if text != AUTOMATIC_EXPLANATION:
            raise ValueError("Подтвердите текущий точный текст автоматического обращения.")
        return "/start"
    if action == "start":
        if text:
            raise ValueError("Начало обращения не принимает произвольный текст.")
        return "/start"
    if not previous or previous.get("status") != "reply_received":
        raise ValueError("Сначала дождитесь свежего ответа SpamBot.")
    if action == "button" and text in previous.get("buttons", []):
        return text
    if action == "explanation" and not previous.get("buttons") and 10 <= len(text.strip()) <= 2000:
        return text.strip()
    raise ValueError("Недоступный шаг или некорректное объяснение.")


async def latest_step(db, account_id):
    from sqlalchemy import select

    from app.modules.telegram_accounts.models import AccountHealthEvent

    return await db.scalar(
        select(AccountHealthEvent)
        .where(AccountHealthEvent.account_id == account_id, AccountHealthEvent.event_type == EVENT)
        .order_by(AccountHealthEvent.created_at.desc())
        .limit(1)
    )


def public_step(event):
    if not event:
        return None
    data = event.raw_error or {}
    result = {
        "id": event.id,
        **{
            key: data.get(key)
            for key in ("status", "command", "reply", "buttons", "error", "classification", "automatic", "stage")
        },
    }
    result["can_restart"] = (
        data.get("status") not in {"queued", "processing", "unknown_delivery"}
        and bool(event.created_at)
        and datetime.now(UTC) - event.created_at.replace(tzinfo=UTC) >= timedelta(hours=24)
    )
    if data.get("status") == "processing" and data.get("started_at"):
        started = datetime.fromisoformat(data["started_at"])
        if datetime.now(UTC) - started > timedelta(minutes=5):
            result.update(
                status="unknown_delivery", error="Обработчик прерван. Автоматическая повторная отправка запрещена."
            )
    return result


async def queue_step(db, account, actor_id, action, text, confirmed, previous_id):
    from sqlalchemy import select

    from app.modules.telegram_accounts.models import AccountHealthEvent
    from app.modules.telegram_safety.models import TelegramSafetyState
    from app.modules.workspaces.models import Workspace

    workspace = await db.get(Workspace, account.workspace_id)
    state = await db.get(TelegramSafetyState, account.id, with_for_update=True)
    if not workspace or workspace.owner_user_id != actor_id:
        raise ValueError("Обращение может отправить только владелец workspace.")
    if not await worker_available(account.workspace_id):
        raise ValueError("Обработчик обращений недоступен. Запрос не отправлен.")
    if not state or not state.manual_hold or account.restriction_status not in {"limited", "blocked"}:
        raise ValueError("Нужны подтверждённое ограничение и остановленный аккаунт.")
    previous = await latest_step(db, account.id)
    data = previous.raw_error if previous else None
    if previous and (data or {}).get("status") in {"queued", "processing", "unknown_delivery"}:
        raise ValueError("Предыдущий шаг ещё не завершён или его доставка неизвестна.")
    starting = action in {"start", "automatic"}
    if not starting and (not previous or previous.id != previous_id):
        raise ValueError("Диалог обновился. Загрузите последний ответ.")
    now = datetime.now(UTC)
    if previous and not starting and now - previous.created_at.replace(tzinfo=UTC) > timedelta(minutes=30):
        raise ValueError("Ответ бота устарел. Начните новую проверку позже.")
    events = list(
        await db.scalars(
            select(AccountHealthEvent).where(
                AccountHealthEvent.account_id == account.id,
                AccountHealthEvent.event_type == EVENT,
                AccountHealthEvent.created_at >= now - timedelta(hours=24),
            )
        )
    )
    required_steps = 5 if action == "automatic" else 1
    if len(events) + required_steps > 8 or (
        starting and any((e.raw_error or {}).get("action") in {"start", "automatic"} for e in events)
    ):
        raise ValueError("Достигнут лимит шагов обращения за 24 часа.")
    command = validate_step(action, text, data, confirmed)
    event = AccountHealthEvent(
        account_id=account.id,
        event_type=EVENT,
        severity="info",
        message="Владелец подтвердил один шаг обращения официальному SpamBot.",
        raw_error={
            "status": "queued",
            "actor_id": actor_id,
            "workspace_id": account.workspace_id,
            "action": action,
            "command": command,
            "buttons": [],
            "confirmed": True,
            "expected_reply_id": (data or {}).get("reply_message_id"),
            "previous_sent_id": (data or {}).get("sent_message_id"),
            "automatic": action == "automatic",
            "stage": 0,
            "approved_explanation": AUTOMATIC_EXPLANATION if action == "automatic" else None,
        },
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return public_step(event)


async def process_pending_appeals(workspace_id: str):
    import asyncio
    from uuid import uuid4

    from app.modules.telegram_gateway.service import load_account_session, load_proxy
    from sqlalchemy import select
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetHistoryRequest

    from app.core.config import get_settings
    from app.db.session import AsyncSessionLocal
    from app.modules.telegram_accounts.models import AccountHealthEvent, TelegramAccount
    from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
    from app.modules.telegram_safety.service import fingerprint
    from app.modules.telegram_safety.spambot import PERMIT_METHOD, SpamBotDiagnosticClient, parse_spambot_reply
    from app.modules.workspaces.models import Workspace

    async with AsyncSessionLocal() as db:
        events = list(
            await db.scalars(
                select(AccountHealthEvent)
                .join(TelegramAccount, TelegramAccount.id == AccountHealthEvent.account_id)
                .where(
                    TelegramAccount.workspace_id == workspace_id,
                    AccountHealthEvent.event_type == EVENT,
                    AccountHealthEvent.raw_error["status"].as_string() == "queued",
                )
                .order_by(AccountHealthEvent.created_at)
                .limit(1)
                .with_for_update(of=AccountHealthEvent, skip_locked=True)
            )
        )
        for event in events:
            data = dict(event.raw_error)
            account = await db.get(TelegramAccount, event.account_id)
            workspace = await db.get(Workspace, workspace_id)
            state = await db.get(TelegramSafetyState, account.id)
            if (
                data.get("actor_id") != workspace.owner_user_id
                or not data.get("confirmed")
                or not state
                or not state.manual_hold
                or data.get("workspace_id") != workspace_id
                or datetime.now(UTC) - event.created_at.replace(tzinfo=UTC) > timedelta(minutes=10)
            ):
                event.raw_error = {
                    **data,
                    "status": "failed",
                    "error": "Авторизация или состояние аккаунта изменились.",
                }
                await db.commit()
                continue
            event.raw_error = {**data, "status": "processing", "started_at": datetime.now(UTC).isoformat()}
            permit_id = str(uuid4())
            permit = TelegramOperation(
                id=permit_id,
                account_id=account.id,
                method=PERMIT_METHOD + ".appeal",
                target=workspace.owner_user_id,
                outbound=False,
                outcome="approved",
                created_at=datetime.now(UTC),
            )
            permit.idempotency_key = fingerprint(f"appeal-permit:{permit_id}:{data['command']}".encode())
            db.add(permit)
            await db.commit()
            client = None
            attempted = False
            result = {**data, "status": "unknown_delivery"}
            try:
                async with asyncio.timeout(60):
                    settings = get_settings()
                    client = SpamBotDiagnosticClient(
                        StringSession(await load_account_session(db, account.id)),
                        settings.telegram_api_id,
                        settings.telegram_api_hash,
                        account_id=account.id,
                        actor_id=workspace.owner_user_id,
                        permit_id=permit.id,
                        proxy=await load_proxy(db, account),
                        allow_direct=not account.proxy_required and account.proxy_id is None,
                        appeal_command=data["command"],
                        timeout=settings.telegram_request_timeout_seconds,
                    )
                    await client.connect()
                    bot = await client.get_entity("SpamBot")
                    assert bot.bot and bot.verified and (bot.username or "").lower() == "spambot"
                    client.bot_id = bot.id
                    peer = await client.get_input_entity(bot)
                    if data["action"] not in {"start", "automatic"}:
                        if not data.get("expected_reply_id") or not data.get("previous_sent_id"):
                            raise ValueError("Нет подтверждённого контекста диалога.")
                        client.start_message_id = data["previous_sent_id"]
                        current = await client(
                            GetHistoryRequest(
                                peer=peer,
                                offset_id=0,
                                offset_date=None,
                                add_offset=0,
                                limit=4,
                                max_id=0,
                                min_id=client.start_message_id,
                                hash=0,
                            )
                        )
                        incoming = [
                            m
                            for m in current.messages
                            if not getattr(m, "out", False)
                            and getattr(m, "id", 0) > client.start_message_id
                            and getattr(m, "message", None)
                        ]
                        latest = max(incoming, key=lambda m: m.id) if incoming else None
                        if not latest or latest.id != data["expected_reply_id"]:
                            raise ValueError("Диалог SpamBot изменился. Отправка отменена.")
                        actual_buttons = [
                            b.text
                            for row in getattr(getattr(latest, "reply_markup", None), "rows", [])
                            for b in row.buttons
                            if type(b).__name__ == "KeyboardButton"
                        ]
                        if data["action"] == "button" and data["command"] not in actual_buttons:
                            raise ValueError("Выбранная кнопка больше недоступна.")
                        if data["action"] == "explanation" and actual_buttons:
                            raise ValueError("SpamBot ожидает выбор кнопки, а не объяснение.")
                    attempted = True
                    sent = await client.send_message(bot, data["command"])
                    result.update(status="sent_waiting_reply", sent_message_id=sent.id)
                    for _ in range(2):
                        await asyncio.sleep(3)
                        history = await client(
                            GetHistoryRequest(
                                peer=peer,
                                offset_id=0,
                                offset_date=None,
                                add_offset=0,
                                limit=4,
                                max_id=0,
                                min_id=sent.id,
                                hash=0,
                            )
                        )
                        replies = [
                            m
                            for m in history.messages
                            if not getattr(m, "out", False)
                            and getattr(m, "id", 0) > sent.id
                            and getattr(m, "message", None)
                            and getattr(getattr(m, "from_id", None), "user_id", bot.id) == bot.id
                        ]
                        if replies:
                            reply = max(replies, key=lambda m: m.id)
                            buttons = [
                                b.text
                                for row in getattr(getattr(reply, "reply_markup", None), "rows", [])
                                for b in row.buttons
                                if type(b).__name__ == "KeyboardButton"
                            ]
                            result.update(
                                status="reply_received",
                                reply=reply.message,
                                buttons=buttons,
                                reply_message_id=reply.id,
                                classification=parse_spambot_reply(reply.message),
                            )
                            break
            except Exception as exc:
                result.update(status="unknown_delivery" if attempted else "failed", error=str(exc)[:250])
            finally:
                if client is not None:
                    await client.disconnect()
            if data.get("automatic") and result["status"] == "reply_received":
                try:
                    if data.get("approved_explanation") != AUTOMATIC_EXPLANATION:
                        raise ValueError("Шаблон изменился после подтверждения. Сценарий остановлен.")
                    next_step = automatic_next(data["stage"], result)
                    if next_step:
                        action, command = next_step
                        db.add(
                            AccountHealthEvent(
                                account_id=account.id,
                                event_type=EVENT,
                                severity="info",
                                message="Следующий шаг подтверждённого владельцем сценария SpamBot.",
                                raw_error={
                                    "status": "queued",
                                    "actor_id": data["actor_id"],
                                    "workspace_id": workspace_id,
                                    "action": action,
                                    "command": command,
                                    "buttons": [],
                                    "confirmed": True,
                                    "automatic": True,
                                    "stage": data["stage"] + 1,
                                    "approved_explanation": data["approved_explanation"],
                                    "expected_reply_id": result["reply_message_id"],
                                    "previous_sent_id": result["sent_message_id"],
                                },
                            )
                        )
                    else:
                        result["status"] = "flow_completed"
                except ValueError as exc:
                    result.update(status="automation_stopped", error=str(exc))
            event.raw_error = result
            permit.outcome = (
                "completed"
                if result["status"] in {"reply_received", "sent_waiting_reply", "flow_completed", "automation_stopped"}
                else "failed"
            )
            permit.finished_at = datetime.now(UTC)
            await db.commit()
