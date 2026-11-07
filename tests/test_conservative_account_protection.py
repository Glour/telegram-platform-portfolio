import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from telethon.sessions import StringSession

from app.modules.telegram_accounts import service
from app.modules.telegram_accounts.models import TelegramAccount, TelegramSession
from app.modules.telegram_accounts.risk import OUTBOUND_MODULES, assert_module_allowed, get_module_eligibility
from app.modules.telegram_accounts.schemas import TelegramTdataImportMeta
from app.modules.telegram_gateway import contacts
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.workers import live_comment_listener as listener


def account_with(restriction, status="active", **kwargs):
    return TelegramAccount(
        id="account",
        workspace_id="workspace",
        import_type="session",
        status=status,
        health_score=90,
        warmup_score=100,
        proxy_required=True,
        proxy_id="proxy",
        daily_limits={"restriction_status": {"status": restriction}, **kwargs},
    )


@pytest.mark.parametrize("restriction", [None, "unknown", "limited", "blocked", "frozen"])
@pytest.mark.parametrize("module", sorted(OUTBOUND_MODULES))
def test_outbound_requires_clear_restriction(restriction, module):
    account = account_with(restriction)
    assert get_module_eligibility(account, module).status == "blocked"
    with pytest.raises(HTTPException):
        assert_module_allowed(account, module)


def test_limited_eligibility_cannot_authorize_outbound():
    account = account_with("clear", last_flood_wait_at="2026-10-03T00:00:00+00:00")
    assert get_module_eligibility(account, "message_broadcast").status == "limited"
    with pytest.raises(HTTPException):
        assert_module_allowed(account, "message_broadcast")


def admitted_new_account(**metadata):
    account = account_with("clear", status="new")
    account.warmup_score = 0
    account.daily_limits = {
        "restriction_status": {
            "status": "clear",
            "evidence_source": "operator_admission",
            "checked_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            **metadata,
        }
    }
    return account


def test_operator_admitted_new_account_may_pilot_with_limited_eligibility():
    eligibility = assert_module_allowed(admitted_new_account(), "neurocommenting")
    assert eligibility.status == "limited"
    assert eligibility.reason == "not_fully_warmed"


@pytest.mark.parametrize(
    "metadata",
    [
        {"evidence_source": None},
        {"evidence_source": "get_me"},
        {"checked_at": None},
        {"checked_at": "not-a-date"},
        {"checked_at": "2026-10-03T12:00:00"},
        {"checked_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()},
        {"checked_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        {"status": "unknown"},
        {"status": "limited", "label": "SpamBot limited"},
        {"status": "blocked"},
    ],
)
def test_new_account_without_fresh_explicit_clear_admission_cannot_pilot(metadata):
    with pytest.raises(HTTPException):
        assert_module_allowed(admitted_new_account(**metadata), "neurocommenting")


def test_operator_admission_never_overrides_hard_blocks():
    account = admitted_new_account()
    account.daily_limits["last_action_limit_error_at"] = datetime.now(UTC).isoformat()
    with pytest.raises(HTTPException):
        assert_module_allowed(account, "neurocommenting")
    account = admitted_new_account()
    account.status = "spam_blocked"
    with pytest.raises(HTTPException):
        assert_module_allowed(account, "neurocommenting")


def test_legacy_unknown_new_account_cannot_pilot():
    with pytest.raises(HTTPException):
        assert_module_allowed(account_with(None, status="new"), "neurocommenting")


def test_import_cannot_queue_discovery_even_with_legacy_flag(monkeypatch):
    queued = Mock()
    monkeypatch.setattr(service.celery_app, "send_task", queued)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(telegram_auto_discovery_on_import=True))
    monkeypatch.setattr(service, "real_telegram_enabled", lambda: True)
    service._queue_imported_account_channel_discovery(account_with("unknown"))
    queued.assert_not_called()


def test_held_account_and_missing_proxy_block_even_with_clear_restriction():
    account = account_with("clear", status="held")
    with pytest.raises(HTTPException):
        assert_module_allowed(account, "message_broadcast")
    account.status = "active"
    account.proxy_id = None
    with pytest.raises(HTTPException):
        assert_module_allowed(account, "message_broadcast")


def test_clear_low_risk_account_allowed_and_unknown_read_only_not_blocked():
    assert assert_module_allowed(account_with("clear"), "message_broadcast").status == "allowed"
    assert assert_module_allowed(account_with("unknown"), "read_only_parsing").status == "allowed"


@pytest.mark.parametrize("restriction", [None, "unknown", "limited", "clear"])
@pytest.mark.parametrize("status", ["new", "failed", "held", "quarantined", "banned", "invalid"])
async def test_session_check_never_grants_admission(monkeypatch, restriction, status):
    account = account_with(restriction, status=status)
    account.health_score = 35
    session = SimpleNamespace(add=Mock(), commit=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(service, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(
        service,
        "check_real_account",
        AsyncMock(
            return_value=SimpleNamespace(
                username="checked",
                phone=None,
                tg_user_id=123,
            )
        ),
    )
    monkeypatch.setattr(service, "_cache_account_profile_snapshot", AsyncMock())
    await service.check_account(session, account, force_restriction_check=True)
    assert account.status == status
    assert account.health_score == 35
    assert account.restriction_status == restriction


@pytest.mark.parametrize("blob", ["", "existing-session"])
def test_login_uses_synchronous_guarded_factory(monkeypatch, blob):
    client = object()
    factory = Mock(return_value=client)
    safety = ModuleType("app.modules.telegram_safety.service")
    safety.create_login_client = factory
    monkeypatch.setitem(sys.modules, safety.__name__, safety)
    settings = SimpleNamespace(telegram_api_id=123, telegram_api_hash="hash", telegram_request_timeout_seconds=25)
    proxy = ("socks5", "127.0.0.1", 1080)
    assert service._phone_login_client(settings, blob, proxy, phone="+10000000000") is client
    factory.assert_called_once_with(
        blob,
        123,
        "hash",
        phone="+10000000000",
        proxy=proxy,
        request_retries=0,
        connection_retries=0,
        retry_delay=0,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        timeout=120,
    )


def test_login_without_proxy_fails_before_factory(monkeypatch):
    safety = ModuleType("app.modules.telegram_safety.service")
    safety.create_login_client = Mock()
    monkeypatch.setitem(sys.modules, safety.__name__, safety)
    with pytest.raises(HTTPException):
        service._phone_login_client(object(), "", None, phone="+10000000000")
    safety.create_login_client.assert_not_called()


@pytest.mark.parametrize("stage", ["отправка кода", "подтверждение кода", "подтверждение 2FA"])
def test_login_timeout_requires_stop_and_manual_review(stage):
    message = service._phone_login_timeout_message(stage)
    assert stage in message
    assert "Результат операции неизвестен" in message
    assert "Остановите вход" in message
    assert "ручную проверку" in message
    assert "Не запрашивайте новый код, не повторяйте вход и не меняйте прокси" in message
    assert "Начать заново" not in message
    assert "смените прокси" not in message


@pytest.mark.parametrize(
    "phone",
    [
        "10000000000",
        "+10000000000",
        " +10000000000 ",
        "+1 000 000 00 00",
        " +1 (000) 000-00-00 ",
    ],
)
async def test_real_login_factory_constructs_guarded_client_without_connecting(monkeypatch, phone):
    from app.modules.telegram_safety import service as safety

    connect = Mock(side_effect=AssertionError("factory accessed database"))
    monkeypatch.setattr(safety, "engine", SimpleNamespace(connect=connect))
    settings = SimpleNamespace(telegram_api_id=123, telegram_api_hash="hash", telegram_request_timeout_seconds=25)
    client = service._phone_login_client(
        settings,
        "",
        ("socks5", "127.0.0.1", 1080),
        phone=phone,
    )
    assert isinstance(client, safety.GuardedTelegramClient)
    assert client.safety_account_id is None
    assert client.safety_fingerprint == safety.fingerprint(b"login:10000000000")
    assert client._safety_connection is None
    connect.assert_not_called()
    await client.disconnect()


async def test_phone_get_me_import_stays_held(monkeypatch):
    session = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    client = SimpleNamespace(
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=123, phone="10000000000", username="checked")),
        session=SimpleNamespace(save=lambda: "session-blob"),
        complete_login=AsyncMock(),
    )
    monkeypatch.setattr(service, "_ensure_proxy_capacity_by_id", AsyncMock())
    monkeypatch.setattr(service, "_queue_imported_account_channel_discovery", Mock())
    monkeypatch.setattr(service, "encrypt_text", lambda value: value)
    account = await service._save_authorized_phone_account(
        session,
        "workspace",
        client,
        {"phone": "+10000000000", "proxy_id": "proxy"},
    )
    assert account.status == "held"
    assert account.restriction_status != "clear"


def offline_tdesk():
    from opentele2.td import MTP

    endpoint = SimpleNamespace(id=2, ip="192.0.2.51", port=443)
    endpoints = {MTP.DcOptions.Address.IPv4: {MTP.DcOptions.Protocol.Tcp: [endpoint]}}
    account = SimpleNamespace(
        MainDcId=2,
        authKey=SimpleNamespace(key=b"x" * 256),
        _local=SimpleNamespace(config=SimpleNamespace(endpoints=Mock(return_value=endpoints))),
    )
    return SimpleNamespace(
        mainAccount=account,
        isLoaded=lambda: True,
        ToTelethon=Mock(side_effect=AssertionError("raw converter called")),
    )


def test_offline_tdata_session_round_trip_without_raw_client(monkeypatch):
    from opentele2 import tl

    raw_client = Mock(side_effect=AssertionError("raw client created"))
    monkeypatch.setattr(tl, "TelegramClient", raw_client)
    stored = StringSession(service._offline_tdata_session_blob(offline_tdesk()))
    assert stored.auth_key.key == b"x" * 256
    assert (stored.dc_id, stored.server_address, stored.port) == (2, "192.0.2.51", 443)
    raw_client.assert_not_called()


@pytest.mark.parametrize("invalid", ["account", "key", "dc", "endpoints"])
def test_unsupported_offline_tdata_fails_closed(invalid):
    tdesk = offline_tdesk()
    if invalid == "account":
        tdesk.mainAccount = None
    elif invalid == "key":
        tdesk.mainAccount.authKey.key = b"short"
    elif invalid == "dc":
        tdesk.mainAccount.MainDcId = 3
    else:
        tdesk.mainAccount._local.config.endpoints.return_value = {}
    with pytest.raises(HTTPException, match="Telethon .session"):
        service._offline_tdata_session_blob(tdesk)
    tdesk.ToTelethon.assert_not_called()


async def test_tdata_import_persists_offline_and_never_queues_rpc(monkeypatch):
    from opentele2 import td, tl

    tdesk = offline_tdesk()
    raw_client = Mock(side_effect=AssertionError("raw client created"))
    monkeypatch.setattr(tl, "TelegramClient", raw_client)
    monkeypatch.setattr(td, "TDesktop", Mock(return_value=tdesk))
    monkeypatch.setattr(service, "real_telegram_enabled", lambda: True)
    monkeypatch.setattr(service.telegram_gateway, "ensure_telegram_configured", lambda: None)
    monkeypatch.setattr(service, "_apply_new_proxy_to_meta", AsyncMock())
    monkeypatch.setattr(service, "_ensure_proxy_capacity_by_id", AsyncMock())
    monkeypatch.setattr(service, "_find_duplicate_imported_account", AsyncMock(return_value=None))
    discovery = Mock(side_effect=AssertionError("RPC discovery queued"))
    monkeypatch.setattr(service, "_queue_imported_account_channel_discovery", discovery)
    monkeypatch.setattr(service, "encrypt_text", lambda value: value)
    session = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    account = await service._import_tdata_dir_account(
        session,
        "workspace",
        Path("unused"),
        TelegramTdataImportMeta(proxy_id="proxy"),
    )
    assert account.status == "held"
    assert account.restriction_status != "clear"
    saved = [call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], TelegramSession)]
    assert StringSession(saved[0].encrypted_session_blob).auth_key.key == b"x" * 256
    tdesk.ToTelethon.assert_not_called()
    raw_client.assert_not_called()
    discovery.assert_not_called()


async def test_listener_entrypoints_never_load_accounts_or_connect(monkeypatch):
    database = Mock(side_effect=AssertionError("listener accessed database"))
    monkeypatch.setattr(listener, "AsyncSessionLocal", database)
    monkeypatch.setattr(listener, "_load_live_bindings", AsyncMock(side_effect=AssertionError("loaded bindings")))
    with pytest.raises(RuntimeError, match="disabled"):
        await listener.main()
    with pytest.raises(RuntimeError, match="disabled"):
        await listener._run_account_listener(listener.LiveAccountBinding("account", []))
    database.assert_not_called()
    listener._load_live_bindings.assert_not_called()


@pytest.mark.parametrize("send", [False, True])
async def test_contacts_without_proxy_fail_before_client(monkeypatch, send):
    factory = Mock(side_effect=AssertionError("client created without proxy"))
    monkeypatch.setattr(contacts, "telegram_client_for_account", factory)
    account = SimpleNamespace(proxy_id=None, proxy_required=False)
    with pytest.raises(TelegramGatewayError) as exc:
        if send:
            await contacts.send_contact_photo(None, account, {}, {}, "unused", 1)
        else:
            await contacts.fetch_saved_contacts(None, account)
    assert exc.value.code == "PROXY_REQUIRED"
    factory.assert_not_called()


async def test_contact_send_never_requests_direct_connection(monkeypatch):
    calls = []

    @asynccontextmanager
    async def context(*args, **kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=999)))

    monkeypatch.setattr(contacts, "telegram_client_for_account", context)
    with pytest.raises(TelegramGatewayError, match="Сессия"):
        await contacts.send_contact_photo(None, SimpleNamespace(proxy_id="proxy"), {"sender_id": "100"}, {}, "", 1)
    assert calls == [{"use_proxy": True, "strict_requests": True}]
