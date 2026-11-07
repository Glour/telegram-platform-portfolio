"""Offline contracts plus an opt-in lock test against an empty disposable DB."""

import ast
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl.functions import InitConnectionRequest, InvokeWithLayerRequest, InvokeWithoutUpdatesRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.functions.messages import (
    GetBotCallbackAnswerRequest,
    GetHistoryRequest,
    SendMediaRequest,
    SendMessageRequest,
)
from telethon.tl.types import InputMediaEmpty

import app.db.import_models  # noqa: F401
from app.core.config import Settings
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway import service as gateway
from app.modules.telegram_gateway.contracts import TelegramGatewayError
from app.modules.telegram_safety import service as safety
from app.modules.telegram_safety.models import TelegramLoginSafety, TelegramOperation, TelegramSafetyState
from app.modules.workspaces.models import Workspace

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def test_channel_lookup_is_read_but_join_and_send_are_outbound():
    from telethon.tl.functions.channels import GetChannelsRequest, JoinChannelRequest

    assert safety.is_outbound(GetChannelsRequest(id=[])) is False
    assert safety.is_outbound(JoinChannelRequest(channel="channel")) is True
    assert safety.is_outbound(SendMessageRequest(peer="peer", message="text")) is True


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(safety, "get_settings", lambda: SimpleNamespace(telegram_operations_enabled=True))
    state = SimpleNamespace(
        manual_hold=False,
        restriction_status="clear",
        operator_approved_at=NOW,
        restriction_checked_at=NOW,
        fingerprint="aa" * 32,
        wait_until=None,
        wait_seconds=None,
        reason=None,
    )
    account = SimpleNamespace(restriction_status="clear", status="active")
    db = SimpleNamespace(
        get=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=Mock(return_value=None))),
    )

    async def get(model, key):
        return account if model is safety.TelegramAccount else state

    db.get.side_effect = get

    @asynccontextmanager
    async def begin():
        yield db

    monkeypatch.setattr(safety, "AsyncSessionLocal", SimpleNamespace(begin=begin))
    monkeypatch.setattr(safety, "_state", AsyncMock(return_value=state))
    monkeypatch.setattr(safety, "utcnow", lambda: NOW)
    monkeypatch.setattr(safety, "engine", SimpleNamespace(connect=AsyncMock(side_effect=AssertionError("No real DB"))))
    monkeypatch.setattr(safety.TelegramClient, "connect", AsyncMock(side_effect=AssertionError("No Telegram")))
    monkeypatch.setattr(safety.TelegramClient, "disconnect", AsyncMock())
    monkeypatch.setattr(safety.TelegramClient, "_call", AsyncMock(side_effect=AssertionError("No base RPC")))
    client = object.__new__(safety.GuardedTelegramClient)
    client.safety_account_id = "account"
    client.safety_fingerprint = "aa" * 32
    client._safety_connection = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=Mock(return_value=123))), commit=AsyncMock()
    )
    client._safety_backend_pid = 123
    client._safety_rpc_lock = asyncio.Lock()
    client._safety_stopped = False
    client.session = SimpleNamespace(process_entities=Mock())
    client._begin_operation = AsyncMock(return_value="operation")
    client._finish = AsyncMock()
    request = SimpleNamespace(resolve=AsyncMock())
    sender = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
    return SimpleNamespace(client=client, state=state, account=account, db=db, request=request, sender=sender)


@pytest.mark.parametrize("seconds", [30, 60, 300, 86400])
async def test_exact_wait_deadline_and_manual_hold(rig, seconds):
    await rig.client._trip(FloodWaitError(request=None, capture=seconds), "operation")
    assert rig.state.wait_seconds == seconds
    assert rig.state.received_at == NOW
    assert rig.state.wait_until == NOW + timedelta(seconds=seconds + 5)
    assert rig.state.manual_hold is True
    assert rig.state.reason == "flood_wait"
    assert rig.client._safety_stopped is True
    rig.sender.send.assert_not_called()


@pytest.mark.parametrize("previous", [None, NOW + timedelta(seconds=900), NOW.replace(tzinfo=None) + timedelta(days=2)])
async def test_deadline_never_shortens(rig, previous):
    rig.state.wait_until = previous
    await rig.client._trip(FloodWaitError(request=None, capture=60))
    assert rig.state.wait_until == max(safety.aware(previous) or NOW, NOW + timedelta(seconds=65))


@pytest.mark.parametrize("seconds", [None, -1, True, "60", 60.0])
async def test_unknown_wait_has_no_invented_release(rig, seconds):
    exc = type("FloodUnknownError", (Exception,), {})("unknown")
    exc.seconds = seconds
    await rig.client._trip(exc)
    assert rig.state.wait_seconds is None
    assert rig.state.wait_until is None
    assert rig.state.manual_hold is True
    assert rig.state.reason == "flood_unknown"


async def test_unknown_wait_preserves_existing_deadline(rig):
    rig.state.wait_until = NOW + timedelta(days=1)
    await rig.client._trip(type("FloodUnknownError", (Exception,), {})("unknown"))
    assert rig.state.wait_until == NOW + timedelta(days=1)


async def test_login_hold_is_persisted_separately(rig):
    rig.client.safety_account_id = None
    rig.db.get.return_value = None
    rig.db.get.side_effect = None
    await rig.client._trip(FloodWaitError(request=None, capture=300))
    login = rig.db.add.call_args.args[0]
    assert isinstance(login, TelegramLoginSafety)
    assert login.manual_hold is True
    assert login.wait_until == NOW + timedelta(seconds=305)
    safety._state.assert_not_awaited()


async def test_success_calls_sender_exactly_once_without_base_retries(rig):
    result = await rig.client._call(rig.sender, rig.request, ordered=True, flood_sleep_threshold=86400)
    assert result.id == 123
    rig.sender.send.assert_awaited_once_with(rig.request, ordered=True)
    rig.request.resolve.assert_awaited_once()
    rig.client._finish.assert_awaited_once_with("operation", "accepted", result=result)
    safety.TelegramClient._call.assert_not_awaited()


@pytest.mark.parametrize("error", [FloodWaitError(None, 30), RuntimeError("unknown"), TimeoutError("lost")])
async def test_first_error_blocks_rpc_even_when_caller_swallows_it(rig, error):
    rig.sender.send.side_effect = error
    try:
        await rig.client._call(rig.sender, rig.request)
    except Exception:
        pass
    assert rig.state.manual_hold is True
    safety.TelegramClient.disconnect.assert_awaited_once()
    later = SimpleNamespace(resolve=AsyncMock())
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, later)
    later.resolve.assert_not_awaited()
    assert rig.sender.send.await_count == 1
    rig.client._begin_operation.assert_awaited_once()


async def test_hold_commits_even_if_operation_result_write_fails(rig):
    rig.sender.send.side_effect = FloodWaitError(None, 60)
    rig.client._finish.side_effect = RuntimeError("operation store unavailable")
    with pytest.raises(RuntimeError, match="operation store unavailable"):
        await rig.client._call(rig.sender, rig.request)
    assert rig.state.manual_hold is True
    assert rig.state.wait_until == NOW + timedelta(seconds=65)
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    assert rig.sender.send.await_count == 1


async def test_hold_storage_failure_still_stops_local_transport(rig, monkeypatch):
    rig.sender.send.side_effect = RuntimeError("RPC failed")
    monkeypatch.setattr(safety, "_state", AsyncMock(side_effect=RuntimeError("DB down")))
    # Permit initial checks, but fail the independent hold transaction.
    original_check = rig.client._check
    rig.client._check = AsyncMock()
    with pytest.raises(RuntimeError, match="DB down"):
        await rig.client._call(rig.sender, rig.request)
    rig.client._check = original_check
    assert rig.client._safety_stopped is True
    safety.TelegramClient.disconnect.assert_awaited_once()
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    assert rig.sender.send.await_count == 1


async def test_cancelled_outbound_has_unknown_delivery_and_hold(rig):
    rig.sender.send.side_effect = asyncio.CancelledError()
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    assert rig.state.manual_hold is True
    assert rig.client._finish.call_args.args[1] == "unknown_delivery"


async def test_batch_is_rejected_before_resolution_or_sender(rig):
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, [rig.request, rig.request])
    rig.request.resolve.assert_not_awaited()
    rig.sender.send.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("manual_hold", True),
        ("restriction_status", "unknown"),
        ("restriction_status", "limited"),
        ("operator_approved_at", None),
        ("restriction_checked_at", NOW - timedelta(hours=25)),
        ("fingerprint", "bb" * 32),
    ],
)
async def test_persistent_barrier_blocks_before_any_rpc(rig, field, value):
    setattr(rig.state, field, value)
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    rig.request.resolve.assert_not_awaited()
    rig.sender.send.assert_not_called()


async def test_storage_loss_fails_closed_before_rpc(rig):
    rig.client._safety_connection.execute.side_effect = OSError("DB lost")
    with pytest.raises(TelegramGatewayError) as caught:
        await rig.client._call(rig.sender, rig.request)
    assert caught.value.code == "SAFETY_STORAGE_UNAVAILABLE"
    assert rig.client._safety_stopped is True
    rig.sender.send.assert_not_called()


async def test_backend_pid_change_loses_lock_and_disconnects_before_rpc(rig):
    rig.client._safety_backend_pid = 1
    rig.client._safety_connection.execute.return_value = SimpleNamespace(scalar_one=Mock(return_value=2))
    with pytest.raises(TelegramGatewayError) as caught:
        await rig.client._call(rig.sender, rig.request)
    assert caught.value.code == "SAFETY_LOCK_LOST"
    assert rig.client._safety_stopped is True
    assert [str(call.args[0]) for call in rig.client._safety_connection.execute.await_args_list] == [
        "SELECT 1",
        "SELECT pg_backend_pid()",
    ]
    safety.TelegramClient.disconnect.assert_awaited_once()
    rig.request.resolve.assert_not_awaited()
    rig.client._begin_operation.assert_not_awaited()
    rig.db.execute.assert_not_awaited()
    rig.sender.send.assert_not_called()
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    rig.sender.send.assert_not_called()


async def test_stable_backend_pid_allows_exactly_one_rpc(rig):
    rig.client._safety_backend_pid = 1
    rig.client._safety_connection.execute.return_value = SimpleNamespace(scalar_one=Mock(return_value=1))
    await rig.client._call(rig.sender, rig.request)
    rig.sender.send.assert_awaited_once_with(rig.request, ordered=False)
    safety.TelegramClient.disconnect.assert_not_awaited()


async def test_global_release_disabled_blocks_before_any_rpc(rig, monkeypatch):
    monkeypatch.setattr(safety, "get_settings", lambda: SimpleNamespace(telegram_operations_enabled=False))
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    rig.request.resolve.assert_not_awaited()
    rig.client._begin_operation.assert_not_awaited()
    rig.sender.send.assert_not_called()


def test_global_release_is_disabled_by_default():
    assert Settings.model_fields["telegram_operations_enabled"].default is False


async def test_mock_persistent_unresolved_operation_blocks_all_rpc(rig):
    rig.db.execute.return_value.scalar_one_or_none.return_value = "previous-operation"
    with pytest.raises(TelegramGatewayError) as caught:
        await rig.client._call(rig.sender, rig.request)
    assert caught.value.code == "SAFETY_UNKNOWN_DELIVERY"
    rig.sender.send.assert_not_called()


async def test_expired_wait_does_not_automatically_release_hold(rig):
    rig.state.manual_hold = True
    rig.state.wait_until = NOW - timedelta(seconds=1)
    with pytest.raises(TelegramGatewayError):
        await rig.client._check()


async def test_queued_rpc_rechecks_hold_inside_serial_lock(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_first(*args, **kwargs):
        entered.set()
        await release.wait()
        raise FloodWaitError(None, 30)

    rig.sender.send.side_effect = fail_first
    first = asyncio.create_task(rig.client._call(rig.sender, rig.request))
    await entered.wait()
    second = asyncio.create_task(rig.client._call(rig.sender, SimpleNamespace(resolve=AsyncMock())))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, TelegramGatewayError) for result in results)
    assert rig.sender.send.await_count == 1
    rig.client._begin_operation.assert_awaited_once()


async def test_recreated_client_checks_persistent_hold(rig):
    await rig.client._trip(FloodWaitError(None, 60))
    rig.client._safety_stopped = False
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    rig.sender.send.assert_not_called()


@pytest.mark.parametrize("method", ["_switch_dc", "_borrow_exported_sender"])
async def test_unguarded_dc_transports_are_disabled(rig, method):
    with pytest.raises(TelegramGatewayError):
        await getattr(rig.client, method)(2)
    rig.sender.send.assert_not_called()


def test_constructor_overrides_unsafe_retry_options(monkeypatch):
    init = Mock(return_value=None)

    def initialize(client, *args, **kwargs):
        client._sender = SimpleNamespace(send=AsyncMock(), _handlers={})
        init(*args, **kwargs)

    monkeypatch.setattr(safety.TelegramClient, "__init__", initialize)
    safety.GuardedTelegramClient(
        "session",
        123,
        "hash",
        account_id="account",
        lock_fingerprint="aa" * 32,
        request_retries=99,
        connection_retries=99,
        flood_sleep_threshold=86400,
        auto_reconnect=True,
        receive_updates=True,
        raise_last_call_error=False,
    )
    assert {
        key: init.call_args.kwargs[key]
        for key in (
            "request_retries",
            "connection_retries",
            "flood_sleep_threshold",
            "auto_reconnect",
            "receive_updates",
            "raise_last_call_error",
        )
    } == dict(
        request_retries=0,
        connection_retries=0,
        flood_sleep_threshold=0,
        auto_reconnect=False,
        receive_updates=False,
        raise_last_call_error=True,
    )


def test_no_raw_telegram_client_constructors_in_application():
    violations = []
    paths = [path for folder in ("src/app", "scripts", "deploy") for path in (ROOT / folder).rglob("*.py")]
    for path in sorted(paths):
        tree = ast.parse(path.read_text())
        aliases, modules = {"TelegramClient"}, {"telethon"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("telethon"):
                aliases.update(n.asname or n.name for n in node.names if n.name == "TelegramClient")
            if isinstance(node, ast.Import):
                modules.update(n.asname or n.name for n in node.names if n.name == "telethon")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            raw = isinstance(func, ast.Name) and func.id in aliases
            raw |= (
                isinstance(func, ast.Attribute)
                and func.attr == "TelegramClient"
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            )
            if raw:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "Raw TelegramClient constructors bypass guard: " + ", ".join(violations)


def test_only_guarded_transport_subclasses_telegram_client():
    subclasses = []
    for path in sorted((ROOT / "src/app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        aliases = {"TelegramClient"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("telethon"):
                aliases.update(n.asname or n.name for n in node.names if n.name == "TelegramClient")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                (isinstance(base, ast.Name) and base.id in aliases)
                or (isinstance(base, ast.Attribute) and base.attr == "TelegramClient")
                for base in node.bases
            ):
                subclasses.append((str(path.relative_to(ROOT)), node.name))
    assert subclasses == [("src/app/modules/telegram_safety/service.py", "GuardedTelegramClient")]


@pytest.mark.parametrize("factory", ["create_account_client", "create_login_client"])
def test_factories_require_proxy_before_constructing_client(monkeypatch, factory):
    constructor = Mock()
    monkeypatch.setattr(safety, "GuardedTelegramClient", constructor)
    monkeypatch.setattr(
        safety, "StringSession", Mock(return_value=SimpleNamespace(auth_key=SimpleNamespace(key=b"key")))
    )
    monkeypatch.setattr(safety, "fingerprint", lambda value: "aa" * 32)
    with pytest.raises(TelegramGatewayError) as caught:
        getattr(safety, factory)("session", 123, "hash", "account-or-phone", proxy=None)
    assert caught.value.code == "PROXY_REQUIRED"
    constructor.assert_not_called()


def test_account_factory_explicit_direct_mode_retains_guarded_client(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(safety, "GuardedTelegramClient", constructor)
    monkeypatch.setattr(
        safety, "StringSession", Mock(return_value=SimpleNamespace(auth_key=SimpleNamespace(key=b"key")))
    )
    monkeypatch.setattr(safety, "fingerprint", lambda value: "aa" * 32)
    result = safety.create_account_client("session", 123, "hash", "account", proxy=None, allow_direct=True)
    assert result is constructor.return_value
    assert constructor.call_args.kwargs["proxy"] is None
    assert constructor.call_args.kwargs["account_id"] == "account"
    assert constructor.call_args.kwargs["lock_fingerprint"] == "aa" * 32
    assert "allow_direct" not in constructor.call_args.kwargs


async def test_gateway_passes_explicit_direct_permission_only_for_optional_proxy(monkeypatch):
    monkeypatch.setattr(gateway, "ensure_telegram_configured", Mock())
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_api_id=123,
            telegram_api_hash="hash",
            telegram_request_timeout_seconds=10,
        ),
    )
    monkeypatch.setattr(gateway, "load_account_session", AsyncMock(return_value="session"))
    monkeypatch.setattr(gateway, "load_proxy", AsyncMock(return_value=None))
    constructor = Mock()
    monkeypatch.setattr(gateway, "create_account_client", constructor)
    account = SimpleNamespace(id="account", proxy_required=False, proxy_id=None)
    await gateway.build_telegram_client_for_account(Mock(), account)
    assert constructor.call_args.kwargs["allow_direct"] is True
    account.proxy_id = "missing-assigned-proxy"
    await gateway.build_telegram_client_for_account(Mock(), account)
    assert constructor.call_args.kwargs["allow_direct"] is False
    account.proxy_required = True
    with pytest.raises(TelegramGatewayError) as caught:
        await gateway.build_telegram_client_for_account(Mock(), account, use_proxy=False)
    assert caught.value.code == "PROXY_REQUIRED"


def test_login_fingerprint_normalizes_phone_digits(monkeypatch):
    fingerprint = Mock(side_effect=lambda value: value.decode())
    constructor = Mock()
    monkeypatch.setattr(safety, "StringSession", Mock(return_value=SimpleNamespace(auth_key=None)))
    monkeypatch.setattr(safety, "GuardedTelegramClient", constructor)
    monkeypatch.setattr(safety, "fingerprint", fingerprint)
    for phone in ("+7 999 123-45-67", "79991234567", " +7 (999) 123 45 67 "):
        safety.create_login_client("", 123, "hash", phone, proxy=("offline", 1))
        assert constructor.call_args.kwargs["lock_fingerprint"] == "login:79991234567"
    assert all(call.args == (b"login:79991234567",) for call in fingerprint.call_args_list)
    safety.create_login_client("", 123, "hash", "+7 999 123-45-68", proxy=("offline", 1))
    assert constructor.call_args.kwargs["lock_fingerprint"] == "login:79991234568"


def test_login_and_account_share_auth_key_lock(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(safety, "GuardedTelegramClient", constructor)
    monkeypatch.setattr(
        safety,
        "StringSession",
        lambda blob: SimpleNamespace(auth_key=SimpleNamespace(key=b"same-key")),
    )
    monkeypatch.setattr(safety, "fingerprint", lambda value: value.hex())
    safety.create_account_client("blob", 123, "hash", "account", proxy=("offline", 1))
    account_lock = constructor.call_args.kwargs["lock_fingerprint"]
    safety.create_login_client("blob", 123, "hash", "+1000", proxy=("offline", 1))
    assert constructor.call_args.kwargs["lock_fingerprint"] == account_lock
    assert constructor.call_args.kwargs["login_fingerprint"] != account_lock


async def test_profile_flood_does_not_continue_to_photo(rig, monkeypatch):
    class Client:
        async def __call__(self, request):
            await rig.client._call(rig.sender, request)

        get_entity = AsyncMock(return_value=SimpleNamespace(id=1))
        upload_file = AsyncMock()

    client = Client()

    @asynccontextmanager
    async def context(*args, **kwargs):
        yield client

    monkeypatch.setattr(gateway, "telegram_client_for_account", context)
    monkeypatch.setattr(
        gateway,
        "_profile_from_entity",
        AsyncMock(return_value=SimpleNamespace(first_name="Name", last_name="", about="Bio")),
    )
    monkeypatch.setattr(gateway, "_download_profile_photo_bytes", AsyncMock(return_value=b"photo"))
    monkeypatch.setattr(UpdateProfileRequest, "resolve", AsyncMock())
    rig.sender.send.side_effect = FloodWaitError(None, 300)
    with pytest.raises(TelegramGatewayError):
        await gateway.clone_account_profile(
            Mock(),
            rig.account,
            source_ref="owned-source",
            source_account=None,
            copy_name=True,
            copy_bio=True,
            copy_photo=True,
            copy_stories=False,
            pin_stories=False,
            delete_old_stories=False,
            story_limit=1,
        )
    client.upload_file.assert_not_awaited()
    assert rig.sender.send.await_count == 1
    assert rig.state.manual_hold is True


async def test_comment_write_denied_never_joins_or_resends(monkeypatch):
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=object()), send_message=AsyncMock(side_effect=ChatWriteForbiddenError(None))
    )

    @asynccontextmanager
    async def context(*args, **kwargs):
        yield client

    join = AsyncMock(side_effect=AssertionError("Hidden discussion join"))
    monkeypatch.setattr(gateway, "telegram_client_for_account", context)
    monkeypatch.setattr(gateway, "_resolve_comment_target_message_id", AsyncMock(return_value=(123, {})))
    monkeypatch.setattr(gateway, "_join_linked_discussion_for_comment", join)
    with pytest.raises(TelegramGatewayError):
        await gateway.send_comment_with_account(Mock(), Mock(), "owned-source", "123", "text")
    join.assert_not_awaited()
    client.send_message.assert_awaited_once()


async def test_join_captcha_requires_manual_verification_without_rpc():
    client = SimpleNamespace(
        iter_dialogs=Mock(side_effect=AssertionError("No automatic CAPTCHA scan")),
        iter_messages=Mock(side_effect=AssertionError("No automatic CAPTCHA scan")),
        send_message=AsyncMock(side_effect=AssertionError("No CAPTCHA reply")),
        get_messages=AsyncMock(side_effect=AssertionError("No CAPTCHA scan")),
    )
    result = await gateway._solve_join_captchas_after_join(client, NOW)
    assert result["manual_verification_required"] is True
    assert result["attempted"] == result["solved"] == result["scans"] == 0
    assert result["actions"] == []
    client.send_message.assert_not_called()


def test_safety_state_defaults_are_fail_closed():
    columns = TelegramSafetyState.__table__.columns
    assert columns.manual_hold.default.arg is True
    assert columns.restriction_status.default.arg == "unknown"
    assert columns.outbound_attempt_limit.default.arg == 1


def test_unknown_methods_are_outbound_and_reads_are_classified():
    assert safety.is_outbound(SimpleNamespace()) is True
    assert safety.is_outbound(GetHistoryRequest(None, 0, None, 0, 1, 0, 0, 0)) is False


def test_bot_callback_and_unknown_get_methods_are_outbound():
    assert safety.is_outbound(GetBotCallbackAnswerRequest(peer=None, msg_id=1, data=b"callback")) is True
    assert safety.is_outbound(type("GetUnknownMutationRequest", (), {})()) is True


def bootstrap_request():
    inner = GetConfigRequest()
    initialized = InitConnectionRequest(
        api_id=123,
        device_model="offline",
        system_version="test",
        app_version="test",
        system_lang_code="en",
        lang_pack="",
        lang_code="en",
        query=inner,
    )
    wrapped = InvokeWithLayerRequest(layer=1, query=InvokeWithoutUpdatesRequest(query=initialized))
    return wrapped, inner


def test_bootstrap_wrappers_unwrap_to_read_only_getconfig():
    wrapped, inner = bootstrap_request()
    assert safety.unwrap_request(wrapped) is inner
    assert safety.unwrap_request(inner) is inner
    assert safety.is_outbound(safety.unwrap_request(wrapped)) is False


async def test_bootstrap_wire_request_is_preserved_but_ledger_is_unwrapped(rig, monkeypatch):
    wrapped, inner = bootstrap_request()
    monkeypatch.setattr(InvokeWithLayerRequest, "resolve", AsyncMock())
    await rig.client._call(rig.sender, wrapped)
    rig.client._begin_operation.assert_awaited_once_with(inner)
    rig.sender.send.assert_awaited_once_with(wrapped, ordered=False)


async def test_no_updates_wraps_main_wire_request_not_ledger(rig):
    rig.client._no_updates = True
    await rig.client._call(rig.sender, rig.request, ordered=True)
    wire = rig.sender.send.await_args.args[0]
    assert isinstance(wire, InvokeWithoutUpdatesRequest)
    assert wire.query is rig.request
    assert rig.sender.send.await_args.kwargs == {"ordered": True}
    assert rig.sender.send.await_count == 1
    rig.client._begin_operation.assert_awaited_once_with(rig.request)


@pytest.mark.parametrize("disconnect_delay", [None, 90])
async def test_service_ping_stays_unwrapped_and_guarded(rig, disconnect_delay):
    from telethon.tl.functions import PingDelayDisconnectRequest, PingRequest

    request = (
        PingRequest(ping_id=42)
        if disconnect_delay is None
        else PingDelayDisconnectRequest(ping_id=42, disconnect_delay=disconnect_delay)
    )
    rig.client._no_updates = True
    await rig.client._call(rig.sender, request)
    rig.sender.send.assert_awaited_once_with(request, ordered=False)
    rig.client._begin_operation.assert_awaited_once_with(request)
    rig.client._finish.assert_awaited_once()


async def test_background_ping_waits_for_own_inflight_rpc(rig):
    from telethon.tl.functions import PingRequest

    started = asyncio.Event()
    release = asyncio.Event()
    inflight = False

    async def check():
        assert not inflight, "Own pending RPC must finish before a ping checks the ledger"

    async def begin(request):
        nonlocal inflight
        inflight = True
        return "operation"

    async def send(request, **kwargs):
        if request is rig.request:
            started.set()
            await release.wait()
        return SimpleNamespace(id=123)

    async def finish(*args, **kwargs):
        nonlocal inflight
        inflight = False

    rig.client._check = AsyncMock(side_effect=check)
    rig.client._begin_operation.side_effect = begin
    rig.client._finish.side_effect = finish
    rig.sender.send.side_effect = send
    first = asyncio.create_task(rig.client._call(rig.sender, rig.request))
    await started.wait()
    ping = asyncio.create_task(rig.client._call(rig.sender, PingRequest(ping_id=42)))
    await asyncio.sleep(0)
    assert not ping.done()
    release.set()
    await asyncio.gather(first, ping)
    assert rig.sender.send.await_count == 2
    assert rig.client._check.await_count == 4
    assert rig.client._finish.await_count == 2


@pytest.mark.parametrize("wrapper_type", [InvokeWithLayerRequest, InitConnectionRequest, InvokeWithoutUpdatesRequest])
async def test_no_updates_never_double_wraps_bootstrap(rig, monkeypatch, wrapper_type):
    rig.client._no_updates = True
    wrapped, inner = bootstrap_request()
    while not isinstance(wrapped, wrapper_type):
        wrapped = wrapped.query
    monkeypatch.setattr(wrapper_type, "resolve", AsyncMock())
    await rig.client._call(rig.sender, wrapped)
    rig.sender.send.assert_awaited_once_with(wrapped, ordered=False)
    rig.client._begin_operation.assert_awaited_once_with(inner)


async def test_constructor_sender_wrap_routes_bootstrap_and_main_rpc_once(monkeypatch):
    raw_send = AsyncMock(return_value=SimpleNamespace(id=123))

    def initialize(client, *args, **kwargs):
        client._sender = SimpleNamespace(send=raw_send, _handlers={})
        client.session = SimpleNamespace(process_entities=Mock())

    monkeypatch.setattr(safety.TelegramClient, "__init__", initialize)
    monkeypatch.setattr(InvokeWithLayerRequest, "resolve", AsyncMock())
    client = safety.GuardedTelegramClient("session", 123, "hash", account_id="account", lock_fingerprint="aa" * 32)
    client._check = AsyncMock()
    client._begin_operation = AsyncMock(return_value="operation")
    client._finish = AsyncMock()
    wrapped, inner = bootstrap_request()
    # Telethon bootstrap calls sender.send directly rather than client._call.
    await asyncio.wait_for(client._sender.send(wrapped), timeout=2)
    raw_send.assert_awaited_once_with(wrapped, ordered=False)
    client._begin_operation.assert_awaited_once_with(inner)
    client._check.assert_has_awaits([(), ()], any_order=False)
    request = outbound_request()
    await asyncio.wait_for(client._call(client._sender, request), timeout=2)
    assert raw_send.await_count == 2
    assert client._begin_operation.await_count == 2


@pytest.fixture
async def sql_store(monkeypatch, tmp_path):
    """Separate real sessions against a fresh local SQLite file, never app configuration."""
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'safety.sqlite'}")

    @event.listens_for(db_engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    connections = []
    monkeypatch.setattr(safety, "engine", db_engine)
    monkeypatch.setattr(safety, "AsyncSessionLocal", factory)
    monkeypatch.setattr(safety, "utcnow", lambda: NOW)
    monkeypatch.setattr(
        safety,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_operations_enabled=True, session_encryption_key="offline-key", secret_key="offline-key"
        ),
    )
    monkeypatch.setattr(safety.TelegramClient, "connect", AsyncMock(side_effect=AssertionError("No Telegram")))
    monkeypatch.setattr(safety.TelegramClient, "disconnect", AsyncMock())
    monkeypatch.setattr(safety.TelegramClient, "_call", AsyncMock(side_effect=AssertionError("No base RPC")))
    try:
        async with db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory.begin() as db:
            db.add(User(id="owner", email="offline@example.test", password_hash="not-a-password"))
            await db.flush()
            db.add(Workspace(id="workspace", owner_user_id="owner", name="Offline", slug="offline-safety"))
            await db.flush()
            db.add(
                TelegramAccount(
                    id="account",
                    workspace_id="workspace",
                    import_type="session",
                    status="active",
                    daily_limits={"restriction_status": {"status": "clear"}},
                )
            )
            await db.flush()
            db.add(
                TelegramSafetyState(
                    account_id="account",
                    fingerprint="aa" * 32,
                    manual_hold=False,
                    restriction_status="clear",
                    restriction_checked_at=NOW,
                    operator_approved_at=NOW,
                    outbound_attempt_limit=1,
                    rpc_attempt_limit=20,
                    first_seen_at=NOW,
                )
            )

        async def client(intent="intent", account_id="account"):
            instance = object.__new__(safety.GuardedTelegramClient)
            instance.safety_account_id = account_id
            instance.safety_fingerprint = "aa" * 32
            instance._safety_connection = SimpleNamespace(
                execute=AsyncMock(return_value=SimpleNamespace(scalar_one=Mock(return_value=123))), commit=AsyncMock()
            )
            instance._safety_backend_pid = 123
            instance._safety_stopped = False
            instance._safety_rpc_lock = asyncio.Lock()
            instance._safety_intent = intent
            instance._safety_business_intent = intent
            instance.session = SimpleNamespace(process_entities=Mock())
            return instance

        yield SimpleNamespace(factory=factory, client=client)
    finally:
        for connection in connections:
            await connection.close()
        await db_engine.dispose()


def outbound_request(random_id=1):
    return SimpleNamespace(
        random_id=random_id,
        resolve=AsyncMock(),
        to_dict=lambda: {"_": "SendMessageRequest", "message": "same approved body", "random_id": random_id},
    )


async def test_login_pending_survives_process_recreation(sql_store):
    first = await sql_store.client(intent="login-one", account_id=None)
    await first._begin_operation(outbound_request())
    replacement = await sql_store.client(intent="login-two", account_id=None)
    with pytest.raises(TelegramGatewayError) as caught:
        await replacement._check()
    assert caught.value.code == "SAFETY_LOGIN_HOLD"
    await replacement._finish(None, "accepted")
    with pytest.raises(TelegramGatewayError):
        await replacement._check()
    await first._finish(None, "accepted")
    await replacement._check()


async def test_login_error_hold_not_cleared_by_late_success(sql_store):
    client = await sql_store.client(intent="login-one", account_id=None)
    await client._begin_operation(outbound_request())
    await client._trip(FloodWaitError(None, 60))
    await client._finish(None, "accepted")
    async with sql_store.factory() as db:
        state = await db.get(TelegramLoginSafety, "aa" * 32)
        assert state.manual_hold
        assert state.reason == "flood_wait"
        assert safety.aware(state.wait_until) == NOW + timedelta(seconds=65)


async def test_completed_login_token_is_permanently_held(sql_store):
    client = await sql_store.client(intent="login-one", account_id=None)
    await client.complete_login()
    recreated = await sql_store.client(intent="login-two", account_id=None)
    with pytest.raises(TelegramGatewayError) as caught:
        await recreated._check()
    assert caught.value.code == "SAFETY_LOGIN_HOLD"


@pytest.mark.parametrize("request_type", [SendMessageRequest, SendMediaRequest])
async def test_business_intent_survives_new_tick_and_regenerated_text(sql_store, request_type):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.outbound_attempt_limit = 2
    first = await sql_store.client(intent="tick-one")
    first._safety_business_intent = "comment:source:post-42"
    op_id = await first._begin_operation(SendMessageRequest(peer="target", message="first text", random_id=1))
    await first._finish(op_id, "accepted", result=SimpleNamespace(id=123))
    recreated = await sql_store.client(intent="tick-two")
    recreated._safety_business_intent = first._safety_business_intent
    request_args = {"peer": "target", "message": "regenerated text", "random_id": 2}
    if request_type is SendMediaRequest:
        request_args["media"] = InputMediaEmpty()
    with pytest.raises(TelegramGatewayError) as caught:
        await recreated._begin_operation(request_type(**request_args))
    assert caught.value.code == "SAFETY_DUPLICATE_OPERATION"
    assert caught.value.raw["operation_id"] == op_id
    assert caught.value.raw["message_id"] == "123"


async def test_send_without_business_intent_fails_before_rpc(sql_store):
    client = await sql_store.client()
    client._safety_business_intent = None
    with pytest.raises(TelegramGatewayError) as caught:
        await client._begin_operation(SendMessageRequest(peer="target", message="body", random_id=1))
    assert caught.value.code == "SAFETY_DELIVERY_INTENT_REQUIRED"


async def test_unmatched_protocol_error_persists_hold_after_restart(sql_store, monkeypatch):
    monkeypatch.setattr(safety.TelegramClient, "disconnect", AsyncMock())
    client = await sql_store.client()
    client._sender = SimpleNamespace(_pop_states=Mock(return_value=[]))
    await client._reject_protocol_retry(SimpleNamespace(obj=SimpleNamespace(bad_msg_id=1)))
    await client._safety_protocol_failure_task
    replacement = await sql_store.client(intent="after-restart")
    with pytest.raises(TelegramGatewayError) as caught:
        await replacement._check()
    assert caught.value.code == "SAFETY_MANUAL_HOLD"


@pytest.mark.parametrize("login", [False, True])
async def test_sql_independent_hold_survives_caller_rollback(sql_store, login):
    client = await sql_store.client(account_id=None if login else "account")
    async with sql_store.factory() as caller:
        await caller.begin()
        # This caller-owned unit of work must not control the safety transaction.
        caller.add(
            TelegramOperation(
                id="caller-only",
                account_id="account",
                method="caller.marker",
                outbound=False,
                outcome="accepted",
                created_at=NOW,
            )
        )
        await client._trip(FloodWaitError(None, 300), "operation")
        await caller.rollback()
    async with sql_store.factory() as db:
        state = await db.get(TelegramLoginSafety if login else TelegramSafetyState, "aa" * 32 if login else "account")
        assert state.manual_hold is True
        assert state.wait_seconds == 300
        assert safety.aware(state.wait_until) == NOW + timedelta(seconds=305)
        assert await db.get(TelegramOperation, "caller-only") is None
    replacement = await sql_store.client(account_id=None if login else "account")
    with pytest.raises(TelegramGatewayError):
        await replacement._check()


@pytest.mark.parametrize("outcome", ["pending", "unknown_delivery"])
@pytest.mark.parametrize("outbound", [False, True])
async def test_sql_recreated_client_blocks_unresolved_reads_and_writes(sql_store, outcome, outbound):
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                account_id="account",
                method="GetHistoryRequest" if not outbound else "SendMessageRequest",
                outbound=outbound,
                outcome=outcome,
                created_at=NOW,
            )
        )
    # Local stop is false and admission is clear: only the persisted ledger blocks.
    replacement = await sql_store.client(intent="new-intent")
    sender = SimpleNamespace(send=AsyncMock())
    with pytest.raises(TelegramGatewayError) as caught:
        await replacement._call(sender, outbound_request())
    assert caught.value.code == "SAFETY_UNKNOWN_DELIVERY"
    sender.send.assert_not_called()


@pytest.mark.parametrize("wait_active", [False, True])
@pytest.mark.parametrize("restriction_location", ["state", "account"])
async def test_sql_admission_cannot_clear_known_limited_or_active_wait(sql_store, wait_active, restriction_location):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.wait_until = NOW + timedelta(seconds=60) if wait_active else NOW - timedelta(seconds=1)
        if restriction_location == "state":
            state.restriction_status = "limited"
        else:
            account = await db.get(TelegramAccount, "account")
            account.daily_limits = {"restriction_status": {"status": "limited"}}
    for action in (
        lambda: safety.approve_account("account", "operator", NOW, "fresh evidence"),
        lambda: safety.resume_account("account", "operator"),
    ):
        with pytest.raises(TelegramGatewayError) as caught:
            await action()
        assert caught.value.code == ("SAFETY_WAIT_ACTIVE" if wait_active else "SAFETY_RESTRICTED")
    async with sql_store.factory() as db:
        state = await db.get(TelegramSafetyState, "account")
        assert state.manual_hold is True
        assert state.actor_id is None
        assert list((await db.scalars(select(TelegramOperation))).all()) == []


async def test_sql_admission_cannot_shorten_clear_account_wait(sql_store):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.wait_until = NOW + timedelta(days=1)
    with pytest.raises(TelegramGatewayError) as caught:
        await safety.approve_account("account", "operator", NOW, "fresh evidence")
    assert caught.value.code == "SAFETY_WAIT_ACTIVE"


async def test_sql_admission_change_after_check_blocks_ledger_insert(sql_store):
    client = await sql_store.client()
    await client._check()
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
    with pytest.raises(TelegramGatewayError) as caught:
        await client._begin_operation(outbound_request())
    assert caught.value.code == "SAFETY_MANUAL_HOLD"
    async with sql_store.factory() as db:
        assert list((await db.scalars(select(TelegramOperation))).all()) == []


async def test_sql_operator_admission_and_resume_audit_are_not_rpc_attempts(sql_store):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.rpc_attempt_limit = 1
    await safety.approve_account("account", "operator", NOW, "verified clear evidence")
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "flood_wait"
        state.wait_until = NOW - timedelta(seconds=1)
    await safety.resume_account("account", "operator")
    client = await sql_store.client()
    operation_id = await client._begin_operation(GetHistoryRequest(None, 0, None, 0, 1, 0, 0, 0))
    async with sql_store.factory() as db:
        operations = list((await db.scalars(select(TelegramOperation))).all())
        audits = [op for op in operations if op.method.startswith("operator.")]
        assert {op.method for op in audits} == {"operator.admit", "operator.resume"}
        assert all(op.outbound is False and op.target == "operator" for op in audits)
        assert len(operations) == 3
        assert await db.get(TelegramOperation, operation_id) is not None
        state = await db.get(TelegramSafetyState, "account")
        assert state.actor_id == "operator"
        assert state.evidence == "verified clear evidence"


@pytest.mark.parametrize("resolution,message_id", [("accepted", "321"), ("not_delivered", None)])
async def test_sql_reconciliation_audit_does_not_release_hold_or_consume_rpc_budget(sql_store, resolution, message_id):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "transport_error"
        state.rpc_attempt_limit = 2
        db.add(
            TelegramOperation(
                id="unknown-op",
                account_id="account",
                method="SendMessageRequest",
                outbound=True,
                outcome="unknown_delivery",
                created_at=NOW,
            )
        )
    with pytest.raises(TelegramGatewayError) as caught:
        await safety.approve_account("account", "operator", NOW, "baseline evidence")
    assert caught.value.code == "SAFETY_UNKNOWN_DELIVERY"
    await safety.resolve_operation(
        "account", "unknown-op", "operator", resolution, "delivery evidence", message_id=message_id
    )
    async with sql_store.factory() as db:
        state = await db.get(TelegramSafetyState, "account")
        assert state.manual_hold is True
        assert state.actor_id == "operator"
        operation = await db.get(TelegramOperation, "unknown-op")
        assert operation.outcome == resolution
        assert operation.message_id == message_id
        audit = (
            await db.scalars(select(TelegramOperation).where(TelegramOperation.method == "operator.resolve"))
        ).one()
        assert audit.outbound is False and audit.target == "operator"
    await safety.approve_account("account", "operator", NOW, "new clear baseline evidence")
    client = await sql_store.client()
    await client._check()
    assert await client._begin_operation(GetHistoryRequest(None, 0, None, 0, 1, 0, 0, 0))


@pytest.mark.parametrize("outcome", ["accepted", "failed", "unknown_delivery", "pending"])
@pytest.mark.parametrize(
    "age,blocked",
    [
        (timedelta(hours=23, minutes=59), True),
        (timedelta(hours=24), True),
        (timedelta(hours=24, seconds=1), False),
    ],
)
async def test_sql_rolling_outbound_budget_counts_all_attempt_outcomes(sql_store, outcome, age, blocked):
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                account_id="account", method="SendMessageRequest", outbound=True, outcome=outcome, created_at=NOW - age
            )
        )
    client = await sql_store.client(intent="fresh-intent")
    # Exercise budget SQL directly; unresolved ledger admission has its own tests.
    if blocked:
        with pytest.raises(TelegramGatewayError) as caught:
            await client._begin_operation(outbound_request())
        assert caught.value.code == "SAFETY_ATTEMPT_BUDGET"
    else:
        assert await client._begin_operation(outbound_request())


async def test_sql_rpc_budget_counts_reads_excludes_old_and_operator_rows(sql_store):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.rpc_attempt_limit = 2
        db.add_all(
            [
                TelegramOperation(
                    account_id="account", method="operator.resolve", outbound=False, outcome="accepted", created_at=NOW
                ),
                TelegramOperation(
                    account_id="account",
                    method="GetHistoryRequest",
                    outbound=False,
                    outcome="failed",
                    created_at=NOW - timedelta(hours=24, seconds=1),
                ),
                TelegramOperation(
                    account_id="account",
                    method="GetHistoryRequest",
                    outbound=False,
                    outcome="failed",
                    created_at=NOW - timedelta(hours=1),
                ),
            ]
        )
    client = await sql_store.client()
    assert await client._begin_operation(GetHistoryRequest(None, 0, None, 0, 1, 0, 0, 0))
    with pytest.raises(TelegramGatewayError) as caught:
        await client._begin_operation(GetHistoryRequest(None, 0, None, 0, 1, 0, 0, 0))
    assert caught.value.code == "SAFETY_RPC_BUDGET"


async def test_sql_durable_idempotency_is_per_intent_not_permanent_body_ban(sql_store):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.outbound_attempt_limit = 5
    first = await sql_store.client(intent="celery-task-id")
    operation_id = await first._begin_operation(outbound_request(random_id=100))
    await first._finish(operation_id, "accepted", result=SimpleNamespace(id=321))
    retry = await sql_store.client(intent="celery-task-id")
    with pytest.raises(TelegramGatewayError) as caught:
        await retry._begin_operation(outbound_request(random_id=200))
    assert caught.value.code == "SAFETY_DUPLICATE_OPERATION"
    new_intent = await sql_store.client(intent="new-api-intent")
    second_id = await new_intent._begin_operation(outbound_request(random_id=300))
    async with sql_store.factory() as db:
        first_op = await db.get(TelegramOperation, operation_id)
        second_op = await db.get(TelegramOperation, second_id)
        assert first_op.message_id == "321"
        assert first_op.idempotency_key != second_op.idempotency_key
        assert first_op.random_id == 100 and second_op.random_id == 300


async def test_sql_accepted_send_without_message_id_holds_unknown_delivery(sql_store, monkeypatch):
    monkeypatch.setattr(SendMessageRequest, "resolve", AsyncMock())
    client = await sql_store.client()
    result = SimpleNamespace(updates=[])
    sender = SimpleNamespace(send=AsyncMock(return_value=result))
    request = SendMessageRequest(peer=None, message="approved offline text", random_id=123)
    assert await client._call(sender, request) is result
    async with sql_store.factory() as db:
        operation = (await db.scalars(select(TelegramOperation))).one()
        assert operation.method == "SendMessageRequest"
        assert operation.outcome == "unknown_delivery"
        assert operation.message_id is None
        state = await db.get(TelegramSafetyState, "account")
        assert state.manual_hold is True
        assert state.reason == "delivery_not_verified"
        assert state.operation_id == operation.id
    assert client._safety_stopped is True
    replacement = await sql_store.client(intent="new-intent")
    with pytest.raises(TelegramGatewayError):
        await replacement._call(sender, request)
    sender.send.assert_awaited_once()


@pytest.mark.parametrize("task_id", [None, "stable-celery-task"])
def test_constructor_uses_celery_task_id_or_new_api_intent(monkeypatch, task_id):
    monkeypatch.setattr(
        safety.TelegramClient,
        "__init__",
        lambda client, *args, **kwargs: setattr(client, "_sender", SimpleNamespace(send=AsyncMock(), _handlers={})),
    )
    monkeypatch.setattr(
        safety,
        "current_task",
        SimpleNamespace(
            _get_current_object=lambda: SimpleNamespace(request=SimpleNamespace(id=task_id)) if task_id else None
        ),
    )
    first = safety.GuardedTelegramClient("session", 123, "hash", account_id="account", lock_fingerprint="aa" * 32)
    second = safety.GuardedTelegramClient("session", 123, "hash", account_id="account", lock_fingerprint="aa" * 32)
    if task_id:
        assert first._safety_intent == second._safety_intent == task_id
    else:
        assert first._safety_intent != second._safety_intent


async def test_protocol_bad_message_never_requeues(rig):
    future = asyncio.get_running_loop().create_future()
    pop = Mock(return_value=[SimpleNamespace(future=future)])
    rig.client._sender = SimpleNamespace(_pop_states=pop, _send_queue=Mock())
    await rig.client._reject_protocol_retry(SimpleNamespace(obj=SimpleNamespace(bad_msg_id=123)))
    await rig.client._safety_protocol_failure_task
    assert rig.client._safety_stopped
    pop.assert_called_once_with(123)
    rig.client._sender._send_queue.extend.assert_not_called()
    with pytest.raises(ConnectionError, match="retry rejected"):
        await future


async def test_wire_timeout_persists_hold_without_repeat(rig):
    rig.client._safety_request_timeout = 0.01
    rig.sender.send.side_effect = lambda *a, **k: None

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    rig.sender.send.side_effect = blocked
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, rig.request)
    assert rig.state.manual_hold
    assert rig.state.reason == "transport_error"
    rig.sender.send.assert_awaited_once()


async def test_postgres_session_lock_on_explicit_empty_test_database(monkeypatch):
    """Parent creates a disposable DB; this test never creates or migrates tables."""
    url = os.environ.get("TELEGRAM_SAFETY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Opt-in: supply an empty safety_transport_test_* PostgreSQL database")
    parsed = make_url(url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or not (parsed.database or "").startswith("safety_transport_test_")
        or os.environ.get("TELEGRAM_SAFETY_TEST_DB_DISPOSABLE") != "yes"
    ):
        pytest.fail("Refusing DB: dedicated safety_transport_test_* database and disposable=yes required")
    engine = create_async_engine(parsed, poolclass=NullPool)
    clients = []
    connected = AsyncMock()
    disconnected = AsyncMock()
    monkeypatch.setattr(safety, "engine", engine)
    monkeypatch.setattr(safety.TelegramClient, "connect", connected)
    monkeypatch.setattr(safety.TelegramClient, "disconnect", disconnected)
    fingerprint = uuid4().hex * 2

    def client():
        instance = object.__new__(safety.GuardedTelegramClient)
        instance.safety_account_id = "offline-lock-test"
        instance.safety_fingerprint = fingerprint
        instance._safety_connection = None
        instance._safety_stopped = False
        instance._check = AsyncMock()
        instance._trip = AsyncMock()
        clients.append(instance)
        return instance

    try:
        async with engine.connect() as connection:
            tables = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
            )
            assert tables == 0, "Refusing nonempty database; no application DB may be used"
        first, competitor, replacement = client(), client(), client()
        await first.connect()
        assert (
            first._safety_backend_pid
            == (await first._safety_connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        # Guard connect commits its transaction: the session lock must survive commit.
        with pytest.raises(TelegramGatewayError) as caught:
            await competitor.connect()
        assert caught.value.code == "SAFETY_ACCOUNT_BUSY"
        assert competitor._safety_connection is None
        assert connected.await_count == 1
        await first.disconnect()
        await replacement.connect()
        assert connected.await_count == 2
        # Server-side connection death models abrupt owner loss, not normal disconnect.
        lost_pid = replacement._safety_backend_pid
        async with engine.connect() as killer:
            assert await killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": lost_pid})
            await killer.commit()
        monkeypatch.setattr(safety, "get_settings", lambda: SimpleNamespace(telegram_operations_enabled=True))
        replacement._check = safety.GuardedTelegramClient._check.__get__(replacement)
        with pytest.raises(TelegramGatewayError) as lost:
            await replacement._check()
        assert lost.value.code in {"SAFETY_STORAGE_UNAVAILABLE", "SAFETY_LOCK_LOST"}
        assert replacement._safety_stopped
        after_crash = client()
        await after_crash.connect()
        assert connected.await_count == 3
    finally:
        for instance in clients:
            if instance._safety_connection is not None:
                await instance.disconnect()
        await engine.dispose()


async def test_postgres_concurrent_budget_and_first_fingerprint_binding(monkeypatch):
    """Real FOR UPDATE races; only an explicitly approved empty disposable DB."""
    url = os.environ.get("TELEGRAM_SAFETY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Opt-in: supply an empty safety_transport_test_* PostgreSQL database")
    parsed = make_url(url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or not (parsed.database or "").startswith("safety_transport_test_")
        or os.environ.get("TELEGRAM_SAFETY_TEST_DB_DISPOSABLE") != "yes"
    ):
        pytest.fail("Refusing DB: dedicated safety_transport_test_* database and disposable=yes required")
    engine = create_async_engine(parsed, poolclass=NullPool, connect_args={"command_timeout": 10})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    schema_created = False
    monkeypatch.setattr(safety, "engine", engine)
    monkeypatch.setattr(safety, "AsyncSessionLocal", factory)
    monkeypatch.setattr(safety, "utcnow", lambda: NOW)
    monkeypatch.setattr(
        safety,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_operations_enabled=True, session_encryption_key="offline-key", secret_key="offline-key"
        ),
    )
    monkeypatch.setattr(safety.TelegramClient, "connect", AsyncMock(side_effect=AssertionError("No Telegram")))
    monkeypatch.setattr(safety.TelegramClient, "disconnect", AsyncMock())
    monkeypatch.setattr(safety.TelegramClient, "_call", AsyncMock(side_effect=AssertionError("No base RPC")))

    def client(intent, fingerprint="aa" * 32):
        instance = object.__new__(safety.GuardedTelegramClient)
        instance.safety_account_id = "account"
        instance.safety_fingerprint = fingerprint
        instance._safety_intent = intent
        instance._safety_stopped = False
        instance._safety_rpc_lock = asyncio.Lock()
        # Transport liveness is a mock; safety state and locking use real PostgreSQL sessions.
        instance._safety_connection = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one=Mock(return_value=123))), commit=AsyncMock()
        )
        instance._safety_backend_pid = 123
        return instance

    async def simultaneous(actions):
        start = asyncio.Event()

        async def run(action):
            await start.wait()
            return await action()

        tasks = [asyncio.create_task(run(action)) for action in actions]
        start.set()
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)

    try:
        async with engine.begin() as connection:
            tables = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
            )
            assert tables == 0, "Refusing nonempty database; no application DB may be used"
            await connection.run_sync(Base.metadata.create_all)
        schema_created = True
        async with factory.begin() as db:
            db.add(User(id="owner", email="offline@example.test", password_hash="not-a-password"))
            await db.flush()
            db.add(Workspace(id="workspace", owner_user_id="owner", name="Offline", slug="offline-safety"))
            await db.flush()
            db.add(
                TelegramAccount(
                    id="account",
                    workspace_id="workspace",
                    import_type="session",
                    status="active",
                    daily_limits={"restriction_status": {"status": "clear"}},
                )
            )
            await db.flush()
            db.add(
                TelegramSafetyState(
                    account_id="account",
                    fingerprint="aa" * 32,
                    manual_hold=False,
                    restriction_status="clear",
                    restriction_checked_at=NOW,
                    operator_approved_at=NOW,
                    outbound_attempt_limit=1,
                    rpc_attempt_limit=20,
                    first_seen_at=NOW,
                )
            )
        first, second = client("intent-one"), client("intent-two")
        results = await simultaneous(
            [
                lambda: first._begin_operation(outbound_request(1)),
                lambda: second._begin_operation(outbound_request(2)),
            ]
        )
        success = [value for value in results if isinstance(value, str)]
        failures = [value for value in results if isinstance(value, BaseException)]
        assert len(success) == len(failures) == 1, [type(value).__name__ for value in results]
        assert isinstance(failures[0], TelegramGatewayError)
        assert failures[0].code == "SAFETY_ATTEMPT_BUDGET"
        async with factory.begin() as db:
            operations = list((await db.scalars(select(TelegramOperation))).all())
            assert len(operations) == 1
            assert operations[0].id == success[0]
            assert operations[0].outbound is True and operations[0].outcome == "pending"
            await db.execute(delete(TelegramOperation))
            state = await db.get(TelegramSafetyState, "account")
            state.fingerprint = None
        first, second = client("auth-one", "aa" * 32), client("auth-two", "bb" * 32)
        results = await simultaneous([first._check, second._check])
        winners = [index for index, value in enumerate(results) if value is None]
        assert len(winners) == 1, [type(value).__name__ for value in results]
        loser = results[1 - winners[0]]
        assert isinstance(loser, TelegramGatewayError)
        assert loser.code == "SAFETY_SESSION_CHANGED"
        async with factory() as db:
            state = await db.get(TelegramSafetyState, "account")
            assert state.fingerprint == (first, second)[winners[0]].safety_fingerprint
        # Binding remains stable on subsequent checks, not just during the race.
        await (first, second)[winners[0]]._check()
        with pytest.raises(TelegramGatewayError) as caught:
            await (first, second)[1 - winners[0]]._check()
        assert caught.value.code == "SAFETY_SESSION_CHANGED"
    finally:
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.drop_all)
        finally:
            await engine.dispose()


async def test_owner_attestation_preserves_unknown_and_authorizes_only_selected_account(sql_store):
    async with sql_store.factory.begin() as db:
        account = await db.get(TelegramAccount, "account")
        account.daily_limits = {"restriction_status": {"status": "unknown"}}
        state = await db.get(TelegramSafetyState, "account")
        state.restriction_status = "unknown"
        state.restriction_checked_at = None
        state.operator_approved_at = None
        state.manual_hold = True
        state.reason = "admission_required"
    result = await safety.approve_owner_attestation("account", "owner", "Owner confirms warmed and trusted")
    assert result["restriction_status"] == "unknown"
    assert result["restriction_checked_at"] is None
    assert result["manual_hold"] is False
    client = await sql_store.client()
    await client._check()
    async with sql_store.factory() as db:
        account = await db.get(TelegramAccount, "account")
        assert account.restriction_status == "unknown"
        assert safety.owner_attested(account, "neurocommenting")
        assert not safety.owner_attested(account, "message_broadcast")
    await client._trip(FloodWaitError(None, 60))
    client = await sql_store.client()
    with pytest.raises(TelegramGatewayError) as caught:
        await client._check()
    assert caught.value.code == "SAFETY_WAIT_ACTIVE"


@pytest.mark.parametrize("failure", ["limited", "wait", "unknown_delivery", "nonowner", "operational_hold"])
async def test_owner_attestation_cannot_clear_runtime_stops(sql_store, failure):
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.reason = "admission_required"
        if failure == "limited":
            state.restriction_status = "limited"
        if failure == "wait":
            state.wait_until = NOW + timedelta(hours=1)
        if failure == "unknown_delivery":
            db.add(TelegramOperation(account_id="account", method="SendMessageRequest", outcome="unknown_delivery"))
        if failure == "operational_hold":
            state.reason = "transport_error"
    with pytest.raises(TelegramGatewayError):
        await safety.approve_owner_attestation(
            "account", "not-owner" if failure == "nonowner" else "owner", "Owner claim"
        )


async def test_owner_attestation_enforces_two_join_attempts_hour_and_five_comments(sql_store, monkeypatch):
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import InputChannel, InputPeerUser

    await safety.approve_owner_attestation("account", "owner", "Explicit cohort admission")
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.outbound_attempt_limit = 50
        state.rpc_attempt_limit = 100
    client = await sql_store.client()
    op = await client._begin_operation(JoinChannelRequest(InputChannel(1, 2)))
    await client._finish(op, "accepted")
    with pytest.raises(TelegramGatewayError) as error:
        await client._begin_operation(JoinChannelRequest(InputChannel(2, 3)))
    assert error.value.code == "SAFETY_JOIN_RATE"
    monkeypatch.setattr(safety, "utcnow", lambda: NOW + timedelta(seconds=1801))
    op = await client._begin_operation(JoinChannelRequest(InputChannel(2, 3)))
    await client._finish(op, "accepted")
    monkeypatch.setattr(safety, "utcnow", lambda: NOW + timedelta(seconds=3599))
    with pytest.raises(TelegramGatewayError) as error:
        await client._begin_operation(JoinChannelRequest(InputChannel(3, 4)))
    assert error.value.code == "SAFETY_JOIN_RATE"
    for i in range(5):
        client._safety_business_intent = f"comment-{i}"
        op = await client._begin_operation(SendMessageRequest(InputPeerUser(1, 2), f"approved {i}", random_id=i + 1))
        await client._finish(op, "accepted", result=SimpleNamespace(id=i + 1))
    client._safety_business_intent = "sixth"
    with pytest.raises(TelegramGatewayError) as error:
        await client._begin_operation(SendMessageRequest(InputPeerUser(1, 2), "sixth", random_id=6))
    assert error.value.code == "SAFETY_COMMENT_BUDGET"


async def test_wrapped_getconfig_timeout_is_failed_read_not_unknown_delivery(rig):
    wrapped, inner = bootstrap_request()
    rig.sender.send = AsyncMock(side_effect=ConnectionError("Read bootstrap transport stopped"))
    wrapped.resolve = AsyncMock()
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, wrapped)
    assert rig.client._finish.call_args.args[1] == "failed"


async def test_bootstrap_salt_correction_is_bounded_and_never_requeues_write(sql_store):
    from telethon.tl.types import InputPeerUser

    client = await sql_store.client()
    wrapped, inner = bootstrap_request()
    await client._begin_operation(inner)
    future = asyncio.get_running_loop().create_future()
    pending = SimpleNamespace(request=wrapped, future=future)
    client._sender = SimpleNamespace(
        _pop_states=Mock(return_value=[pending]), _state=SimpleNamespace(salt=0), _send_queue=Mock()
    )
    msg = SimpleNamespace(obj=safety.BadServerSalt(1, 1, 48, 42))
    await client._handle_bootstrap_notification(msg)
    assert client._sender._state.salt == 42
    client._sender._send_queue.extend.assert_called_once_with([pending])
    assert not client._safety_stopped
    await client._handle_bootstrap_notification(msg)
    await client._safety_protocol_failure_task
    assert client._safety_stopped
    assert client._sender._send_queue.extend.call_count == 1
    with pytest.raises(ConnectionError):
        await future
    send = SendMessageRequest(InputPeerUser(1, 2), "approved", random_id=1)
    assert safety._bootstrap_correction_kind(msg, [SimpleNamespace(request=send)]) is None


@pytest.mark.parametrize("code", [32, 33, 64])
def test_bootstrap_sequence_correction_is_not_allowed(code):
    wrapped, _ = bootstrap_request()
    msg = SimpleNamespace(obj=safety.BadMsgNotification(1, 1, code))
    assert safety._bootstrap_correction_kind(msg, [SimpleNamespace(request=wrapped)]) is None


@pytest.mark.parametrize("write_attempted", [False, True])
async def test_manual_bootstrap_repair_resume_cannot_resume_write_history(sql_store, write_attempted):
    await safety.approve_owner_attestation("account", "owner", "Owner confirms")
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "transport_error"
        db.add(TelegramOperation(account_id="account", method="GetConfigRequest", outbound=False, outcome="failed"))
        if write_attempted:
            db.add(
                TelegramOperation(account_id="account", method="JoinChannelRequest", outbound=True, outcome="failed")
            )
    if write_attempted:
        with pytest.raises(TelegramGatewayError):
            await safety.resume_owner_after_bootstrap_repair("account", "owner", "Initial read transport repaired")
    else:
        state = await safety.resume_owner_after_bootstrap_repair("account", "owner", "Initial read transport repaired")
        assert not state["manual_hold"]
        assert state["restriction_status"] == "clear"  # existing value was retained, not fabricated


async def test_controller_persists_completed_join_after_attempt_marker(sql_store, monkeypatch):
    import importlib.util

    from app.modules.campaigns.models import Campaign
    from app.modules.sources.models import TelegramSource

    spec = importlib.util.spec_from_file_location(
        "scoped_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, "AsyncSessionLocal", sql_store.factory)
    await safety.approve_owner_attestation("account", "owner", "Trusted warmed cohort")
    now = datetime.now(UTC)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramSource(
                id="source",
                workspace_id="workspace",
                type="channel",
                username="sample",
                source_metadata={"demo_launch_assignments": {"launch": {"account_id": "account"}}},
            )
        )
        db.add(
            Campaign(
                id="campaign",
                workspace_id="workspace",
                type="comment_posts",
                name="Offline controller",
                status="paused",
                policy={},
                config={
                    "account_ids": ["account"],
                    "source_ids": ["source"],
                    "controlled_launch": {
                        "status": "joining",
                        "activate_not_before": (now + timedelta(hours=8)).isoformat(),
                        "jobs": [
                            {
                                "source_id": "source",
                                "kind": "channel",
                                "status": "queued",
                                "not_before": now.isoformat(),
                            }
                        ],
                    },
                },
            )
        )
    entity = SimpleNamespace(id=123, username="sample", broadcast=True, megagroup=False, left=True)
    linked = SimpleNamespace(id=456, username="discussion", megagroup=True, left=False, join_to_send=False)

    class OfflineClient:
        async def get_entity(self, ref):
            return entity

        async def __call__(self, request):
            if type(request).__name__ == "GetFullChannelRequest":
                return SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=456), chats=[entity, linked])
            return SimpleNamespace(updates=[])

    @asynccontextmanager
    async def fake_client(db, account):
        yield OfflineClient()

    monkeypatch.setattr(controller, "telegram_client_for_account", fake_client)
    row = {
        "account_id": "account",
        "campaign_id": "campaign",
        "workspace_id": "workspace",
        "source_ids": ["source"],
        "launch_id": "launch",
    }
    await controller.join_one(row)
    async with sql_store.factory() as db:
        campaign = await db.get(Campaign, "campaign")
        job = campaign.config["controlled_launch"]["jobs"][0]
        assert job["status"] == "joined"
        assert job["comments_enabled"] is True
        assert campaign.config["controlled_launch"]["status"] == "joining"
        source = await db.get(TelegramSource, "source")
        assert source.source_metadata["account_access"]["account"]["joined"] is True


async def test_controller_failed_account_does_not_stop_other_accounts(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "isolated_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    rows = [{"account_id": str(i)} for i in range(20)]
    joins = AsyncMock(side_effect=[RuntimeError("one account failed")] + [None] * 19)
    ticks = AsyncMock(side_effect=[RuntimeError("one campaign failed")] + [None] * 19)
    monkeypatch.setattr(controller, "join_one", joins)
    monkeypatch.setattr(controller, "campaigns_tick", ticks)
    await controller.tick_accounts(rows)
    assert joins.await_count == 20
    assert ticks.await_count == 20
    assert ticks.await_args_list[-1].args == ([rows[-1]],)


async def test_controller_starts_ready_campaign_while_other_account_is_held(sql_store, monkeypatch):
    import importlib.util

    from app.modules.campaigns.models import Campaign

    spec = importlib.util.spec_from_file_location(
        "independent_campaign_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, "AsyncSessionLocal", sql_store.factory)
    at = datetime.now(UTC) - timedelta(minutes=1)
    control = {
        "status": "joining",
        "activate_not_before": at.isoformat(),
        "jobs": [{"status": "joined", "kind": "channel", "comments_enabled": True}],
    }
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        db.add(TelegramAccount(id="other-account", workspace_id="workspace", import_type="session", status="active"))
        await db.flush()
        db.add(
            TelegramSafetyState(
                account_id="other-account", manual_hold=False, restriction_status="unknown", operator_approved_at=at
            )
        )
        db.add(
            Campaign(
                id="blocked-campaign",
                workspace_id="workspace",
                type="comment_posts",
                name="Blocked",
                status="running",
                policy={},
                config={"controlled_launch": control},
            )
        )
        db.add(
            Campaign(
                id="ready-campaign",
                workspace_id="workspace",
                type="comment_posts",
                name="Ready",
                status="paused",
                policy={},
                config={"controlled_launch": control},
            )
        )
    starts = AsyncMock()
    monkeypatch.setattr(controller, "start_comment_posts_campaign_run", starts)

    @asynccontextmanager
    async def offline_scope(*args):
        yield None

    monkeypatch.setattr(controller, "telegram_account_connection_scope", offline_scope)
    await controller.campaigns_tick(
        [
            {"campaign_id": "blocked-campaign", "account_id": "account", "workspace_id": "workspace"},
            {"campaign_id": "ready-campaign", "account_id": "other-account", "workspace_id": "workspace"},
        ]
    )
    assert starts.await_count == 1
    assert starts.call_args.args[2].id == "ready-campaign"
    async with sql_store.factory() as db:
        blocked = await db.get(Campaign, "blocked-campaign")
        ready = await db.get(Campaign, "ready-campaign")
        assert blocked.config["controlled_launch"]["status"] == "joining"
        assert blocked.status == "paused"
        assert ready.config["delivery_mode"] == "generate_and_send"
        assert ready.config["controlled_launch"]["status"] == "running"
        assert ready.status == "running"


async def test_controller_skips_missing_owner_admission(sql_store, monkeypatch):
    import importlib.util

    from app.modules.campaigns.models import Campaign

    spec = importlib.util.spec_from_file_location(
        "missing_admission_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, "AsyncSessionLocal", sql_store.factory)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = False
        db.add(
            Campaign(
                id="no-admission-campaign",
                workspace_id="workspace",
                type="comment_posts",
                name="No admission",
                status="paused",
                policy={},
                config={},
            )
        )
    client = Mock(side_effect=AssertionError("Must not open Telegram without admission"))
    monkeypatch.setattr(controller, "telegram_client_for_account", client)
    assert (
        await controller.join_one(
            {"account_id": "account", "campaign_id": "no-admission-campaign", "workspace_id": "workspace"}
        )
        is False
    )
    client.assert_not_called()


def test_owner_attestation_accepts_unchecked_null_but_rejects_limited():
    from app.modules.telegram_safety.admission import owner_attested

    account = SimpleNamespace(
        id="a",
        workspace_id="w",
        status="active",
        restriction_status=None,
        daily_limits={
            "owner_admission": {
                "basis": "owner_attestation",
                "active": True,
                "account_id": "a",
                "workspace_id": "w",
                "actor_id": "owner",
                "approved_at": "now",
                "modules": ["auto_join"],
            }
        },
    )
    assert owner_attested(account, "auto_join")
    account.restriction_status = "limited"
    assert not owner_attested(account, "auto_join")


def test_owner_join_override_expires_and_is_identity_bound():
    from app.modules.telegram_safety.admission import owner_join_limits

    admission = {
        "basis": "owner_attestation",
        "active": True,
        "account_id": "a",
        "workspace_id": "w",
        "actor_id": "owner",
        "approved_at": NOW.isoformat(),
        "modules": ["auto_join"],
    }
    override = {
        "basis": "owner_request",
        "account_id": "a",
        "workspace_id": "w",
        "actor_id": "owner",
        "approved_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "hour_limit": 30,
        "min_interval_seconds": 90,
    }
    admission["join_rate_override"] = override
    account = SimpleNamespace(
        id="a", workspace_id="w", status="active", restriction_status=None, daily_limits={"owner_admission": admission}
    )
    assert owner_join_limits(account, NOW) == (30, 90)
    assert owner_join_limits(account, NOW + timedelta(hours=1)) == (2, 1801)
    override["account_id"] = "other"
    assert owner_join_limits(account, NOW) == (2, 1801)
    override["account_id"] = "a"
    account.restriction_status = "limited"
    assert owner_join_limits(account, NOW) == (2, 1801)


async def test_owner_join_override_enforced_at_transport(sql_store, monkeypatch):
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import InputChannel

    await safety.approve_owner_attestation("account", "owner", "Owner accelerates joins")
    async with sql_store.factory.begin() as db:
        account = await db.get(TelegramAccount, "account")
        meta = dict(account.daily_limits)
        ad = dict(meta["owner_admission"])
        ad["join_rate_override"] = {
            "basis": "owner_request",
            "account_id": "account",
            "workspace_id": "workspace",
            "actor_id": "owner",
            "approved_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "hour_limit": 30,
            "min_interval_seconds": 90,
        }
        meta["owner_admission"] = ad
        account.daily_limits = meta
        state = await db.get(TelegramSafetyState, "account")
        state.outbound_attempt_limit = 35
    client = await sql_store.client()
    monkeypatch.setattr(safety, "utcnow", lambda: NOW)
    op = await client._begin_operation(JoinChannelRequest(InputChannel(1, 2)))
    await client._finish(op, "accepted")
    monkeypatch.setattr(safety, "utcnow", lambda: NOW + timedelta(seconds=89))
    with pytest.raises(TelegramGatewayError) as error:
        await client._begin_operation(JoinChannelRequest(InputChannel(3, 4)))
    assert error.value.code == "SAFETY_JOIN_RATE"
    monkeypatch.setattr(safety, "utcnow", lambda: NOW + timedelta(seconds=90))
    op = await client._begin_operation(JoinChannelRequest(InputChannel(3, 4)))
    await client._finish(op, "accepted")


async def test_controller_join_concurrency_is_bounded(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bounded_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    active = 0
    peak = 0
    finished = []

    async def joining(row):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        finished.append(row["account_id"])
        active -= 1

    monkeypatch.setattr(controller, "join_one", joining)
    monkeypatch.setattr(controller, "campaigns_tick", AsyncMock())
    await controller.tick_accounts([{"account_id": str(i)} for i in range(20)])
    assert peak == 4 and len(finished) == 20


async def test_join_approval_response_does_not_trip_account(rig, monkeypatch):
    from telethon.errors import InviteRequestSentError
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import InputChannel

    rig.sender.send = AsyncMock(side_effect=InviteRequestSentError(None))
    monkeypatch.setattr(rig.client, "_trip", AsyncMock())
    request = JoinChannelRequest(InputChannel(1, 2))
    request.resolve = AsyncMock()
    with pytest.raises(TelegramGatewayError) as error:
        await rig.client._call(rig.sender, request)
    assert error.value.code == "JOIN_APPROVAL_PENDING"
    assert rig.client._finish.call_args.args[1] == "approval_pending"
    rig.client._trip.assert_not_called()


async def test_join_approval_hold_repair_is_narrow(sql_store):
    await safety.approve_owner_attestation("account", "owner", "Owner authorizes")
    async with sql_store.factory.begin() as db:
        op = TelegramOperation(
            account_id="account",
            method="JoinChannelRequest",
            outbound=True,
            outcome="failed",
            error_code="InviteRequestSentError",
        )
        db.add(op)
        await db.flush()
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "unknown_rpc"
        state.operation_id = op.id
    repaired = await safety.resume_owner_after_join_request_classification("account", "owner")
    assert repaired["manual_hold"] is False
    async with sql_store.factory.begin() as db:
        op = await db.get(TelegramOperation, state.operation_id)
        assert op.outcome == "approval_pending" and op.error_code == "InviteRequestSentError"
        op.error_code = "PeerFloodError"
        op.outcome = "failed"
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "unknown_rpc"
    with pytest.raises(TelegramGatewayError) as error:
        await safety.resume_owner_after_join_request_classification("account", "owner")
    assert error.value.code == "SAFETY_REVIEW_REQUIRED"


@pytest.mark.parametrize("outbound", [False, True])
async def test_elapsed_owner_read_wait_preserves_deadline_and_cannot_resume_write(sql_store, outbound):
    await safety.approve_owner_attestation("account", "owner", "Owner authorizes")
    deadline = NOW - timedelta(seconds=1)
    async with sql_store.factory.begin() as db:
        op = TelegramOperation(
            account_id="account",
            method="JoinChannelRequest" if outbound else "ResolveUsernameRequest",
            outbound=outbound,
            outcome="failed",
            error_code="FloodWaitError",
        )
        db.add(op)
        await db.flush()
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "flood_wait"
        state.operation_id = op.id
        state.wait_until = deadline
    if outbound:
        with pytest.raises(TelegramGatewayError) as error:
            await safety.resume_owner_after_elapsed_read_wait("account", "owner")
        assert error.value.code == "SAFETY_REVIEW_REQUIRED"
    else:
        result = await safety.resume_owner_after_elapsed_read_wait("account", "owner")
        assert not result["manual_hold"]
        async with sql_store.factory() as db:
            state = await db.get(TelegramSafetyState, "account")
            assert state.wait_until.replace(tzinfo=UTC) == deadline


async def test_replacement_preflight_rejects_approval_before_join(sql_store, monkeypatch):
    import importlib.util

    from app.modules.campaigns.models import Campaign
    from app.modules.sources.models import TelegramSource

    spec = importlib.util.spec_from_file_location(
        "approval_preflight_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, "AsyncSessionLocal", sql_store.factory)
    await safety.approve_owner_attestation("account", "owner", "Owner replacement")
    at = datetime.now(UTC)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramSource(
                id="s",
                workspace_id="workspace",
                type="channel",
                username="sample",
                source_metadata={"demo_open_preflight": {"approved": True}},
            )
        )
        db.add(
            Campaign(
                id="c",
                workspace_id="workspace",
                type="comment_posts",
                name="Preflight",
                status="paused",
                policy={},
                config={
                    "account_ids": ["account"],
                    "source_ids": ["s"],
                    "controlled_launch": {
                        "status": "joining",
                        "activate_not_before": at.isoformat(),
                        "jobs": [
                            {"source_id": "s", "kind": "channel", "status": "queued", "not_before": at.isoformat()}
                        ],
                    },
                },
            )
        )
    channel = SimpleNamespace(id=1, broadcast=True, left=True)
    discussion = SimpleNamespace(id=2, join_request=True, join_to_send=False)
    calls = []

    class Client:
        async def get_entity(self, ref):
            return channel

        async def __call__(self, request):
            calls.append(type(request).__name__)
            return SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=2), chats=[channel, discussion])

    @asynccontextmanager
    async def fake_client(db, account):
        yield Client()

    monkeypatch.setattr(controller, "telegram_client_for_account", fake_client)
    assert (
        await controller.join_one(
            {
                "account_id": "account",
                "campaign_id": "c",
                "workspace_id": "workspace",
                "source_ids": ["s"],
                "launch_id": "launch",
            }
        )
        is False
    )
    assert calls == ["GetFullChannelRequest"]
    async with sql_store.factory() as db:
        c = await db.get(Campaign, "c")
        assert c.config["controlled_launch"]["jobs"][0]["error_code"] == "SOURCE_NOT_OPEN"


async def test_source_private_read_does_not_trip_account(rig, monkeypatch):
    from telethon.errors import ChannelPrivateError
    from telethon.tl.types import InputPeerChannel

    rig.sender.send = AsyncMock(side_effect=ChannelPrivateError(None))
    monkeypatch.setattr(rig.client, "_trip", AsyncMock())
    request = GetHistoryRequest(InputPeerChannel(1, 2), 0, None, 0, 5, 0, 0, 0)
    request.resolve = AsyncMock()
    with pytest.raises(TelegramGatewayError) as error:
        await rig.client._call(rig.sender, request)
    assert error.value.code == "SOURCE_READ_DENIED"
    assert rig.client._finish.call_args.args[1] == "source_denied"
    rig.client._trip.assert_not_called()


async def test_source_read_denial_repair_preserves_target(sql_store):
    await safety.approve_owner_attestation("account", "owner", "Owner replacement")
    async with sql_store.factory.begin() as db:
        op = TelegramOperation(
            account_id="account",
            method="GetHistoryRequest",
            target="target",
            outbound=False,
            outcome="failed",
            error_code="ChannelPrivateError",
        )
        db.add(op)
        await db.flush()
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "unknown_rpc"
        state.operation_id = op.id
        opid = op.id
    result = await safety.resume_owner_after_source_read_denial("account", "owner")
    assert not result["manual_hold"]
    async with sql_store.factory() as db:
        op = await db.get(TelegramOperation, opid)
        assert op.outcome == "source_denied" and op.target == "target" and op.error_code == "ChannelPrivateError"


@pytest.mark.parametrize("later_rpc", [False, True])
@pytest.mark.parametrize("action_type", ["parse_source", "generate_and_send_comment"])
async def test_preconnect_resume_requires_no_later_dispatch(sql_store, later_rpc, action_type):
    from app.modules.campaigns.models import Campaign, CampaignAction

    await safety.approve_owner_attestation("account", "owner", "Explicit warmed owner cohort")
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "transport_error"
        state.received_at = NOW
        state.operation_id = None
        state.restriction_status = "unknown"
        db.add(Campaign(id="repair-c", workspace_id="workspace", type="comment_posts", name="Offline"))
        await db.flush()
        db.add(
            CampaignAction(
                campaign_id="repair-c",
                telegram_account_id="account",
                action_type=action_type,
                target_type="source",
                target_id="offline-source",
                status="failed",
                error_code="TELEGRAM_CONNECTION_FAILED",
                created_at=NOW,
            )
        )
        if later_rpc:
            db.add(
                TelegramOperation(
                    account_id="account",
                    method="GetUsersRequest",
                    outcome="accepted",
                    created_at=NOW + timedelta(seconds=1),
                )
            )
    if later_rpc:
        with pytest.raises(TelegramGatewayError) as caught:
            await safety.resume_owner_after_preconnect_repair("account", "owner", "Same proxy tunnel checked")
        assert caught.value.code == "SAFETY_REVIEW_REQUIRED"
    else:
        result = await safety.resume_owner_after_preconnect_repair("account", "owner", "Same proxy tunnel checked")
        assert result["manual_hold"] is False
        assert result["restriction_status"] == "unknown"


async def test_controller_campaign_timeout_is_isolated(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "timeout_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(controller, "join_one", AsyncMock())
    attempted = []

    async def fake_tick(rows):
        attempted.append(rows[0]["account_id"])
        if rows[0]["account_id"] == "bad":
            raise TimeoutError("bounded network failure")

    monkeypatch.setattr(controller, "campaigns_tick", fake_tick)
    await controller.tick_accounts([{"account_id": "bad"}, {"account_id": "good"}])
    assert attempted == ["bad", "good"]


async def test_polling_scope_connects_once_and_never_reuses_another_account(monkeypatch):
    accounts = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    clients = {
        a.id: SimpleNamespace(
            connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True), disconnect=AsyncMock()
        )
        for a in accounts
    }

    async def build(db, account):
        return clients[account.id]

    monkeypatch.setattr(gateway, "build_telegram_client_for_account", build)

    async def poll(account):
        async with gateway.telegram_account_connection_scope(None, account) as outer:
            for _ in range(15):
                async with gateway.telegram_client_for_account(None, account) as inner:
                    assert inner is outer is clients[account.id]
                await asyncio.sleep(0)
            assert clients[account.id].disconnect.await_count == 0

    await asyncio.gather(*(poll(a) for a in accounts))
    for client in clients.values():
        client.connect.assert_awaited_once()
        client.is_user_authorized.assert_awaited_once()
        client.disconnect.assert_awaited_once()
    assert gateway._scoped_account_client.get() is None


async def test_controller_keeps_lock_on_reserved_physical_connection(monkeypatch, tmp_path):
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location(
        "connection_controller", ROOT / "artifacts/demo-launch-finish-2026-10-05/controller.py"
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "workspace_id": "w",
                "launch_id": "offline",
                "accounts": [{"account_id": str(i), "campaign_id": str(i)} for i in range(20)],
            }
        )
    )
    monkeypatch.setattr(controller, "PLAN", plan)
    connection = SimpleNamespace(scalar=AsyncMock(return_value=True), commit=AsyncMock(), execute=AsyncMock())

    @asynccontextmanager
    async def connect():
        yield connection

    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: list(range(20)))))

    @asynccontextmanager
    async def store():
        yield db

    monkeypatch.setattr(controller, "engine", SimpleNamespace(connect=connect))
    monkeypatch.setattr(controller, "AsyncSessionLocal", store)
    monkeypatch.setattr(controller, "tick_accounts", AsyncMock())
    await controller.main()
    connection.scalar.assert_awaited_once()
    connection.execute.assert_awaited_once()
    assert "pg_advisory_unlock" in str(connection.execute.await_args.args[0])


async def test_explicit_task_cancellation_still_propagates(rig):
    started = asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    rig.sender.send.side_effect = send
    task = asyncio.create_task(rig.client._call(rig.sender, rig.request))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.state.manual_hold is True
    assert rig.client._finish.call_args.args[1] == "unknown_delivery"


@pytest.mark.parametrize("outbound", [False, True])
async def test_read_transport_review_cannot_resume_a_write(sql_store, outbound):
    await safety.approve_owner_attestation("account", "owner", "Owner warmed cohort")
    async with sql_store.factory.begin() as db:
        op = TelegramOperation(
            id="read-failure",
            account_id="account",
            method="SendMessageRequest" if outbound else "GetHistoryRequest",
            outbound=outbound,
            outcome="failed",
            error_code="ConnectionError",
            created_at=NOW,
        )
        db.add(op)
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "transport_error"
        state.operation_id = op.id
    if outbound:
        with pytest.raises(TelegramGatewayError):
            await safety.resume_owner_after_read_transport_review(
                "account", "owner", "Same proxy verified, transport corrected"
            )
    else:
        result = await safety.resume_owner_after_read_transport_review(
            "account", "owner", "Same proxy verified, transport corrected"
        )
        assert result["manual_hold"] is False


@pytest.mark.parametrize("still_assigned", [True, False])
async def test_rejected_discussion_must_be_unassigned_before_continuation(sql_store, still_assigned):
    from app.modules.campaigns.models import Campaign
    from app.modules.sources.models import TelegramSource

    await safety.approve_owner_attestation("account", "owner", "Owner warmed cohort")
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramSource(
                id="denied-source",
                workspace_id="workspace",
                url="https://example.test",
                type="channel",
                linked_discussion_tg_id="123",
            )
        )
        db.add(
            Campaign(
                id="denied-c",
                workspace_id="workspace",
                type="comment_posts",
                name="Offline",
                config={"account_ids": ["account"], "source_ids": ["denied-source"] if still_assigned else []},
            )
        )
        op = TelegramOperation(
            id="denied-write",
            account_id="account",
            method="SendMessageRequest",
            outbound=True,
            outcome="failed",
            error_code="UserBannedInChannelError",
            target="123",
            created_at=NOW,
        )
        db.add(op)
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "source_denied"
        state.scope = "account_target:123"
        state.operation_id = op.id
    with pytest.raises(TelegramGatewayError):
        await safety.resume_owner_after_excluded_discussion("account", "owner")
    async with sql_store.factory() as db:
        assert (await db.get(TelegramSafetyState, "account")).manual_hold


@pytest.mark.parametrize("rpc_dispatched", [False, True])
async def test_scoped_setup_cancel_requires_zero_rpc_since_owner_poll(sql_store, rpc_dispatched):
    from app.modules.campaigns.models import Campaign

    await safety.approve_owner_attestation("account", "owner", "Owner warmed cohort")
    async with sql_store.factory.begin() as db:
        db.add(
            Campaign(
                id="setup-c",
                workspace_id="workspace",
                type="comment_posts",
                name="Offline",
                config={"account_ids": ["account"]},
            )
        )
        db.add(
            TelegramOperation(
                account_id="account",
                method="operator.request_recovery_poll",
                outcome="approved",
                target="setup-c",
                created_at=NOW - timedelta(minutes=3),
            )
        )
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "transport_error"
        state.received_at = NOW
        state.operation_id = None
        if rpc_dispatched:
            db.add(
                TelegramOperation(
                    account_id="account",
                    method="GetHistoryRequest",
                    outcome="accepted",
                    created_at=NOW - timedelta(minutes=1),
                )
            )
    if rpc_dispatched:
        with pytest.raises(TelegramGatewayError):
            await safety.resume_owner_after_preconnect_repair(
                "account", "owner", "Reviewed scoped setup cancellation; zero RPC before cancellation"
            )
    else:
        result = await safety.resume_owner_after_preconnect_repair(
            "account", "owner", "Reviewed scoped setup cancellation; zero RPC before cancellation"
        )
        assert result["manual_hold"] is False


@pytest.mark.parametrize("method,outbound", [("GetDiscussionMessageRequest", False), ("SendMessageRequest", True)])
async def test_invalid_post_read_review_never_reclassifies_write(sql_store, method, outbound):
    await safety.approve_owner_attestation("account", "owner", "Owner warmed cohort")
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                id="missing-post",
                account_id="account",
                method=method,
                outbound=outbound,
                outcome="failed",
                error_code="MsgIdInvalidError",
                created_at=NOW,
            )
        )
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "unknown_rpc"
        state.operation_id = "missing-post"
    if outbound:
        with pytest.raises(TelegramGatewayError):
            await safety.resume_owner_after_invalid_post_read("account", "owner")
    else:
        result = await safety.resume_owner_after_invalid_post_read("account", "owner")
        assert not result["manual_hold"]
        async with sql_store.factory() as db:
            assert (await db.get(TelegramOperation, "missing-post")).outcome == "post_unavailable"


@pytest.mark.parametrize("method", ["GetDiscussionMessageRequest", "SendMessageRequest"])
async def test_invalid_post_classification_does_not_hide_write_error(rig, method):
    request = type(method, (), {"resolve": AsyncMock()})()
    rig.sender.send.side_effect = type("MsgIdInvalidError", (Exception,), {})("invalid post")
    with pytest.raises(TelegramGatewayError):
        await rig.client._call(rig.sender, request)
    if method == "GetDiscussionMessageRequest":
        assert not rig.state.manual_hold
        assert rig.client._finish.call_args.args[1] == "post_unavailable"
    else:
        assert rig.state.manual_hold
        assert rig.client._finish.call_args.args[1] == "failed"


@pytest.mark.parametrize("outcome,expected", [("accepted", "send_interval"), ("failed", None)])
async def test_transport_receipt_enforces_spacing_without_comment_row(sql_store, monkeypatch, outcome, expected):
    from app.modules.campaigns import engine as campaign_engine

    monkeypatch.setattr(campaign_engine, "_account_can_send_comments", lambda account: True)
    at = datetime.now(UTC)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                account_id="account",
                method="SendMessageRequest",
                outbound=True,
                outcome=outcome,
                created_at=at - timedelta(minutes=10),
            )
        )
    async with sql_store.factory() as db:
        account = await db.get(TelegramAccount, "account")
        result = await campaign_engine._comment_account_send_wait_reason(
            db, "workspace", account, {"daily_limit_per_account": 5, "min_send_interval_seconds": 17280}, at
        )
        assert result == expected


async def test_peer_flood_is_not_misclassified_as_a_timed_wait(rig):
    await rig.client._trip(type("PeerFloodError", (Exception,), {})("anti-spam"), "op")
    assert rig.state.reason == "spam_restricted"
    assert rig.state.restriction_status == "limited"
    assert rig.state.manual_hold
    assert rig.state.wait_until is None


async def test_spam_diagnosis_uses_blocking_rpc_not_last_operator_event(sql_store):
    await safety.approve_owner_attestation("account", "owner", "Owner warmed cohort")
    async with sql_store.factory.begin() as db:
        db.add_all(
            [
                TelegramOperation(
                    id="first-denial",
                    account_id="account",
                    method="SendMessageRequest",
                    outbound=True,
                    outcome="failed",
                    error_code="UserBannedInChannelError",
                    target="one",
                    created_at=NOW - timedelta(minutes=1),
                ),
                TelegramOperation(
                    id="blocking-denial",
                    account_id="account",
                    method="SendMessageRequest",
                    outbound=True,
                    outcome="failed",
                    error_code="UserBannedInChannelError",
                    target="two",
                    created_at=NOW,
                ),
                TelegramOperation(
                    id="operator-event",
                    account_id="account",
                    method="operator.note",
                    outbound=False,
                    outcome="approved",
                    created_at=NOW + timedelta(minutes=1),
                ),
            ]
        )
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "source_denied"
        state.operation_id = "blocking-denial"
        state.restriction_status = "unknown"
    result = await safety.get_safety_state("account")
    assert result["last_operation"]["id"] == "operator-event"
    assert result["blocking_operation"]["id"] == "blocking-denial"
    assert result["diagnosis"]["spam_signal"] == "strong_suspected"
    assert result["diagnosis"]["distinct_targets"] == 2
    assert result["restriction_status"] == "unknown"
    from app.modules.telegram_safety.schemas import SafetyStateResponse

    parsed = SafetyStateResponse.model_validate(result)
    assert parsed.diagnosis["raw_code"] == "UserBannedInChannelError"
    assert parsed.blocking_operation.target == "two"


@pytest.mark.parametrize(
    "code,method,category,scope,signal",
    [
        ("USER_BANNED_IN_CHANNEL", "SendMessageRequest", "write_restriction", "source", "suspected"),
        ("PeerFloodError", "SendMessageRequest", "spam_restriction", "account", "rpc_confirmed"),
        ("FloodWaitError", "GetHistoryRequest", "rate_limit", "account", "none"),
        ("SlowModeWaitError", "SendMessageRequest", "rate_limit", "account", "none"),
        ("ChannelPrivateError", "GetHistoryRequest", "source_access", "source", "none"),
        ("MsgIdInvalidError", "GetDiscussionMessageRequest", "invalid_post", "post", "none"),
        ("MsgIdInvalidError", "SendMessageRequest", "invalid_post", "account", "none"),
        ("ChatGuestSendForbiddenError", "SendMessageRequest", "membership", "source", "none"),
        ("AuthKeyDuplicatedError", None, "session", "account", "none"),
        ("UserDeactivatedBanError", None, "account_disabled", "account", "none"),
        ("TimeoutError", "SendMessageRequest", "transport", "account", "none"),
        ("UnexpectedError", "SendMessageRequest", "unknown", "account", "none"),
    ],
)
def test_error_catalog_separates_spam_permissions_and_transport(code, method, category, scope, signal):
    from app.modules.telegram_safety.error_catalog import classify_error

    row = classify_error(code, method)
    assert (row["category"], row["stop_scope"], row["spam_signal"]) == (category, scope, signal)
    assert not row["automatic_retry"]


async def test_local_banned_discussion_does_not_stop_other_sources(rig):
    rig.sender.send.side_effect = type("UserBannedInChannelError", (Exception,), {})("write denied")
    first = type("SendMessageRequest", (), {"resolve": AsyncMock(), "peer": SimpleNamespace(channel_id="one")})()
    with pytest.raises(TelegramGatewayError) as error:
        await rig.client._call(rig.sender, first)
    assert error.value.code == "SOURCE_ACCESS_DENIED"
    assert rig.client._finish.call_args.args[1] == "source_denied"
    assert not rig.state.manual_hold
    assert not rig.client._safety_stopped
    rig.sender.send.side_effect = None
    second = type("SendMessageRequest", (), {"resolve": AsyncMock(), "peer": SimpleNamespace(channel_id="two")})()
    await rig.client._call(rig.sender, second)
    assert rig.sender.send.await_count == 2


@pytest.mark.parametrize(
    "body,status",
    [
        ("Good news, no limits are currently applied to your account.", "clear"),
        ("While the account is limited, you cannot send messages to strangers.", "limited"),
        ("Your account has been blocked.", "blocked"),
        ("Please describe your problem.", "unknown"),
        ("A sentence about limits without a status.", "unknown"),
    ],
)
def test_spambot_reply_requires_explicit_status(body, status):
    from app.modules.telegram_safety.spambot import parse_spambot_reply

    assert parse_spambot_reply(body) == status


@pytest.mark.parametrize("target,body", [(999, "/start"), (178220800, "advertisement")])
def test_spambot_diagnostic_cannot_send_elsewhere(target, body):
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputPeerUser

    from app.modules.telegram_safety.spambot import SpamBotDiagnosticClient

    client = object.__new__(SpamBotDiagnosticClient)
    client.bot_id = 178220800
    client.start_message_id = None
    with pytest.raises(TelegramGatewayError):
        client._validate_request(SendMessageRequest(InputPeerUser(target, 1), body, random_id=42))


async def test_spambot_single_start_is_durable_and_does_not_release_hold(sql_store, monkeypatch):
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputPeerUser

    from app.modules.telegram_safety import spambot

    monkeypatch.setattr(spambot, "AsyncSessionLocal", sql_store.factory)
    monkeypatch.setattr(spambot, "utcnow", lambda: NOW)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
    permit = await spambot.authorize_diagnostic("account", "owner")
    client = object.__new__(spambot.SpamBotDiagnosticClient)
    client.safety_account_id = "account"
    client.actor_id = "owner"
    client.permit_id = permit
    client.safety_fingerprint = "aa" * 32
    client.bot_id = 178220800
    client.start_message_id = None
    request = SendMessageRequest(InputPeerUser(178220800, 1), "/start", random_id=42)
    op_id = await client._begin_operation(request)
    assert op_id
    with pytest.raises(TelegramGatewayError):
        await client._begin_operation(request)
    with pytest.raises(TelegramGatewayError):
        await spambot.authorize_diagnostic("account", "owner")
    async with sql_store.factory() as db:
        assert (await db.get(TelegramSafetyState, "account")).manual_hold
        assert (await db.get(TelegramOperation, op_id)).method == "diagnostic.spambot.SendMessageRequest"


@pytest.mark.parametrize("actor,expired", [("other", False), ("owner", True)])
async def test_spambot_permit_is_owner_bound_and_expires(sql_store, monkeypatch, actor, expired):
    from app.modules.telegram_safety import spambot

    monkeypatch.setattr(spambot, "AsyncSessionLocal", sql_store.factory)
    monkeypatch.setattr(spambot, "utcnow", lambda: NOW)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
    permit = await spambot.authorize_diagnostic("account", "owner")
    if expired:
        async with sql_store.factory.begin() as db:
            p = await db.get(TelegramOperation, permit)
            p.created_at = NOW - timedelta(minutes=11)
    client = object.__new__(spambot.SpamBotDiagnosticClient)
    client.safety_account_id = "account"
    client.actor_id = actor
    client.permit_id = permit
    client.safety_fingerprint = "aa" * 32
    async with sql_store.factory() as db:
        with pytest.raises(TelegramGatewayError):
            await client._permit(db)


@pytest.mark.parametrize(
    "code,restriction,status",
    [
        ("UserBannedInChannelError", "unknown", "quarantined"),
        ("UserBannedInChannelError", "limited", "spam_blocked"),
        ("ConnectionError", "unknown", "active"),
    ],
)
async def test_first_error_is_visible_in_account_card_without_parent_row_write(sql_store, code, restriction, status):
    from app.modules.telegram_safety.views import apply_safety_projection

    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                id="card-error",
                account_id="account",
                method="SendMessageRequest",
                outbound=True,
                outcome="failed",
                error_code=code,
            )
        )
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.operation_id = "card-error"
        state.restriction_status = restriction
    async with sql_store.factory() as db:
        account = await db.get(TelegramAccount, "account")
        original = account.status
        result = await apply_safety_projection(db, account, {"status": "active", "restriction_status": "unknown"})
        assert result["status"] == status
        assert account.status == original


async def test_spambot_command_id_uses_outgoing_mapping_not_incoming_reply(monkeypatch):
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputPeerUser, UpdateMessageID

    from app.modules.telegram_safety.spambot import SpamBotDiagnosticClient

    incoming = type("UpdateNewMessage", (), {"message": SimpleNamespace(id=102, out=False)})()
    result = SimpleNamespace(updates=[incoming, UpdateMessageID(id=101, random_id=42)])
    monkeypatch.setattr(safety.GuardedTelegramClient, "_call", AsyncMock(return_value=result))
    client = object.__new__(SpamBotDiagnosticClient)
    client.bot_id = 178220800
    client.start_message_id = None
    await client._call(None, SendMessageRequest(InputPeerUser(178220800, 1), "/start", random_id=42))
    assert client.start_message_id == 101


async def test_verified_spambot_evidence_confirms_label_without_releasing_hold(sql_store):
    async with sql_store.factory.begin() as db:
        account = await db.get(TelegramAccount, "account")
        account.daily_limits = {
            "restriction_status": {"source": "verified_spambot", "status": "limited", "checked_at": NOW.isoformat()}
        }
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.restriction_status = "limited"
    result = await safety.get_safety_state("account")
    assert result["diagnosis"]["spam_signal"] == "confirmed"
    assert result["manual_hold"]


async def test_spambot_bootstrap_correction_is_bounded_read_only(sql_store, monkeypatch):
    from telethon.tl.functions.help import GetConfigRequest

    from app.modules.telegram_safety import spambot

    monkeypatch.setattr(spambot, "AsyncSessionLocal", sql_store.factory)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                account_id="account", method=spambot.PREFIX + "GetConfigRequest", outbound=False, outcome="pending"
            )
        )
    client = object.__new__(spambot.SpamBotDiagnosticClient)
    client.safety_account_id = "account"
    client._permit = AsyncMock()
    pending = SimpleNamespace(request=GetConfigRequest())
    queue = SimpleNamespace(extend=Mock())
    client._sender = SimpleNamespace(
        _pop_states=Mock(return_value=[pending]), _state=SimpleNamespace(salt=0), _send_queue=queue
    )
    client._reject_protocol_states = AsyncMock()
    msg = SimpleNamespace(obj=safety.BadServerSalt(1, 1, 48, 42))
    await client._handle_bootstrap_notification(msg)
    assert client._sender._state.salt == 42
    queue.extend.assert_called_once()
    await client._handle_bootstrap_notification(msg)
    assert queue.extend.call_count == 1
    client._reject_protocol_states.assert_awaited_once()


async def test_spambot_transport_error_preserves_original_stop(sql_store, monkeypatch):
    from app.modules.telegram_safety import spambot

    monkeypatch.setattr(spambot, "AsyncSessionLocal", sql_store.factory)
    async with sql_store.factory.begin() as db:
        db.add(
            TelegramOperation(
                id="original-write-denial",
                account_id="account",
                method="SendMessageRequest",
                outcome="failed",
                error_code="UserBannedInChannelError",
            )
        )
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
        state.reason = "spam_suspected"
        state.operation_id = "original-write-denial"
    client = object.__new__(spambot.SpamBotDiagnosticClient)
    client.safety_account_id = "account"
    await client._trip(ConnectionError("diagnostic transport failed"), "read-op")
    async with sql_store.factory() as db:
        state = await db.get(TelegramSafetyState, "account")
        assert state.manual_hold and state.reason == "spam_suspected" and state.operation_id == "original-write-denial"
    assert client._safety_stopped


async def test_spambot_precommand_repair_never_allows_second_command(sql_store, monkeypatch):
    from app.modules.telegram_safety import spambot

    monkeypatch.setattr(spambot, "AsyncSessionLocal", sql_store.factory)
    monkeypatch.setattr(spambot, "utcnow", lambda: NOW)
    async with sql_store.factory.begin() as db:
        state = await db.get(TelegramSafetyState, "account")
        state.manual_hold = True
    permit = await spambot.authorize_diagnostic("account", "owner")
    async with sql_store.factory.begin() as db:
        (await db.get(TelegramOperation, permit)).outcome = "completed"
        db.add(
            TelegramOperation(
                account_id="account",
                method=spambot.PREFIX + "GetConfigRequest",
                outcome="accepted",
                outbound=False,
                created_at=NOW,
            )
        )
    assert await spambot.repair_precommand_diagnostic("account", "owner") == permit
    with pytest.raises(TelegramGatewayError):
        await spambot.repair_precommand_diagnostic("account", "owner")
    async with sql_store.factory.begin() as db:
        (await db.get(TelegramOperation, permit)).outcome = "completed"
        db.add(
            TelegramOperation(
                account_id="account",
                method=spambot.PREFIX + "SendMessageRequest",
                outcome="accepted",
                outbound=True,
                created_at=NOW,
            )
        )
    with pytest.raises(TelegramGatewayError):
        await spambot.repair_precommand_diagnostic("account", "owner")
    async with sql_store.factory() as db:
        assert (await db.get(TelegramSafetyState, "account")).manual_hold


async def test_spambot_connect_does_not_fetch_profile():
    from app.modules.telegram_safety.spambot import SpamBotDiagnosticClient

    client = object.__new__(SpamBotDiagnosticClient)
    assert await client.get_me() is None


@pytest.mark.parametrize(
    "rpc_error",
    ["ChannelPrivateError", "ChatWriteForbiddenError", "ChatAdminRequiredError", "ChatGuestSendForbiddenError"],
)
async def test_local_source_write_denial_keeps_account_usable(rig, monkeypatch, rpc_error):
    from telethon import errors

    rig.sender.send = AsyncMock(side_effect=getattr(errors, rpc_error)(None))
    monkeypatch.setattr(rig.client, "_trip", AsyncMock())
    with pytest.raises(TelegramGatewayError) as error:
        await rig.client._call(rig.sender, rig.request)
    assert error.value.code == "SOURCE_ACCESS_DENIED"
    assert error.value.raw["rpc_error"] == rpc_error
    assert rig.client._finish.call_args.args[1] == "source_denied"
    rig.client._trip.assert_not_called()


@pytest.mark.parametrize("method,code", [("GetHistoryRequest", "ConnectionError"), ("PingRequest", "BadRequestError")])
async def test_owner_transient_read_waits_without_permanent_hold(sql_store, method, code):
    await safety.approve_owner_attestation("account", "owner", "Owner admitted working cohort")
    request = type(method, (), {})()
    client = await sql_store.client()
    await client._trip(type(code, (Exception,), {})("read failed"), request=request)
    async with sql_store.factory() as db:
        state = await db.get(TelegramSafetyState, "account")
        assert state.manual_hold is False
        assert state.reason == "owner_attested_admission"
        assert state.wait_until is not None


async def test_delivery_receipts_enforce_25_even_with_failed_attempt_headroom(sql_store):
    await safety.approve_owner_attestation("account", "owner", "Owner target25")
    async with sql_store.factory.begin() as db:
        a = await db.get(safety.TelegramAccount, "account")
        limits = dict(a.daily_limits)
        limits["owner_admission"] = {
            **limits["owner_admission"],
            "comment_attempt_limit": 75,
            "comment_delivery_limit": 25,
        }
        a.daily_limits = limits
        for i in range(25):
            db.add(
                TelegramOperation(
                    account_id="account",
                    method="SendMessageRequest",
                    outbound=True,
                    outcome="accepted",
                    message_id=str(i),
                    created_at=NOW - timedelta(minutes=30),
                )
            )
    client = await sql_store.client("new-unique-intent")
    request = type("SendMessageRequest", (), {})()
    with pytest.raises(TelegramGatewayError) as error:
        await client._begin_operation(request)
    assert error.value.code == "SAFETY_COMMENT_BUDGET"


@pytest.mark.parametrize("error_name", ["UsernameNotOccupiedError", "UsernameInvalidError"])
async def test_missing_source_username_does_not_stop_account(rig, error_name):
    from telethon import errors
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    request = ResolveUsernameRequest(username="missing_channel")
    rig.sender.send.side_effect = getattr(errors, error_name)(request)
    with pytest.raises(TelegramGatewayError) as caught:
        await rig.client._call(rig.sender, request)
    assert caught.value.code == "SOURCE_READ_DENIED"
    assert rig.state.manual_hold is False
    assert rig.client._safety_stopped is False
    rig.client._finish.assert_awaited_once()
    assert rig.client._finish.await_args.args[1] == "source_denied"
    diagnosis = safety.classify_error(error_name, "ResolveUsernameRequest")
    assert diagnosis["stop_scope"] == "source"
    assert diagnosis["restriction_status"] is None
    assert diagnosis["spam_signal"] == "none"
