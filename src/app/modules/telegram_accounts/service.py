import asyncio
import base64
import hashlib
import json
import math
import struct
import tarfile
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.modules.telegram_gateway.service import (
    TelegramGatewayError,
    check_real_account,
    proxy_tuple_for_proxy_id,
    real_telegram_enabled,
)
from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.sessions import SQLiteSession, StringSession

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.errors import bad_request
from app.modules.account_warmup.models import WarmupRun, WarmupStep
from app.modules.campaigns.domain import RUN_ACTIVE_STATUSES
from app.modules.campaigns.models import Campaign, CampaignEvent, CampaignRun
from app.modules.proxies.models import Proxy
from app.modules.proxies.service import create_proxy_records_from_raw_text
from app.modules.telegram_accounts.models import (
    AccountHealthEvent,
    TelegramAccount,
    TelegramSession,
)
from app.modules.telegram_accounts.schemas import (
    TelegramAccountCloneProfileRequest,
    TelegramAccountCloneProfileResponse,
    TelegramAccountImport,
    TelegramAccountLoginCodeResponse,
    TelegramAccountProfilePatch,
    TelegramAccountProfilePhotoResponse,
    TelegramAccountProfilePhotosResponse,
    TelegramAccountProfileResponse,
    TelegramAccountTwoFaUpdate,
    TelegramPhoneLoginCodeConfirm,
    TelegramPhoneLoginCompleteResponse,
    TelegramPhoneLoginPasswordConfirm,
    TelegramPhoneLoginStart,
    TelegramPhoneLoginStartResponse,
    TelegramSessionFileImportMeta,
    TelegramTdataImportMeta,
)
from app.modules.telegram_gateway import service as telegram_gateway
from app.modules.telegram_gateway.contracts import TelegramRestrictionInfo
from app.workers.celery_app import celery_app as celery_app

PROFILE_METADATA_KEY = "profile"
RESTRICTION_METADATA_KEY = "restriction_status"
SESSION_IMPORT_METADATA_KEY = "session_import"
PROFILE_PHOTO_CHECK_INTERVAL = timedelta(hours=6)
ALLOWED_PROFILE_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PROFILE_PHOTO_BYTES = 5 * 1024 * 1024
MAX_STORED_PROFILE_PHOTO_BYTES = MAX_PROFILE_PHOTO_BYTES
MAX_SESSION_FILE_BYTES = 20 * 1024 * 1024
MAX_SESSION_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_SESSION_ARCHIVE_FILES = 300
MAX_TDATA_ARCHIVE_BYTES = 250 * 1024 * 1024
TDATA_KEY_FILE_NAMES = {"key_data", "key_datas", "key_data1", "key_data0"}
PHONE_LOGIN_TOKEN_VERSION = 1
PHONE_LOGIN_REQUEST_TIMEOUT_SECONDS = 120
PHONE_LOGIN_CONNECT_TIMEOUT_SECONDS = 25
PHONE_LOGIN_CODE_REQUEST_TIMEOUT_SECONDS = 35
PHONE_LOGIN_SIGN_IN_TIMEOUT_SECONDS = 45
PHONE_LOGIN_PROFILE_TIMEOUT_SECONDS = 35
PHONE_LOGIN_DISCONNECT_TIMEOUT_SECONDS = 10


def _phone_login_client(settings: object, session_blob: str, proxy: tuple | None, *, phone: str) -> TelegramClient:
    if proxy is None:
        raise bad_request("Telegram phone login requires an assigned proxy.")
    from app.modules.telegram_safety.service import create_login_client

    return create_login_client(
        session_blob,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        phone=phone,
        proxy=proxy,
        request_retries=0,
        connection_retries=0,
        retry_delay=0,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        timeout=max(settings.telegram_request_timeout_seconds, PHONE_LOGIN_REQUEST_TIMEOUT_SECONDS),
    )


async def _connect_phone_login_client(client: TelegramClient) -> None:
    await asyncio.wait_for(client.connect(), timeout=PHONE_LOGIN_CONNECT_TIMEOUT_SECONDS)


async def _disconnect_phone_login_client(client: TelegramClient) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), timeout=PHONE_LOGIN_DISCONNECT_TIMEOUT_SECONDS)
    except (ConnectionError, OSError, TimeoutError):
        return


def _phone_login_timeout_message(stage: str) -> str:
    return (
        f"Telegram слишком долго отвечает на этапе «{stage}». "
        "Результат операции неизвестен. Остановите вход и передайте аккаунт на ручную проверку. "
        "Не запрашивайте новый код, не повторяйте вход и не меняйте прокси до завершения проверки."
    )


async def start_phone_login(
    session: AsyncSession,
    workspace_id: str,
    payload: TelegramPhoneLoginStart,
) -> TelegramPhoneLoginStartResponse:
    if not real_telegram_enabled():
        raise bad_request("Telegram phone login is available only when TELEGRAM_MODE=real.")
    telegram_gateway.ensure_telegram_configured()

    phone = payload.phone.strip()
    settings = get_settings()
    await _apply_new_proxy_to_meta(session, workspace_id, payload)
    if payload.proxy_id:
        await session.commit()
    _ensure_import_has_proxy(payload.proxy_id, payload.new_proxy_raw)
    await _ensure_proxy_capacity_by_id(session, workspace_id, payload.proxy_id)
    proxy = await proxy_tuple_for_proxy_id(session, payload.proxy_id)
    client = _phone_login_client(settings, "", proxy, phone=phone)
    try:
        await _connect_phone_login_client(client)
        sent = await asyncio.wait_for(
            client.send_code_request(phone),
            timeout=PHONE_LOGIN_CODE_REQUEST_TIMEOUT_SECONDS,
        )
        token = _phone_login_token(
            {
                "version": PHONE_LOGIN_TOKEN_VERSION,
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "session_blob": client.session.save(),
                "workspace_id": workspace_id,
                "username": payload.username,
                "display_name": payload.display_name,
                "proxy_id": payload.proxy_id,
                "proxy_required": payload.proxy_required,
                "warmup_status": payload.warmup_status,
                "warmup_score": payload.warmup_score,
            }
        )
        return TelegramPhoneLoginStartResponse(status="code_sent", auth_token=token, phone=phone)
    except FloodWaitError as exc:
        raise bad_request(f"Telegram просит подождать {exc.seconds} сек. перед повторной отправкой кода.") from exc
    except RPCError as exc:
        raise bad_request(f"{type(exc).__name__}: {exc}") from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise bad_request(_phone_login_timeout_message("отправка кода")) from exc
    finally:
        await _disconnect_phone_login_client(client)


async def confirm_phone_login_code(
    session: AsyncSession,
    workspace_id: str,
    payload: TelegramPhoneLoginCodeConfirm,
) -> TelegramPhoneLoginCompleteResponse:
    state = _phone_login_state(payload.auth_token, workspace_id)
    settings = get_settings()
    proxy = await proxy_tuple_for_proxy_id(session, state.get("proxy_id"))
    client = _phone_login_client(settings, state["session_blob"], proxy, phone=state["phone"])
    try:
        await _connect_phone_login_client(client)
        try:
            await asyncio.wait_for(
                client.sign_in(
                    phone=state["phone"],
                    code=payload.code.strip(),
                    phone_code_hash=state["phone_code_hash"],
                ),
                timeout=PHONE_LOGIN_SIGN_IN_TIMEOUT_SECONDS,
            )
        except SessionPasswordNeededError:
            state["session_blob"] = client.session.save()
            return TelegramPhoneLoginCompleteResponse(
                status="password_required",
                auth_token=_phone_login_token(state),
                message="Для аккаунта включен облачный пароль Telegram. Введите 2FA пароль.",
            )
        account = await _save_authorized_phone_account(session, workspace_id, client, state)
        return TelegramPhoneLoginCompleteResponse(status="imported", account=account)
    except FloodWaitError as exc:
        raise bad_request(f"Telegram просит подождать {exc.seconds} сек.") from exc
    except RPCError as exc:
        raise bad_request(f"{type(exc).__name__}: {exc}") from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise bad_request(_phone_login_timeout_message("подтверждение кода")) from exc
    finally:
        await _disconnect_phone_login_client(client)


async def confirm_phone_login_password(
    session: AsyncSession,
    workspace_id: str,
    payload: TelegramPhoneLoginPasswordConfirm,
) -> TelegramPhoneLoginCompleteResponse:
    state = _phone_login_state(payload.auth_token, workspace_id)
    settings = get_settings()
    proxy = await proxy_tuple_for_proxy_id(session, state.get("proxy_id"))
    client = _phone_login_client(settings, state["session_blob"], proxy, phone=state["phone"])
    try:
        await _connect_phone_login_client(client)
        await asyncio.wait_for(
            client.sign_in(password=payload.password),
            timeout=PHONE_LOGIN_SIGN_IN_TIMEOUT_SECONDS,
        )
        account = await _save_authorized_phone_account(session, workspace_id, client, state)
        return TelegramPhoneLoginCompleteResponse(status="imported", account=account)
    except FloodWaitError as exc:
        raise bad_request(f"Telegram просит подождать {exc.seconds} сек.") from exc
    except RPCError as exc:
        raise bad_request(f"{type(exc).__name__}: {exc}") from exc
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise bad_request(_phone_login_timeout_message("подтверждение 2FA")) from exc
    finally:
        await _disconnect_phone_login_client(client)


async def _save_authorized_phone_account(
    session: AsyncSession,
    workspace_id: str,
    client: TelegramClient,
    state: dict,
) -> TelegramAccount:
    if not await asyncio.wait_for(client.is_user_authorized(), timeout=PHONE_LOGIN_PROFILE_TIMEOUT_SECONDS):
        raise bad_request("Telegram authorization was not completed.")
    me = await asyncio.wait_for(client.get_me(), timeout=PHONE_LOGIN_PROFILE_TIMEOUT_SECONDS)
    first_name = getattr(me, "first_name", None) or ""
    last_name = getattr(me, "last_name", None) or ""
    username = getattr(me, "username", None) or state.get("username")
    phone = getattr(me, "phone", None) or state.get("phone")
    display_name = _default_account_display_name(
        state.get("display_name"),
        phone=phone,
        username=username,
        fallback=" ".join(part for part in [first_name, last_name] if part).strip()
        or str(getattr(me, "id", "Telegram account")),
    )
    await _ensure_proxy_capacity_by_id(session, workspace_id, state.get("proxy_id"))
    await client.complete_login()
    account = TelegramAccount(
        workspace_id=workspace_id,
        phone_hash=hashlib.sha256(phone.encode("utf-8")).hexdigest() if phone else None,
        username=username,
        display_name=display_name,
        import_type="phone",
        proxy_id=state.get("proxy_id"),
        proxy_required=bool(state.get("proxy_required", True)),
        status="held",
        health_score=90,
        warmup_score=_import_warmup_score(state),
        warmup_status=_import_warmup_status(state),
    )
    _set_profile_phone_metadata(account, phone)
    session.add(account)
    await session.flush()
    session.add(
        TelegramSession(
            account_id=account.id,
            encrypted_session_blob=encrypt_text(client.session.save()) or "",
        )
    )
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="phone_login_imported",
            severity="info",
            message="Telegram account imported through phone login flow.",
            raw_error={"tg_user_id": getattr(me, "id", None), "username": username},
        )
    )
    await session.commit()
    await session.refresh(account)
    _queue_imported_account_channel_discovery(account)
    return account


def _phone_login_token(state: dict) -> str:
    return encrypt_text(json.dumps(state, ensure_ascii=False)) or ""


def _phone_login_state(token: str, workspace_id: str) -> dict:
    try:
        raw = decrypt_text(token)
        state = json.loads(raw or "{}")
    except Exception as exc:
        raise bad_request("Phone login session expired or invalid. Start login again.") from exc
    if state.get("workspace_id") != workspace_id or state.get("version") != PHONE_LOGIN_TOKEN_VERSION:
        raise bad_request("Phone login session does not match current workspace. Start login again.")
    required = ["phone", "phone_code_hash", "session_blob"]
    if any(not state.get(key) for key in required):
        raise bad_request("Phone login session is incomplete. Start login again.")
    return state


def _default_account_display_name(
    display_name: str | None,
    *,
    phone: str | None = None,
    username: str | None = None,
    fallback: str | None = None,
) -> str | None:
    explicit_name = (display_name or "").strip()
    if explicit_name:
        return explicit_name
    phone_name = (phone or "").strip()
    if phone_name:
        return phone_name
    username_name = (username or "").strip()
    if username_name:
        return username_name
    fallback_name = (fallback or "").strip()
    return fallback_name or None


async def import_session_account(
    session: AsyncSession,
    workspace_id: str,
    payload: TelegramAccountImport,
) -> TelegramAccount:
    duplicate = await _find_duplicate_imported_account(
        session,
        workspace_id,
        phone=payload.phone,
        username=payload.username,
        session_blob=payload.session_blob,
    )
    if duplicate:
        return duplicate

    await _apply_new_proxy_to_meta(session, workspace_id, payload)
    _ensure_import_has_proxy(payload.proxy_id, payload.new_proxy_raw)
    await _ensure_proxy_capacity_by_id(session, workspace_id, payload.proxy_id)
    phone_hash = hashlib.sha256(payload.phone.encode("utf-8")).hexdigest() if payload.phone else None
    account = TelegramAccount(
        workspace_id=workspace_id,
        phone_hash=phone_hash,
        username=payload.username,
        display_name=_default_account_display_name(
            payload.display_name,
            phone=payload.phone,
            username=payload.username,
        ),
        import_type="session",
        proxy_id=payload.proxy_id,
        proxy_required=payload.proxy_required,
        status="new",
        health_score=60 if payload.proxy_id else 45,
        warmup_score=_import_warmup_score(payload),
        warmup_status=_import_warmup_status(payload),
        daily_limits=_session_import_metadata(payload.session_blob),
    )
    _set_profile_phone_metadata(account, payload.phone)
    session.add(account)
    await session.flush()
    session.add(
        TelegramSession(
            account_id=account.id,
            encrypted_session_blob=encrypt_text(payload.session_blob) or "",
        )
    )
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="session_imported",
            severity="info",
            message="Session imported. Run account check to validate it through Telegram.",
        )
    )
    await session.commit()
    await session.refresh(account)
    _queue_imported_account_channel_discovery(account)
    return account


async def import_tdata_account(
    session: AsyncSession,
    workspace_id: str,
    archive: UploadFile,
    meta: TelegramTdataImportMeta,
) -> TelegramAccount:
    with tempfile.TemporaryDirectory(prefix="tgai-tdata-") as tmp:
        tmp_path = Path(tmp)
        archive_path = await _save_upload_file(
            archive,
            tmp_path,
            max_bytes=MAX_TDATA_ARCHIVE_BYTES,
            default_filename="tdata.zip",
        )
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        _extract_tdata_archive(archive_path, extract_dir)
        tdata_dir = _find_tdata_dir(extract_dir)
        if not tdata_dir:
            raise bad_request("Архив должен содержать папку tdata с файлами key_datas/key_data1/key_data0.")

        return await _import_tdata_dir_account(session, workspace_id, tdata_dir, meta)


async def _import_tdata_dir_account(
    session: AsyncSession,
    workspace_id: str,
    tdata_dir: Path,
    meta: TelegramTdataImportMeta,
) -> TelegramAccount:
    if not real_telegram_enabled():
        raise bad_request("TData import is available only when TELEGRAM_MODE=real.")
    telegram_gateway.ensure_telegram_configured()

    try:
        from opentele2.api import API
        from opentele2.td import TDesktop
    except ImportError as exc:
        raise bad_request("TData converter is not installed on the server.") from exc

    tdesk = TDesktop(str(tdata_dir), api=API.TelegramDesktop, passcode=meta.password)
    if not tdesk.isLoaded():
        raise bad_request("Не удалось прочитать tdata. Проверьте архив и пароль Telegram Desktop.")

    session_blob = _offline_tdata_session_blob(tdesk)

    duplicate = await _find_duplicate_imported_account(
        session,
        workspace_id,
        phone=meta.phone,
        username=meta.username,
        session_blob=session_blob,
    )
    if duplicate:
        return duplicate

    phone = meta.phone
    username = meta.username
    display_name = _default_account_display_name(
        meta.display_name,
        phone=phone,
        username=username,
        fallback="Telegram account",
    )

    await _apply_new_proxy_to_meta(session, workspace_id, meta)
    _ensure_import_has_proxy(meta.proxy_id, meta.new_proxy_raw)
    await _ensure_proxy_capacity_by_id(session, workspace_id, meta.proxy_id)
    account = TelegramAccount(
        workspace_id=workspace_id,
        phone_hash=hashlib.sha256(phone.encode("utf-8")).hexdigest() if phone else None,
        username=username,
        display_name=display_name,
        import_type="tdata",
        proxy_id=meta.proxy_id,
        proxy_required=meta.proxy_required,
        status="held",
        health_score=50,
        warmup_score=_import_warmup_score(meta),
        warmup_status=_import_warmup_status(meta),
        daily_limits=_session_import_metadata(session_blob),
    )
    _set_profile_phone_metadata(account, phone)
    session.add(account)
    await session.flush()
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob=encrypt_text(session_blob) or ""))
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type="tdata_imported",
            severity="info",
            message="Telegram Desktop session imported offline; manual admission required.",
            raw_error={"offline_conversion": True, "username": username},
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


def _offline_tdata_session_blob(tdesk: object) -> str:
    try:
        from opentele2.td import MTP
        from telethon.crypto import AuthKey

        account = tdesk.mainAccount
        key = account.authKey.key
        endpoints = account._local.config.endpoints(account.MainDcId)
        endpoint = endpoints[MTP.DcOptions.Address.IPv4][MTP.DcOptions.Protocol.Tcp][0]
        if not isinstance(key, bytes) or len(key) != 256 or endpoint.id != account.MainDcId:
            raise ValueError("Invalid offline auth key or DC")
        stored = StringSession()
        stored.set_dc(endpoint.id, endpoint.ip, endpoint.port)
        stored.auth_key = AuthKey(key)
        return stored.save()
    except (
        ImportError,
        AttributeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        OverflowError,
        struct.error,
    ) as exc:
        raise bad_request(
            "Безопасная офлайн-конвертация tdata недоступна. Предоставьте Telethon .session; "
            "подключение через raw-клиент запрещено до guarded-конвертера."
        ) from exc


async def import_session_file_account(
    session: AsyncSession,
    workspace_id: str,
    session_file: UploadFile,
    meta: TelegramSessionFileImportMeta,
) -> TelegramAccount:
    if not (session_file.filename or "").endswith(".session"):
        raise bad_request("Загрузите Telethon файл с расширением .session.")

    with tempfile.TemporaryDirectory(prefix="tgai-session-") as tmp:
        session_path = await _save_upload_file(
            session_file,
            Path(tmp),
            max_bytes=MAX_SESSION_FILE_BYTES,
            default_filename="telethon.session",
        )
        session_blob = _session_blob_from_sqlite_path(session_path)

    duplicate = await _find_duplicate_imported_account(
        session,
        workspace_id,
        phone=meta.phone,
        username=meta.username,
        session_blob=session_blob,
    )
    if duplicate:
        return duplicate

    await _apply_new_proxy_to_meta(session, workspace_id, meta)
    _ensure_import_has_proxy(meta.proxy_id, meta.new_proxy_raw)
    return await _create_session_file_account(
        session,
        workspace_id,
        session_blob=session_blob,
        meta=meta,
        event_type="session_file_imported",
        event_message="Telethon session file imported. Run account check to validate it through Telegram.",
    )


async def import_session_archive_accounts(
    session: AsyncSession,
    workspace_id: str,
    archive: UploadFile,
    meta: TelegramSessionFileImportMeta,
    proxy_ids: list[str] | None = None,
    *,
    new_proxy_raw_text: str | None = None,
    new_proxy_max_accounts: int = 1,
    auto_assign_free_proxies: bool = False,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="tgai-session-archive-") as tmp:
        tmp_path = Path(tmp)
        archive_path = await _save_upload_file(
            archive,
            tmp_path,
            max_bytes=MAX_SESSION_ARCHIVE_BYTES,
            default_filename="accounts.zip",
            empty_message="Архив аккаунтов пустой.",
        )
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        _extract_session_archive(archive_path, extract_dir)
        session_paths = _session_archive_session_paths(extract_dir)
        if len(session_paths) > MAX_SESSION_ARCHIVE_FILES:
            raise bad_request(f"В одном архиве можно импортировать до {MAX_SESSION_ARCHIVE_FILES} session файлов.")

        requested_proxy_ids = list(proxy_ids or [])
        if auto_assign_free_proxies:
            requested_proxy_ids.extend(await _free_proxy_ids(session, workspace_id))
        if new_proxy_raw_text:
            created_proxies = await create_proxy_records_from_raw_text(
                session,
                workspace_id,
                new_proxy_raw_text,
                default_type="socks5",
                max_accounts=new_proxy_max_accounts,
                note="Импортировано из мастера аккаунтов",
            )
            if created_proxies:
                await session.commit()
            requested_proxy_ids.extend(proxy.id for proxy in created_proxies)
        if meta.proxy_id:
            requested_proxy_ids.insert(0, meta.proxy_id)

        proxy_pool = await _proxy_assignment_pool(session, workspace_id, requested_proxy_ids)

        if not session_paths:
            tdata_dirs = _find_tdata_dirs(extract_dir)
            if not tdata_dirs:
                raise bad_request("Архив должен содержать файлы .session или папку tdata с key_datas/key_data.")
            if meta.proxy_required and _proxy_pool_available_capacity(proxy_pool) < len(tdata_dirs):
                raise bad_request(
                    "Для импорта аккаунтов нужно назначить достаточно свободных мест на прокси. "
                    f"Найдено свободных мест: {_proxy_pool_available_capacity(proxy_pool)}, "
                    f"tdata аккаунтов: {len(tdata_dirs)}. "
                    "Выберите свободные прокси, увеличьте max_accounts осознанно или вставьте новые прокси строками."
                )
            items: list[dict] = []
            for tdata_dir in tdata_dirs:
                proxy_id = _next_available_proxy_id(proxy_pool)
                tdata_meta = TelegramTdataImportMeta(
                    phone=meta.phone,
                    username=meta.username,
                    display_name=meta.display_name or _tdata_import_display_name(tdata_dir, extract_dir),
                    proxy_id=proxy_id,
                    proxy_required=meta.proxy_required,
                    warmup_status=meta.warmup_status,
                    warmup_score=meta.warmup_score,
                )
                try:
                    account = await _import_tdata_dir_account(session, workspace_id, tdata_dir, tdata_meta)
                    if proxy_id and account.proxy_id == proxy_id:
                        proxy_pool[proxy_id]["assigned"] += 1
                    items.append(
                        {
                            "file_name": str(tdata_dir.relative_to(extract_dir)),
                            "status": "imported",
                            "account": account,
                            "error": None,
                        }
                    )
                except Exception as exc:
                    await session.rollback()
                    items.append(
                        {
                            "file_name": str(tdata_dir.relative_to(extract_dir)),
                            "status": "failed",
                            "account": None,
                            "error": str(exc),
                        }
                    )
            return {
                "imported": sum(1 for item in items if item["status"] == "imported"),
                "skipped": 0,
                "failed": sum(1 for item in items if item["status"] == "failed"),
                "items": items,
            }

        metadata_by_stem = _session_archive_metadata(extract_dir)
        items: list[dict] = []
        if meta.proxy_required:
            available_capacity = _proxy_pool_available_capacity(proxy_pool)
            if available_capacity < len(session_paths):
                raise bad_request(
                    "Для импорта аккаунтов нужно назначить достаточно свободных мест на прокси. "
                    f"Найдено свободных мест: {available_capacity}, файлов сессий: {len(session_paths)}. "
                    "Выберите свободные прокси, увеличьте max_accounts осознанно или вставьте новые прокси строками."
                )
        import_batch_id = uuid.uuid4().hex
        import_batch_at = datetime.now(UTC).isoformat()
        import_batch_size = len(session_paths)
        for session_path in session_paths:
            item_meta = _metadata_for_session_path(session_path, meta, metadata_by_stem)
            proxy_id = _next_available_proxy_id(proxy_pool)
            if proxy_id:
                item_meta.proxy_id = proxy_id
                item_meta.proxy_required = True
            elif requested_proxy_ids:
                item_meta.proxy_id = None
                item_meta.proxy_required = True
            try:
                session_blob = _session_blob_from_sqlite_path(session_path)
                duplicate = await _find_duplicate_imported_account(
                    session,
                    workspace_id,
                    phone=item_meta.phone,
                    username=item_meta.username,
                    session_blob=session_blob,
                )
                if duplicate:
                    _set_session_import_metadata(
                        duplicate,
                        session_blob=session_blob,
                        batch_id=import_batch_id,
                        batch_size=import_batch_size,
                        batch_at=import_batch_at,
                        file_name=session_path.name,
                    )
                    _add_health_event(
                        session,
                        duplicate,
                        event_type="session_archive_duplicate_skipped",
                        message=f"Повторный импорт из ZIP пропущен: {session_path.name}.",
                        raw_error={"archive": archive.filename, "file_name": session_path.name},
                    )
                    await session.commit()
                    items.append(
                        {
                            "file_name": session_path.name,
                            "status": "skipped",
                            "account": duplicate,
                            "error": "Аккаунт уже загружен в систему.",
                        }
                    )
                    continue
                account = await _create_session_file_account(
                    session,
                    workspace_id,
                    session_blob=session_blob,
                    meta=item_meta,
                    event_type="session_archive_imported",
                    event_message=f"Telethon session imported from ZIP archive: {session_path.name}.",
                    raw_error={"archive": archive.filename, "file_name": session_path.name},
                    batch_id=import_batch_id,
                    batch_size=import_batch_size,
                    batch_at=import_batch_at,
                    file_name=session_path.name,
                )
                if proxy_id and account.proxy_id == proxy_id:
                    proxy_pool[proxy_id]["assigned"] += 1
                items.append({"file_name": session_path.name, "status": "imported", "account": account, "error": None})
            except Exception as exc:
                await session.rollback()
                items.append({"file_name": session_path.name, "status": "failed", "account": None, "error": str(exc)})

    return {
        "imported": sum(1 for item in items if item["status"] == "imported"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "failed": sum(1 for item in items if item["status"] == "failed"),
        "items": items,
    }


async def _proxy_assignment_pool(
    session: AsyncSession,
    workspace_id: str,
    proxy_ids: list[str],
) -> dict[str, dict[str, int]]:
    clean_ids = list(dict.fromkeys(proxy_id for proxy_id in proxy_ids if proxy_id))
    if not clean_ids:
        return {}

    proxies = list(
        (
            await session.execute(
                select(Proxy).where(
                    Proxy.workspace_id == workspace_id,
                    Proxy.id.in_(clean_ids),
                    Proxy.status != "deleted",
                )
            )
        )
        .scalars()
        .all()
    )
    proxy_by_id = {proxy.id: proxy for proxy in proxies}
    if len(proxy_by_id) != len(clean_ids):
        raise bad_request("Один или несколько прокси не найдены.")

    counts = {
        proxy_id: int(account_count)
        for proxy_id, account_count in (
            await session.execute(
                select(TelegramAccount.proxy_id, func.count(TelegramAccount.id))
                .where(
                    TelegramAccount.workspace_id == workspace_id,
                    TelegramAccount.proxy_id.in_(clean_ids),
                    TelegramAccount.status.notin_(["archived", "deleted"]),
                )
                .group_by(TelegramAccount.proxy_id)
            )
        ).all()
        if proxy_id
    }
    return {
        proxy_id: {
            "assigned": counts.get(proxy_id, 0),
            "max": proxy_by_id[proxy_id].max_accounts,
            "assignable": 1 if proxy_by_id[proxy_id].status in {"active", "new"} else 0,
        }
        for proxy_id in clean_ids
    }


def _proxy_pool_available_capacity(proxy_pool: dict[str, dict[str, int]]) -> int:
    return sum(
        max(0, usage["max"] - usage["assigned"]) for usage in proxy_pool.values() if usage.get("assignable", 1) == 1
    )


async def _free_proxy_ids(session: AsyncSession, workspace_id: str) -> list[str]:
    proxies = list(
        (
            await session.execute(
                select(Proxy)
                .where(Proxy.workspace_id == workspace_id, Proxy.status != "deleted")
                .order_by(Proxy.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not proxies:
        return []
    pool = await _proxy_assignment_pool(session, workspace_id, [proxy.id for proxy in proxies])
    return [
        proxy.id
        for proxy in proxies
        if pool.get(proxy.id, {}).get("assignable", 1) == 1
        and pool.get(proxy.id, {}).get("assigned", 0) < pool.get(proxy.id, {}).get("max", 0)
    ]


def _next_available_proxy_id(proxy_pool: dict[str, dict[str, int]]) -> str | None:
    for proxy_id, usage in proxy_pool.items():
        if usage.get("assignable", 1) == 1 and usage["assigned"] < usage["max"]:
            return proxy_id
    return None


async def _ensure_proxy_capacity_by_id(
    session: AsyncSession,
    workspace_id: str,
    proxy_id: str | None,
) -> None:
    if not proxy_id:
        return
    pool = await _proxy_assignment_pool(session, workspace_id, [proxy_id])
    usage = pool.get(proxy_id)
    if not usage:
        raise bad_request("Прокси не найден.")
    if usage.get("assignable", 1) != 1:
        raise bad_request("Прокси помечен как проблемный. Выберите новый чистый прокси.")
    if usage["assigned"] >= usage["max"]:
        raise bad_request(
            f"Прокси уже достиг лимита {usage['assigned']}/{usage['max']} аккаунтов. "
            "Выберите другой прокси или увеличьте max_accounts осознанно."
        )


async def _apply_new_proxy_to_meta(
    session: AsyncSession,
    workspace_id: str,
    meta: TelegramAccountImport | TelegramPhoneLoginStart | TelegramSessionFileImportMeta | TelegramTdataImportMeta,
) -> None:
    raw = (getattr(meta, "new_proxy_raw", None) or "").strip()
    if not raw:
        return
    proxies = await create_proxy_records_from_raw_text(
        session,
        workspace_id,
        raw,
        default_type="socks5",
        max_accounts=getattr(meta, "new_proxy_max_accounts", 1),
        note="Импортировано из мастера аккаунтов",
    )
    if len(proxies) != 1:
        raise bad_request("Для одиночного импорта укажите ровно один новый прокси.")
    meta.proxy_id = proxies[0].id
    meta.proxy_required = True


def _ensure_import_has_proxy(proxy_id: str | None, new_proxy_raw: str | None = None) -> None:
    if proxy_id:
        return
    if (new_proxy_raw or "").strip():
        return
    raise bad_request("Импорт аккаунта без прокси запрещен. Выберите свободный прокси или вставьте новый прокси.")


def _import_warmup_status(meta: object) -> str:
    value = getattr(meta, "warmup_status", None)
    if isinstance(meta, dict):
        value = meta.get("warmup_status")
    return str(value or "not_started")


def _import_warmup_score(meta: object) -> int:
    value = getattr(meta, "warmup_score", None)
    if isinstance(meta, dict):
        value = meta.get("warmup_score")
    if value is None:
        return 0
    return max(0, min(100, int(value)))


def _session_blob_from_sqlite_path(session_path: Path) -> str:
    sqlite_session = SQLiteSession(str(session_path))
    try:
        session_blob = StringSession.save(sqlite_session)
    except Exception as exc:
        raise bad_request(f"Не удалось прочитать Telethon .session файл {session_path.name}.") from exc
    finally:
        sqlite_session.close()

    if not session_blob:
        raise bad_request(f"Session файл {session_path.name} пустой или не содержит Telegram auth key.")
    return session_blob


async def _create_session_file_account(
    session: AsyncSession,
    workspace_id: str,
    *,
    session_blob: str,
    meta: TelegramSessionFileImportMeta,
    event_type: str,
    event_message: str,
    raw_error: dict | None = None,
    batch_id: str | None = None,
    batch_size: int | None = None,
    batch_at: str | None = None,
    file_name: str | None = None,
) -> TelegramAccount:
    duplicate = await _find_duplicate_imported_account(
        session,
        workspace_id,
        phone=meta.phone,
        username=meta.username,
        session_blob=session_blob,
    )
    if duplicate:
        return duplicate

    await _ensure_proxy_capacity_by_id(session, workspace_id, meta.proxy_id)
    phone_hash = hashlib.sha256(meta.phone.encode("utf-8")).hexdigest() if meta.phone else None
    account = TelegramAccount(
        workspace_id=workspace_id,
        phone_hash=phone_hash,
        username=meta.username,
        display_name=_default_account_display_name(
            meta.display_name,
            phone=meta.phone,
            username=meta.username,
        ),
        import_type="session_file",
        proxy_id=meta.proxy_id,
        proxy_required=meta.proxy_required,
        status="new",
        health_score=60 if meta.proxy_id else 45,
        warmup_score=_import_warmup_score(meta),
        warmup_status=_import_warmup_status(meta),
        daily_limits=_session_import_metadata(
            session_blob,
            batch_id=batch_id,
            batch_size=batch_size,
            batch_at=batch_at,
            file_name=file_name,
        ),
    )
    _set_profile_phone_metadata(account, meta.phone)
    session.add(account)
    await session.flush()
    session.add(TelegramSession(account_id=account.id, encrypted_session_blob=encrypt_text(session_blob) or ""))
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type=event_type,
            severity="info",
            message=event_message,
            raw_error=raw_error,
        )
    )
    await session.commit()
    await session.refresh(account)
    _queue_imported_account_channel_discovery(account)
    return account


async def _find_duplicate_imported_account(
    session: AsyncSession,
    workspace_id: str,
    *,
    phone: str | None = None,
    username: str | None = None,
    session_blob: str | None = None,
) -> TelegramAccount | None:
    phone_hash = hashlib.sha256(phone.encode("utf-8")).hexdigest() if phone else None
    username_clean = _clean_username(username)
    candidates: list[TelegramAccount] = []

    duplicate_candidates_query = select(TelegramAccount).where(
        TelegramAccount.workspace_id == workspace_id,
        TelegramAccount.status != "deleted",
    )

    if phone_hash or username_clean:
        result = await session.execute(duplicate_candidates_query)
        username_lower = username_clean.lower() if username_clean else None
        for account in result.scalars().all():
            if phone_hash and account.phone_hash == phone_hash:
                return account
            if username_lower and (account.username or "").lower() == username_lower:
                return account
            candidates.append(account)

    fingerprint = _session_fingerprint(session_blob)
    if not fingerprint:
        return None
    if not candidates:
        result = await session.execute(duplicate_candidates_query)
        candidates = list(result.scalars().all())
    for account in candidates:
        metadata = account.daily_limits if isinstance(account.daily_limits, dict) else {}
        session_import = metadata.get(SESSION_IMPORT_METADATA_KEY)
        if isinstance(session_import, dict) and session_import.get("fingerprint") == fingerprint:
            return account
    result = await session.execute(
        select(TelegramAccount, TelegramSession)
        .join(TelegramSession, TelegramSession.account_id == TelegramAccount.id)
        .where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status != "deleted",
        )
    )
    for account, telegram_session in result.all():
        try:
            existing_blob = decrypt_text(telegram_session.encrypted_session_blob) or ""
        except Exception:
            continue
        if existing_blob == session_blob or _session_fingerprint(existing_blob) == fingerprint:
            return account
    return None


def _session_fingerprint(session_blob: str | None) -> str | None:
    if not session_blob:
        return None
    return hashlib.sha256(session_blob.encode("utf-8")).hexdigest()


def _session_import_metadata(
    session_blob: str,
    *,
    batch_id: str | None = None,
    batch_size: int | None = None,
    batch_at: str | None = None,
    file_name: str | None = None,
) -> dict:
    fingerprint = _session_fingerprint(session_blob)
    if not fingerprint:
        return {}
    metadata: dict[str, str | int] = {"fingerprint": fingerprint}
    if batch_id:
        metadata["batch_id"] = batch_id
    if batch_size:
        metadata["batch_size"] = batch_size
    if batch_at:
        metadata["batch_at"] = batch_at
    if file_name:
        metadata["file_name"] = file_name
    return {SESSION_IMPORT_METADATA_KEY: metadata}


def _set_session_import_metadata(
    account: TelegramAccount,
    *,
    session_blob: str,
    batch_id: str | None = None,
    batch_size: int | None = None,
    batch_at: str | None = None,
    file_name: str | None = None,
) -> None:
    data = dict(account.daily_limits or {})
    existing = data.get(SESSION_IMPORT_METADATA_KEY)
    metadata = dict(existing) if isinstance(existing, dict) else {}
    metadata.update(
        _session_import_metadata(
            session_blob,
            batch_id=batch_id,
            batch_size=batch_size,
            batch_at=batch_at,
            file_name=file_name,
        ).get(SESSION_IMPORT_METADATA_KEY, {})
    )
    if metadata:
        data[SESSION_IMPORT_METADATA_KEY] = metadata
        account.daily_limits = data


def _queue_imported_account_channel_discovery(account: TelegramAccount) -> None:
    # Intake is offline even when a legacy environment enables auto-discovery.
    return


async def _save_upload_file(
    upload: UploadFile,
    tmp_path: Path,
    *,
    max_bytes: int,
    default_filename: str,
    empty_message: str = "Архив tdata пустой.",
) -> Path:
    filename = Path(upload.filename or default_filename).name
    archive_path = tmp_path / filename
    total = 0
    with archive_path.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise bad_request(f"Файл слишком большой. Максимум {max_bytes // 1024 // 1024} МБ.")
            output.write(chunk)
    if total == 0:
        raise bad_request(empty_message)
    return archive_path


def _extract_session_archive(archive_path: Path, extract_dir: Path) -> None:
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    _ensure_safe_archive_path(extract_dir, member.filename)
                    if Path(member.filename).suffix.lower() == ".session" and member.file_size > MAX_SESSION_FILE_BYTES:
                        limit_mb = MAX_SESSION_FILE_BYTES // 1024 // 1024
                        raise bad_request(f"Файл {Path(member.filename).name} больше {limit_mb} МБ.")
                archive.extractall(extract_dir)
            return
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as archive:
                for member in archive.getmembers():
                    _ensure_safe_archive_path(extract_dir, member.name)
                    if (
                        member.isfile()
                        and Path(member.name).suffix.lower() == ".session"
                        and member.size > MAX_SESSION_FILE_BYTES
                    ):
                        limit_mb = MAX_SESSION_FILE_BYTES // 1024 // 1024
                        raise bad_request(f"Файл {Path(member.name).name} больше {limit_mb} МБ.")
                archive.extractall(extract_dir)
            return
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise bad_request("Не удалось распаковать архив аккаунтов.") from exc
    raise bad_request("Поддерживаются .zip, .tar, .tar.gz и .tgz архивы с .session или tdata.")


def _session_archive_session_paths(extract_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in extract_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() == ".session"
        and not _is_ignored_archive_member(path.relative_to(extract_dir))
    )


def _is_ignored_archive_member(relative_path: Path) -> bool:
    return any(part == "__MACOSX" or part.startswith("._") for part in relative_path.parts)


def _session_archive_metadata(extract_dir: Path) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for path in extract_dir.rglob("*.json"):
        if (
            not path.is_file()
            or path.stat().st_size > 1024 * 1024
            or _is_ignored_archive_member(path.relative_to(extract_dir))
        ):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            metadata[path.stem] = payload
    return metadata


def _metadata_for_session_path(
    session_path: Path,
    fallback: TelegramSessionFileImportMeta,
    metadata_by_stem: dict[str, dict],
) -> TelegramSessionFileImportMeta:
    payload = metadata_by_stem.get(session_path.stem, {})
    phone = _optional_metadata_text(payload, "phone") or _phone_from_session_stem(session_path.stem) or fallback.phone
    username = _optional_metadata_text(payload, "username") or fallback.username
    display_name = (
        _optional_metadata_text(payload, "display_name")
        or _optional_metadata_text(payload, "name")
        or _optional_metadata_text(payload, "first_name")
        or fallback.display_name
    )
    if not display_name and not phone:
        display_name = session_path.stem
    return TelegramSessionFileImportMeta(
        phone=phone,
        username=username,
        display_name=display_name,
        proxy_id=fallback.proxy_id,
        proxy_required=fallback.proxy_required,
        warmup_status=fallback.warmup_status,
        warmup_score=fallback.warmup_score,
    )


def _optional_metadata_text(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _phone_from_session_stem(stem: str) -> str | None:
    cleaned = stem.replace(" ", "")
    if cleaned.startswith("+") and cleaned[1:].isdigit():
        return cleaned
    if cleaned.isdigit() and len(cleaned) >= 8:
        return f"+{cleaned}"
    return None


def _extract_tdata_archive(archive_path: Path, extract_dir: Path) -> None:
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    _ensure_safe_archive_path(extract_dir, member.filename)
                archive.extractall(extract_dir)
            return
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as archive:
                for member in archive.getmembers():
                    _ensure_safe_archive_path(extract_dir, member.name)
                archive.extractall(extract_dir)
            return
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise bad_request("Не удалось распаковать архив tdata.") from exc
    raise bad_request("Поддерживаются только .zip, .tar и .tar.gz архивы с tdata.")


def _ensure_safe_archive_path(base_dir: Path, member_name: str) -> None:
    target = (base_dir / member_name).resolve()
    if not str(target).startswith(str(base_dir.resolve())):
        raise bad_request("Архив tdata содержит небезопасные пути.")


def _find_tdata_dir(root: Path) -> Path | None:
    tdata_dirs = _find_tdata_dirs(root)
    return tdata_dirs[0] if tdata_dirs else None


def _find_tdata_dirs(root: Path) -> list[Path]:
    candidates = {
        path.parent for path in root.rglob("*") if path.is_file() and path.name.lower() in TDATA_KEY_FILE_NAMES
    }
    return sorted(
        candidates,
        key=lambda path: (path.name.lower() != "tdata", len(path.parts), str(path)),
    )


def _tdata_import_display_name(tdata_dir: Path, root: Path) -> str | None:
    try:
        relative = tdata_dir.relative_to(root)
    except ValueError:
        relative = tdata_dir
    parts = [part for part in relative.parts if part.lower() != "tdata"]
    return parts[-1] if parts else tdata_dir.name


async def check_account(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    check_restrictions: bool = True,
    force_restriction_check: bool = False,
) -> TelegramAccount:
    if real_telegram_enabled():
        try:
            info = await check_real_account(session, account)
        except TelegramGatewayError as exc:
            if _is_frozen_account_error(exc):
                _mark_account_frozen(
                    session,
                    account,
                    exc,
                    "Telegram вернул признак замороженного аккаунта при проверке сессии.",
                )
                await _taint_account_proxy(
                    session,
                    account,
                    reason="account_frozen_during_session_check",
                    message="Аккаунт заморожен на этом прокси при проверке сессии.",
                )
                await _pause_campaigns_without_actionable_accounts(
                    session,
                    account,
                    reason="account_frozen_during_session_check",
                )
            else:
                account.status = "failed"
                account.health_score = min(account.health_score, 20)
                _add_health_event(
                    session,
                    account,
                    event_type="account_check_failed",
                    severity="error",
                    message=_friendly_gateway_error(exc),
                    raw_error={"code": exc.code, **exc.raw},
                )
            await session.commit()
            await session.refresh(account)
            return account

        account.username = info.username or account.username
        if info.phone:
            account.phone_hash = hashlib.sha256(info.phone.encode("utf-8")).hexdigest()
            _set_profile_phone_metadata(account, info.phone)

        await _cache_account_profile_snapshot(session, account)

        restriction = _restriction_info_from_metadata(account)
        # Session reachability is not evidence of unrestricted outbound access or approval.

        event_raw = {"tg_user_id": info.tg_user_id, "username": info.username}
        if restriction:
            event_raw["restriction_status"] = restriction.status
            event_raw["restriction_until_text"] = restriction.until_text
            event_raw["restriction_until_at"] = restriction.until_at
            event_raw["restriction_source"] = "historical_or_runtime"
        if check_restrictions or force_restriction_check:
            event_raw["restriction_probe_skipped"] = "active_probe_disabled"

        _add_health_event(
            session,
            account,
            event_type="account_checked",
            severity="warning" if account.status in {"blocked", "spam_blocked", "quarantined", "frozen"} else "info",
            message=_account_check_message(account),
            raw_error=event_raw,
        )
        await session.commit()
        await session.refresh(account)
        return account

    account.health_score = max(account.health_score, 70)
    account.status = "warming" if account.status == "new" else account.status
    _add_health_event(
        session,
        account,
        event_type="account_checked",
        message="Проверка аккаунта выполнена в тестовом режиме.",
    )
    await session.commit()
    await session.refresh(account)
    return account


async def _stop_active_warmup_for_restricted_account(
    session: AsyncSession,
    account: TelegramAccount,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    active_runs = (
        (
            await session.execute(
                select(WarmupRun).where(
                    WarmupRun.telegram_account_id == account.id,
                    WarmupRun.status.in_(["scheduled", "running"]),
                )
            )
        )
        .scalars()
        .all()
    )
    if not active_runs:
        if account.warmup_status in {"scheduled", "running", "warming"}:
            account.warmup_status = "failed"
        return

    run_ids = [run.id for run in active_runs]
    for run in active_runs:
        run.status = "failed"
        run.completed_at = run.completed_at or now
        run.stop_reason = reason
    account.warmup_status = "failed"
    await session.execute(
        update(WarmupStep)
        .where(
            WarmupStep.warmup_run_id.in_(run_ids),
            WarmupStep.status == "queued",
        )
        .values(
            status="cancelled",
            executed_at=now,
            error_code=reason,
            error_message="Прогрев остановлен: Telegram ограничил аккаунт.",
        )
    )


async def _cache_account_profile_snapshot(session: AsyncSession, account: TelegramAccount) -> None:
    if _profile_snapshot_is_fresh(account):
        return
    try:
        profile = await telegram_gateway.get_account_profile(session, account)
    except TelegramGatewayError as exc:
        _mark_profile_photo_checked(account)
        _add_health_event(
            session,
            account,
            event_type="account_profile_cache_failed",
            severity="warning",
            message="Не удалось подтянуть фото профиля Telegram при проверке аккаунта.",
            raw_error={"code": exc.code, **exc.raw},
        )
        return

    account.username = profile.username or account.username
    _set_profile_metadata(
        account,
        first_name=profile.first_name,
        last_name=profile.last_name,
        bio=profile.about,
        phone=getattr(profile, "phone", None),
    )
    _set_profile_photo_data_url_metadata(account, profile.photo_data_url)
    _mark_profile_photo_checked(account)


async def _pause_campaigns_without_actionable_accounts(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(Campaign).where(
            Campaign.workspace_id == account.workspace_id,
            Campaign.status.in_(["pending", "running"]),
        )
    )
    for campaign in result.scalars().all():
        config = campaign.config if isinstance(campaign.config, dict) else {}
        account_ids = [str(item) for item in config.get("account_ids", []) if isinstance(item, str)]
        if account.id not in account_ids:
            continue
        remaining_account_ids = [account_id for account_id in account_ids if account_id != account.id]
        if remaining_account_ids and await _campaign_has_actionable_accounts(
            session,
            account.workspace_id,
            remaining_account_ids,
        ):
            assignment = config.get("source_assignment") if isinstance(config.get("source_assignment"), dict) else {}
            campaign.config = {
                **config,
                "account_ids": remaining_account_ids,
                "source_assignment": {
                    str(account_id): source_ids
                    for account_id, source_ids in assignment.items()
                    if str(account_id) != account.id
                },
                "removed_accounts": [
                    *(config.get("removed_accounts") if isinstance(config.get("removed_accounts"), list) else []),
                    {
                        "account_id": account.id,
                        "account": account.display_name or account.username or account.id,
                        "status": account.status,
                        "restriction_status": account.restriction_status,
                        "reason": reason,
                        "removed_at": now.isoformat(),
                    },
                ][-50:],
            }
            await _record_campaign_account_removed(session, campaign, account, reason=reason, now=now)
            continue
        if config.get("account_waiting_reason") != reason:
            campaign.config = {
                **config,
                "account_waiting_reason": reason,
                "account_waiting_since": now.isoformat(),
            }
        campaign.status = "failed"
        await _fail_campaign_runs_due_to_accounts(session, campaign, account, reason=reason, now=now)


async def _campaign_has_actionable_accounts(session: AsyncSession, workspace_id: str, account_ids: list[str]) -> bool:
    if not account_ids:
        return False
    result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.workspace_id == workspace_id, TelegramAccount.id.in_(account_ids))
    )
    for item in result.scalars().all():
        if item.status in {"active", "ready", "warming"} and item.restriction_status not in {"limited", "blocked"}:
            return True
    return False


async def _record_campaign_account_removed(
    session: AsyncSession,
    campaign: Campaign,
    account: TelegramAccount,
    *,
    reason: str,
    now: datetime,
) -> None:
    result = await session.execute(
        select(CampaignRun).where(
            CampaignRun.workspace_id == campaign.workspace_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(RUN_ACTIVE_STATUSES),
        )
    )
    for run in result.scalars().all():
        run.heartbeat_at = now
        session.add(
            CampaignEvent(
                run_id=run.id,
                campaign_id=campaign.id,
                level="warning",
                event_type="campaign_account_removed",
                message="Замороженный аккаунт удален из кампании, остальные аккаунты продолжают работу.",
                payload={
                    "reason": reason,
                    "account_id": account.id,
                    "account": account.display_name or account.username or account.id,
                },
            )
        )


async def _fail_campaign_runs_due_to_accounts(
    session: AsyncSession,
    campaign: Campaign,
    account: TelegramAccount,
    *,
    reason: str,
    now: datetime,
) -> None:
    result = await session.execute(
        select(CampaignRun).where(
            CampaignRun.workspace_id == campaign.workspace_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(RUN_ACTIVE_STATUSES),
        )
    )
    error_message = (
        "Кампания остановлена: единственный аккаунт заморожен или заблокирован Telegram. "
        f"{account.display_name or account.username or account.id}: {reason}"
    )
    for run in result.scalars().all():
        run.status = "failed"
        run.finished_at = now
        run.heartbeat_at = now
        run.error_code = "ACCOUNT_SAFETY_BLOCKED"
        run.error_message = error_message
        run.report = {
            **(run.report if isinstance(run.report, dict) else {}),
            "status": "failed",
            "finished_at": now.isoformat(),
            "error": {"code": run.error_code, "message": error_message},
        }
        session.add(
            CampaignEvent(
                run_id=run.id,
                campaign_id=campaign.id,
                level="error",
                event_type="campaign_failed_no_active_accounts",
                message=("Кампания остановлена: все привязанные аккаунты сейчас недоступны для Telegram-действий."),
                payload={"reason": reason, "account_id": account.id, "error_code": run.error_code},
            )
        )


async def get_account_profile(session: AsyncSession, account: TelegramAccount) -> TelegramAccountProfileResponse:
    if real_telegram_enabled():
        if _profile_snapshot_is_fresh(account):
            return _local_profile_response(account, can_edit_security=True)
        try:
            profile = await telegram_gateway.get_account_profile(session, account)
        except TelegramGatewayError as exc:
            _add_health_event(
                session,
                account,
                event_type="profile_load_fallback",
                message="Telegram profile could not be refreshed; cached profile was returned.",
                raw_error={"code": exc.code, "message": str(exc)},
                severity="warning",
            )
            await session.commit()
            await session.refresh(account)
            return _local_profile_response(account, can_edit_security=True)
        _set_profile_metadata(
            account,
            first_name=profile.first_name,
            last_name=profile.last_name,
            bio=profile.about,
            phone=profile.phone,
        )
        _set_profile_photo_data_url_metadata(account, profile.photo_data_url)
        await session.commit()
        await session.refresh(account)
        photo_data_url = profile.photo_data_url or account.profile_photo_data_url
        return TelegramAccountProfileResponse(
            account_id=account.id,
            username=profile.username,
            first_name=profile.first_name,
            last_name=profile.last_name,
            display_name=profile.display_name,
            bio=profile.about,
            about=profile.about,
            phone=profile.phone,
            photo_data_url=photo_data_url,
            status=account.status,
            health_score=account.health_score,
            can_edit_security=True,
        )

    return _local_profile_response(account, can_edit_security=False)


async def update_account_profile(
    session: AsyncSession,
    account: TelegramAccount,
    payload: TelegramAccountProfilePatch,
) -> TelegramAccountProfileResponse:
    about = payload.bio if payload.bio is not None else payload.about
    username = _clean_username(payload.username)
    changed_fields = _changed_profile_fields(payload)

    if real_telegram_enabled():
        _ensure_account_allows_real_write(account)
        try:
            profile = await telegram_gateway.update_account_profile(
                session,
                account,
                first_name=payload.first_name,
                last_name=payload.last_name,
                username=username,
                about=about,
            )
        except TelegramGatewayError as exc:
            if _is_frozen_account_error(exc):
                _mark_account_frozen(
                    session,
                    account,
                    exc,
                    "Telegram запретил изменение профиля для замороженного аккаунта.",
                )
                await _taint_account_proxy(
                    session,
                    account,
                    reason="account_frozen_during_profile_update",
                    message="Аккаунт заморожен на этом прокси при изменении профиля.",
                )
                await _pause_campaigns_without_actionable_accounts(
                    session,
                    account,
                    reason="account_frozen_during_profile_update",
                )
                await session.commit()
            raise _gateway_bad_request(exc) from exc
        account.username = profile.username
        account.status = "active"
        account.health_score = max(account.health_score, 90)
        _set_profile_metadata(
            account,
            first_name=profile.first_name,
            last_name=profile.last_name,
            bio=profile.about,
            phone=profile.phone,
        )
        _set_profile_photo_data_url_metadata(account, profile.photo_data_url)
        _add_health_event(
            session,
            account,
            event_type="account_profile_updated",
            message="Telegram profile updated.",
            raw_error={"mode": "real", "fields": changed_fields, "username": profile.username},
        )
        await session.commit()
        await session.refresh(account)
        return TelegramAccountProfileResponse(
            account_id=account.id,
            username=profile.username,
            first_name=profile.first_name,
            last_name=profile.last_name,
            display_name=profile.display_name,
            bio=profile.about,
            about=profile.about,
            phone=profile.phone,
            photo_data_url=profile.photo_data_url,
            status=account.status,
            health_score=account.health_score,
            can_edit_security=True,
        )

    first_name, last_name = _local_name_parts(account)
    if payload.first_name is not None:
        first_name = payload.first_name
    if payload.last_name is not None:
        last_name = payload.last_name
    if username is not None:
        account.username = username
    if about is not None:
        _set_profile_metadata(account, first_name=first_name, last_name=last_name, bio=about)
    else:
        _set_profile_metadata(
            account,
            first_name=first_name,
            last_name=last_name,
            bio=_profile_metadata(account).get("bio"),
        )
    account.status = "warming" if account.status == "new" else account.status
    account.health_score = max(account.health_score, 70)
    _add_health_event(
        session,
        account,
        event_type="account_profile_updated",
        message="Account profile metadata updated in scaffold mode.",
        raw_error={"mode": "scaffold", "fields": changed_fields},
    )
    await session.commit()
    await session.refresh(account)
    return _local_profile_response(account, can_edit_security=False)


async def queue_account_profile_update(
    session: AsyncSession,
    account: TelegramAccount,
    payload: TelegramAccountProfilePatch,
    *,
    request_id: str,
) -> TelegramAccountProfileResponse:
    _ensure_account_allows_real_write(account)
    about = payload.bio if payload.bio is not None else payload.about
    username = _clean_username(payload.username)
    first_name, last_name = _local_name_parts(account)
    if payload.first_name is not None:
        first_name = payload.first_name
    if payload.last_name is not None:
        last_name = payload.last_name
    if username is not None:
        account.username = username
    if about is not None:
        _set_profile_metadata(account, first_name=first_name, last_name=last_name, bio=about)
    else:
        _set_profile_metadata(
            account,
            first_name=first_name,
            last_name=last_name,
            bio=_profile_metadata(account).get("bio"),
        )
    account.status = "warming" if account.status == "new" else account.status
    _add_health_event(
        session,
        account,
        event_type="account_profile_update_queued",
        message="Telegram profile update queued.",
        raw_error={
            "mode": "queued",
            "request_id": request_id,
            "fields": _changed_profile_fields(payload),
        },
    )
    await session.commit()
    await session.refresh(account)
    return _local_profile_response(account, can_edit_security=real_telegram_enabled())


async def update_account_profile_photo(
    session: AsyncSession,
    account: TelegramAccount,
    photo: UploadFile,
) -> dict[str, str]:
    content_type = photo.content_type or ""
    body = await photo.read(MAX_PROFILE_PHOTO_BYTES + 1)
    return await update_account_profile_photo_bytes(
        session,
        account,
        photo=body,
        file_name=photo.filename or "profile-photo.jpg",
        content_type=content_type,
    )


async def update_account_profile_photo_bytes(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    photo: bytes,
    file_name: str,
    content_type: str,
) -> dict[str, str]:
    if content_type not in ALLOWED_PROFILE_PHOTO_CONTENT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_PROFILE_PHOTO_CONTENT_TYPES))
        raise bad_request(f"Profile photo must be one of: {allowed}.")

    if len(photo) > MAX_PROFILE_PHOTO_BYTES:
        raise bad_request("Profile photo must be 5 MB or smaller.")

    mode = "real" if real_telegram_enabled() else "scaffold"
    if real_telegram_enabled():
        _ensure_account_allows_real_write(account)
        try:
            await telegram_gateway.update_account_profile_photo(
                session,
                account,
                photo=photo,
                file_name=file_name,
            )
        except TelegramGatewayError as exc:
            if _is_frozen_account_error(exc):
                _mark_account_frozen(
                    session,
                    account,
                    exc,
                    "Telegram запретил изменение фото для замороженного аккаунта.",
                )
                await _taint_account_proxy(
                    session,
                    account,
                    reason="account_frozen_during_photo_update",
                    message="Аккаунт заморожен на этом прокси при изменении фото.",
                )
                await _pause_campaigns_without_actionable_accounts(
                    session,
                    account,
                    reason="account_frozen_during_photo_update",
                )
                await session.commit()
            raise _gateway_bad_request(exc) from exc

    _add_health_event(
        session,
        account,
        event_type="account_profile_photo_updated",
        message=f"Account profile photo updated in {mode} mode.",
        raw_error={"mode": mode, "content_type": content_type, "bytes": len(photo)},
    )
    _set_profile_photo_metadata(account, photo=photo, content_type=content_type)
    await session.commit()
    return {"status": "ok", "account_id": account.id, "mode": mode}


async def list_account_profile_photos(
    session: AsyncSession,
    account: TelegramAccount,
) -> TelegramAccountProfilePhotosResponse:
    if real_telegram_enabled():
        try:
            photos = await telegram_gateway.list_account_profile_photos(session, account)
        except TelegramGatewayError as exc:
            raise _gateway_bad_request(exc) from exc
        current_photo = next((photo for photo in photos if photo.is_current and photo.photo_data_url), None)
        if current_photo:
            _set_profile_photo_data_url_metadata(account, current_photo.photo_data_url)
            await session.commit()
            await session.refresh(account)
        return TelegramAccountProfilePhotosResponse(
            photos=[
                TelegramAccountProfilePhotoResponse(
                    photo_id=photo.photo_id,
                    date=photo.date,
                    photo_data_url=photo.photo_data_url,
                    is_current=photo.is_current,
                )
                for photo in photos
            ]
        )

    photo_data_url = _profile_metadata(account).get("photo_data_url")
    if not isinstance(photo_data_url, str) or not photo_data_url.startswith("data:image/"):
        return TelegramAccountProfilePhotosResponse(photos=[])
    return TelegramAccountProfilePhotosResponse(
        photos=[
            TelegramAccountProfilePhotoResponse(
                photo_id="local-current",
                date=account.updated_at,
                photo_data_url=photo_data_url,
                is_current=True,
            )
        ]
    )


async def delete_account_profile_photo(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    photo_id: str,
) -> TelegramAccountProfilePhotosResponse:
    mode = "real" if real_telegram_enabled() else "scaffold"
    if real_telegram_enabled():
        _ensure_account_allows_real_write(account)
        try:
            await telegram_gateway.delete_account_profile_photo(session, account, photo_id=photo_id)
        except TelegramGatewayError as exc:
            if _is_frozen_account_error(exc):
                _mark_account_frozen(
                    session,
                    account,
                    exc,
                    "Telegram запретил удаление фото для замороженного аккаунта.",
                )
                await _taint_account_proxy(
                    session,
                    account,
                    reason="account_frozen_during_photo_delete",
                    message="Аккаунт заморожен на этом прокси при удалении фото профиля.",
                )
                await _pause_campaigns_without_actionable_accounts(
                    session,
                    account,
                    reason="account_frozen_during_photo_delete",
                )
                await session.commit()
            raise _gateway_bad_request(exc) from exc
    elif photo_id != "local-current":
        raise bad_request("Profile photo was not found.")

    _add_health_event(
        session,
        account,
        event_type="account_profile_photo_deleted",
        message=f"Account profile photo deleted in {mode} mode.",
        raw_error={"mode": mode, "photo_id": photo_id},
    )
    _clear_profile_photo_metadata(account)
    await session.commit()
    await session.refresh(account)
    if real_telegram_enabled():
        return await list_account_profile_photos(session, account)
    return TelegramAccountProfilePhotosResponse(photos=[])


async def request_account_login_code(
    session: AsyncSession,
    account: TelegramAccount,
) -> TelegramAccountLoginCodeResponse:
    if not real_telegram_enabled():
        return TelegramAccountLoginCodeResponse(
            status="queued",
            account_id=account.id,
            phone=account.phone or _profile_metadata(account).get("phone"),
            message="Scaffold mode: код входа не отправлялся.",
        )
    try:
        result = await telegram_gateway.request_account_login_code(session, account)
    except TelegramGatewayError as exc:
        raise _gateway_bad_request(exc) from exc
    _set_profile_phone_metadata(account, result.phone)
    _add_health_event(
        session,
        account,
        event_type="account_login_code_requested",
        message="Telegram login code read from service chat.",
        raw_error={
            "phone": result.phone,
            "has_login_code": bool(result.login_code),
            "message_date": result.message_date.isoformat() if result.message_date else None,
        },
    )
    await session.commit()
    return TelegramAccountLoginCodeResponse(
        status="found",
        account_id=account.id,
        phone=result.phone,
        login_code=result.login_code,
        message_date=result.message_date,
        message="Код входа найден в служебном чате Telegram.",
    )


def generated_profile_for_account(
    account: TelegramAccount,
    *,
    index: int,
    style_prompt: str,
    profile_gender: str = "female",
) -> dict[str, str | bytes]:
    gender = profile_gender if profile_gender in {"female", "male"} else "female"
    seed = int(hashlib.sha256(f"{account.id}:{index}:{style_prompt}:{gender}".encode()).hexdigest()[:8], 16)
    female = gender == "female"
    first_names = (
        [
            "Анна",
            "Мария",
            "София",
            "Алиса",
            "Екатерина",
            "Полина",
            "Виктория",
            "Дарья",
            "Кира",
            "Ева",
        ]
        if female
        else [
            "Алексей",
            "Михаил",
            "Даниил",
            "Илья",
            "Кирилл",
            "Артем",
            "Максим",
            "Никита",
            "Роман",
            "Егор",
        ]
    )
    last_names = (
        [
            "Миронова",
            "Соколова",
            "Волкова",
            "Кузнецова",
            "Орлова",
            "Новикова",
            "Лебедева",
            "Федорова",
        ]
        if female
        else [
            "Миронов",
            "Соколов",
            "Волков",
            "Кузнецов",
            "Орлов",
            "Новиков",
            "Лебедев",
            "Федоров",
        ]
    )
    bio_parts = [
        "пишу коротко и по делу",
        "люблю живые разговоры",
        "смотрю на вещи спокойно",
        "интересуюсь городом, людьми и проектами",
        "без лишнего шума",
    ]
    first_name = first_names[seed % len(first_names)]
    last_name = last_names[(seed // 7) % len(last_names)]
    bio = f"{bio_parts[(seed // 11) % len(bio_parts)]}. {style_prompt.strip()[:48]}".strip()
    if len(bio) > 68:
        bio = bio[:68].rsplit(" ", 1)[0].rstrip()
    return {
        "first_name": first_name,
        "last_name": last_name,
        "bio": bio,
        "gender": "female" if female else "male",
        "photo": _generated_profile_photo_png(seed, female=female),
    }


def _generated_profile_photo_png(seed: int, *, female: bool) -> bytes:
    size = 512
    bg = [(222, 232, 244), (236, 226, 218), (218, 234, 226), (232, 224, 240)][seed % 4]
    skin = [(226, 178, 139), (205, 145, 104), (238, 196, 161), (170, 112, 82)][(seed // 3) % 4]
    hair = [(48, 34, 28), (84, 56, 38), (158, 108, 62), (30, 30, 34)][(seed // 5) % 4]
    shirt = [(31, 87, 137), (28, 126, 107), (122, 63, 146), (132, 71, 54)][(seed // 9) % 4]
    pixels = [bytearray(size * 3) for _ in range(size)]
    for y in range(size):
        for x in range(size):
            shade = int(18 * (x + y) / (size * 2))
            _set_px(pixels, x, y, (min(bg[0] + shade, 255), min(bg[1] + shade, 255), min(bg[2] + shade, 255)))
    _ellipse(pixels, 118, 340, 394, 560, shirt)
    _ellipse(pixels, 198, 246, 314, 386, skin)
    _ellipse(pixels, 142, 92, 370, 326, skin)
    if female:
        _ellipse(pixels, 118, 64, 394, 310, hair)
        _ellipse(pixels, 154, 104, 358, 330, skin)
        _ellipse(pixels, 132, 72, 246, 188, hair)
    else:
        _ellipse(pixels, 138, 62, 374, 220, hair)
        _ellipse(pixels, 148, 104, 364, 330, skin)
    _ellipse(pixels, 190, 214, 216, 238, (32, 34, 38))
    _ellipse(pixels, 296, 214, 322, 238, (32, 34, 38))
    _ellipse(pixels, 196, 220, 208, 232, (246, 248, 250))
    _ellipse(pixels, 302, 220, 314, 232, (246, 248, 250))
    _arc_mouth(pixels, 220, 274, 292, 310, (126, 54, 58))
    return _png_rgb(b"".join(bytes([0]) + bytes(row) for row in pixels), size, size)


def _set_px(pixels: list[bytearray], x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= y < len(pixels) and 0 <= x < len(pixels[y]) // 3:
        offset = x * 3
        pixels[y][offset : offset + 3] = bytes(color)


def _ellipse(pixels: list[bytearray], x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    rx = max((x2 - x1) / 2, 1)
    ry = max((y2 - y1) / 2, 1)
    for y in range(max(y1, 0), min(y2, len(pixels))):
        for x in range(max(x1, 0), min(x2, len(pixels[y]) // 3)):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1:
                _set_px(pixels, x, y, color)


def _arc_mouth(pixels: list[bytearray], x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
    for x in range(x1, x2):
        t = (x - x1) / max(x2 - x1, 1)
        y = int(y1 + math.sin(t * math.pi) * (y2 - y1))
        for dy in range(3):
            _set_px(pixels, x, y + dy, color)


def _png_rgb(raw_scanlines: bytes, width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        import zlib

        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    import zlib

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw_scanlines, 9)),
            chunk(b"IEND", b""),
        ]
    )


async def clone_account_profile(
    session: AsyncSession,
    account: TelegramAccount,
    payload: TelegramAccountCloneProfileRequest,
    *,
    source_account: TelegramAccount | None = None,
) -> TelegramAccountCloneProfileResponse:
    if not payload.source_ref and not source_account:
        raise bad_request("Укажите источник профиля: username/id или импортированный аккаунт.")
    if source_account and source_account.id == account.id:
        raise bad_request("Источник и целевой аккаунт совпадают. Выберите другой аккаунт для клонирования.")

    if real_telegram_enabled():
        _ensure_account_allows_real_write(account)
        try:
            clone_result = await telegram_gateway.clone_account_profile(
                session,
                account,
                source_ref=payload.source_ref,
                source_account=source_account,
                copy_name=payload.copy_name,
                copy_bio=payload.copy_bio,
                copy_photo=payload.copy_photo,
                copy_stories=payload.copy_stories,
                pin_stories=payload.pin_stories,
                delete_old_stories=payload.delete_old_stories,
                story_limit=payload.story_limit,
            )
        except TelegramGatewayError as exc:
            if _is_frozen_account_error(exc):
                _mark_account_frozen(
                    session,
                    account,
                    exc,
                    "Telegram запретил клонирование профиля для замороженного аккаунта.",
                )
                await _taint_account_proxy(
                    session,
                    account,
                    reason="account_frozen_during_profile_clone",
                    message="Аккаунт заморожен на этом прокси при клонировании профиля.",
                )
                await _pause_campaigns_without_actionable_accounts(
                    session,
                    account,
                    reason="account_frozen_during_profile_clone",
                )
                await session.commit()
            raise _gateway_bad_request(exc) from exc

        profile = clone_result.profile
        account.username = profile.username or account.username
        account.status = "active"
        account.health_score = max(account.health_score, 90)
        _set_profile_metadata(
            account,
            first_name=profile.first_name,
            last_name=profile.last_name,
            bio=profile.about,
        )
        _add_health_event(
            session,
            account,
            event_type="account_profile_cloned",
            message="Telegram profile cloned from source account.",
            raw_error={
                "mode": "real",
                "source_ref": payload.source_ref,
                "source_account_id": source_account.id if source_account else None,
                "copied_fields": clone_result.copied_fields,
                "skipped_fields": clone_result.skipped_fields,
                "warnings": clone_result.warnings,
            },
        )
        await session.commit()
        await session.refresh(account)
        profile_response = TelegramAccountProfileResponse(
            account_id=account.id,
            username=profile.username,
            first_name=profile.first_name,
            last_name=profile.last_name,
            display_name=profile.display_name,
            bio=profile.about,
            about=profile.about,
            phone=profile.phone,
            photo_data_url=profile.photo_data_url,
            status=account.status,
            health_score=account.health_score,
            can_edit_security=True,
        )
        return TelegramAccountCloneProfileResponse(
            status="ok",
            account_id=account.id,
            source_ref=payload.source_ref,
            source_account_id=source_account.id if source_account else None,
            mode="real",
            copied_fields=clone_result.copied_fields,
            skipped_fields=clone_result.skipped_fields,
            warnings=clone_result.warnings,
            profile=profile_response,
        )

    if not source_account:
        raise bad_request("В тестовом режиме выберите источник из уже импортированных аккаунтов.")

    source_profile = _local_profile_response(source_account, can_edit_security=False)
    copied_fields: list[str] = []
    skipped_fields: list[str] = ["username"]
    warnings: list[str] = []
    first_name, last_name = _local_name_parts(account)
    bio = _profile_metadata(account).get("bio")

    if payload.copy_name:
        if source_profile.first_name or source_profile.last_name:
            first_name = source_profile.first_name
            last_name = source_profile.last_name or ""
            copied_fields.append("name")
        else:
            skipped_fields.append("name")

    if payload.copy_bio:
        bio = source_profile.about if source_profile.about is not None else source_profile.bio
        bio = bio or ""
        copied_fields.append("bio")

    if payload.copy_photo:
        skipped_fields.append("photo")
        warnings.append("Фото профиля копируется только в реальном Telegram-режиме.")
    if payload.copy_stories:
        skipped_fields.append("stories")
        warnings.append("Сторис копируются только в реальном Telegram-режиме.")

    _set_profile_metadata(account, first_name=first_name, last_name=last_name, bio=bio)
    if payload.copy_name and (first_name or last_name):
        copied_name = " ".join(part for part in [first_name, last_name] if part).strip()
        account.display_name = copied_name or account.display_name
    account.status = "warming" if account.status == "new" else account.status
    account.health_score = max(account.health_score, 70)
    _add_health_event(
        session,
        account,
        event_type="account_profile_cloned",
        message="Account profile cloned in scaffold mode.",
        raw_error={
            "mode": "scaffold",
            "source_ref": payload.source_ref,
            "source_account_id": source_account.id,
            "copied_fields": copied_fields,
            "skipped_fields": skipped_fields,
            "warnings": warnings,
        },
    )
    await session.commit()
    await session.refresh(account)
    return TelegramAccountCloneProfileResponse(
        status="ok",
        account_id=account.id,
        source_ref=payload.source_ref,
        source_account_id=source_account.id,
        mode="scaffold",
        copied_fields=copied_fields,
        skipped_fields=skipped_fields,
        warnings=warnings,
        profile=_local_profile_response(account, can_edit_security=False),
    )


async def update_account_2fa(
    session: AsyncSession,
    account: TelegramAccount,
    payload: TelegramAccountTwoFaUpdate,
) -> dict[str, str]:
    if payload.confirmation_text != "CHANGE 2FA":
        raise bad_request('Type "CHANGE 2FA" to confirm 2FA changes.')

    action = "enabled" if payload.new_password else "disabled"
    mode = "real" if real_telegram_enabled() else "scaffold"
    if real_telegram_enabled():
        _ensure_account_allows_real_write(account)
        try:
            await telegram_gateway.update_account_2fa(
                session,
                account,
                current_password=payload.current_password,
                new_password=payload.new_password,
                hint=payload.hint,
            )
        except TelegramGatewayError as exc:
            raise _gateway_bad_request(exc) from exc

    _add_health_event(
        session,
        account,
        event_type="account_2fa_updated",
        message=f"Account 2FA {action} in {mode} mode.",
        raw_error={"mode": mode, "action": action},
    )
    await session.commit()
    return {"status": "ok", "account_id": account.id, "mode": mode, "action": action}


def _changed_profile_fields(payload: TelegramAccountProfilePatch) -> list[str]:
    fields: list[str] = []
    for field_name in ["first_name", "last_name", "username", "bio", "about"]:
        if getattr(payload, field_name) is not None:
            fields.append(field_name)
    return fields


def _clean_username(username: str | None) -> str | None:
    if username is None:
        return None
    username = username.removeprefix("@").strip()
    return username or None


def _is_frozen_account_error(exc: TelegramGatewayError) -> bool:
    code = exc.code.lower()
    message = str(exc).lower()
    return "frozen" in code or "frozen account" in message or "frozen accounts" in message


def _ensure_account_allows_real_write(account: TelegramAccount) -> None:
    if account.status in {"frozen", "quarantined", "deleted", "archived", "spam_blocked", "blocked"}:
        raise bad_request(
            "Аккаунт заморожен, ограничен, в карантине или отключён. "
            "Не выполняю Telegram-действие, чтобы не усилить ограничения."
        )


def _mark_account_frozen(
    session: AsyncSession,
    account: TelegramAccount,
    exc: TelegramGatewayError,
    message: str,
) -> None:
    now = datetime.now(UTC)
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    account.status = "frozen"
    account.health_score = min(account.health_score, 10)
    account.daily_limits = {
        **data,
        "restriction_status": {
            **(data.get("restriction_status") if isinstance(data.get("restriction_status"), dict) else {}),
            "status": "frozen",
            "label": "Аккаунт заморожен Telegram",
            "checked_at": now.isoformat(),
            "source": "telegram_gateway",
        },
        "last_frozen_error_at": now.isoformat(),
        "last_frozen_error_code": exc.code,
    }
    _add_health_event(
        session,
        account,
        event_type="account_frozen_detected",
        severity="error",
        message=message,
        raw_error={"code": exc.code, **exc.raw},
    )


async def _taint_account_proxy(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    reason: str,
    message: str,
) -> None:
    if not account.proxy_id:
        return
    proxy = await session.get(Proxy, account.proxy_id)
    if not proxy or proxy.status == "deleted":
        return
    proxy.status = "tainted"
    proxy.last_error_code = reason
    proxy.last_error_message = message


def _gateway_bad_request(exc: TelegramGatewayError):
    return bad_request(_friendly_gateway_error(exc))


def _friendly_gateway_error(exc: TelegramGatewayError) -> str:
    code = exc.code.lower()
    message = str(exc).lower()
    if _is_frozen_account_error(exc):
        return (
            "Telegram заморозил аккаунт и временно запретил это действие. "
            "Не меняйте профиль и не запускайте активные кампании с этого аккаунта; "
            "проверьте аккаунт в официальном Telegram."
        )
    if "flood" in code or "flood" in message or "wait" in message:
        seconds = exc.raw.get("seconds")
        if seconds:
            return f"Telegram временно ограничил частые действия аккаунта. Повторите через {seconds} сек."
        return "Telegram временно ограничил частые действия аккаунта. Нужно подождать и повторить позже."
    if "session_unauthorized" in code or "not authorized" in message or "expired" in message:
        return "Telegram-сессия больше не авторизована. Нужно заново подключить аккаунт."
    if "session_invalid" in code or "session string is invalid" in message:
        return "Telegram-сессия повреждена. Импортируйте аккаунт заново."
    if "login_code_not_found" in code:
        return (
            "Код входа не найден. Сначала запросите код на телефоне, дождитесь сообщения от Telegram, "
            "затем нажмите «Код входа» ещё раз."
        )
    return f"{exc.code}: {exc}"


def _restriction_info_from_metadata(account: TelegramAccount) -> TelegramRestrictionInfo | None:
    metadata = _restriction_metadata(account)
    status = metadata.get("status")
    checked_at = metadata.get("checked_at")
    if not isinstance(status, str) or not isinstance(checked_at, str):
        return None
    return TelegramRestrictionInfo(
        status=status,
        label=str(metadata.get("label") or ""),
        raw_text=metadata.get("raw_text") if isinstance(metadata.get("raw_text"), str) else None,
        checked_at=checked_at,
        until_text=metadata.get("until_text") if isinstance(metadata.get("until_text"), str) else None,
        until_at=metadata.get("until_at") if isinstance(metadata.get("until_at"), str) else None,
    )


def _restriction_metadata(account: TelegramAccount) -> dict:
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    metadata = data.get(RESTRICTION_METADATA_KEY)
    return metadata if isinstance(metadata, dict) else {}


def _account_check_message(account: TelegramAccount) -> str:
    if account.status == "frozen":
        return "Проверка выполнена: аккаунт помечен как замороженный Telegram. Активные действия лучше не запускать."
    if account.status in {"blocked", "spam_blocked", "quarantined"}:
        return "Проверка выполнена: сессия доступна, но аккаунт остается в ограниченном статусе по runtime-событиям."
    return "Проверка выполнена: сессия живая. Ограничения фиксируются по фактическим ошибкам действий."


def _profile_metadata(account: TelegramAccount) -> dict:
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    metadata = data.get(PROFILE_METADATA_KEY)
    return metadata if isinstance(metadata, dict) else {}


def _set_profile_metadata(
    account: TelegramAccount,
    *,
    first_name: str | None,
    last_name: str | None,
    bio: str | None,
    phone: str | None = None,
) -> None:
    data = dict(account.daily_limits or {})
    existing = _profile_metadata(account)
    next_profile = {
        **existing,
        "first_name": first_name,
        "last_name": last_name,
        "bio": bio,
    }
    if phone:
        next_profile["phone"] = phone
    data[PROFILE_METADATA_KEY] = next_profile
    account.daily_limits = data


def _set_profile_phone_metadata(account: TelegramAccount, phone: str | None) -> None:
    phone = (phone or "").strip()
    if not phone:
        return
    data = dict(account.daily_limits or {})
    profile = dict(_profile_metadata(account))
    profile["phone"] = phone
    data[PROFILE_METADATA_KEY] = profile
    account.daily_limits = data


def _set_profile_photo_metadata(account: TelegramAccount, *, photo: bytes, content_type: str) -> None:
    if len(photo) > MAX_STORED_PROFILE_PHOTO_BYTES:
        return
    data = dict(account.daily_limits or {})
    profile = dict(_profile_metadata(account))
    profile["photo_data_url"] = f"data:{content_type};base64,{base64.b64encode(photo).decode('ascii')}"
    data[PROFILE_METADATA_KEY] = profile
    account.daily_limits = data


def _set_profile_photo_data_url_metadata(account: TelegramAccount, photo_data_url: str | None) -> None:
    if not photo_data_url or not photo_data_url.startswith("data:image/"):
        return
    if len(photo_data_url.encode("utf-8")) > MAX_STORED_PROFILE_PHOTO_BYTES * 2:
        return
    data = dict(account.daily_limits or {})
    profile = dict(_profile_metadata(account))
    profile["photo_data_url"] = photo_data_url
    data[PROFILE_METADATA_KEY] = profile
    account.daily_limits = data


def _clear_profile_photo_metadata(account: TelegramAccount) -> None:
    data = dict(account.daily_limits or {})
    profile = dict(_profile_metadata(account))
    profile.pop("photo_data_url", None)
    data[PROFILE_METADATA_KEY] = profile
    account.daily_limits = data


def _profile_snapshot_is_fresh(account: TelegramAccount) -> bool:
    metadata = _profile_metadata(account)
    has_identity = bool(metadata.get("first_name") or metadata.get("last_name"))
    photo_data_url = metadata.get("photo_data_url")
    has_photo = isinstance(photo_data_url, str) and photo_data_url.startswith("data:image/")
    if has_identity and has_photo:
        return True
    checked_at = metadata.get("photo_checked_at")
    if not isinstance(checked_at, str):
        return False
    try:
        checked_dt = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if checked_dt.tzinfo is None:
        checked_dt = checked_dt.replace(tzinfo=UTC)
    return datetime.now(UTC) - checked_dt < PROFILE_PHOTO_CHECK_INTERVAL


def _mark_profile_photo_checked(account: TelegramAccount) -> None:
    data = dict(account.daily_limits or {})
    profile = dict(_profile_metadata(account))
    profile["photo_checked_at"] = datetime.now(UTC).isoformat()
    data[PROFILE_METADATA_KEY] = profile
    account.daily_limits = data


def _local_name_parts(account: TelegramAccount) -> tuple[str | None, str | None]:
    metadata = _profile_metadata(account)
    first_name = metadata.get("first_name")
    last_name = metadata.get("last_name")
    if first_name or last_name:
        return first_name, last_name

    parts = (account.display_name or "").split(maxsplit=1)
    if not parts:
        return None, None
    return parts[0], parts[1] if len(parts) > 1 else None


def _local_profile_response(account: TelegramAccount, *, can_edit_security: bool) -> TelegramAccountProfileResponse:
    first_name, last_name = _local_name_parts(account)
    metadata = _profile_metadata(account)
    bio = metadata.get("bio") or metadata.get("about")
    return TelegramAccountProfileResponse(
        account_id=account.id,
        username=account.username,
        first_name=first_name,
        last_name=last_name,
        display_name=account.display_name,
        bio=bio,
        about=bio,
        phone=metadata.get("phone") if isinstance(metadata.get("phone"), str) else None,
        photo_data_url=metadata.get("photo_data_url") if isinstance(metadata.get("photo_data_url"), str) else None,
        status=account.status,
        health_score=account.health_score,
        can_edit_security=can_edit_security,
    )


def _add_health_event(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    event_type: str,
    message: str,
    raw_error: dict | None = None,
    severity: str = "info",
) -> None:
    session.add(
        AccountHealthEvent(
            account_id=account.id,
            event_type=event_type,
            severity=severity,
            message=message,
            raw_error=raw_error,
        )
    )
