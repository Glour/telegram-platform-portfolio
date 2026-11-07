"""Offline authorization and payload serialization regressions for manual Telegram."""

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl import types
from telethon.tl.functions.messages import GetHistoryRequest

from app.modules.telegram_gateway.contracts import TelegramGatewayError

path = os.getenv("MANUAL_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_manual/service.py"
)
spec = importlib.util.spec_from_file_location("manual_under_test", path)
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class ManualTelegramTests(unittest.TestCase):
    def test_peer_reference_bound_to_account_actor_and_kind(self):
        token = manual.seal("a", "owner", "peer", {"kind": "chat", "id": 5})
        self.assertEqual(manual.unseal(token, "a", "owner", "peer")["id"], 5)
        for args in [("other", "owner", "peer"), ("a", "other", "peer"), ("a", "owner", "button")]:
            with self.assertRaises(TelegramGatewayError):
                manual.unseal(token, *args)

    def test_tampered_reference_rejected(self):
        token = manual.seal("a", "owner", "peer", {"id": 5})
        with self.assertRaises(TelegramGatewayError):
            manual.unseal(token[:-10] + "xxxxxxxxxx", "a", "owner", "peer")

    def test_expired_reference_rejected(self):
        token = manual.seal("a", "owner", "peer", {"id": 5})
        with patch.object(manual, "utcnow", return_value=manual.utcnow() + manual.timedelta(hours=1)):
            with self.assertRaises(TelegramGatewayError):
                manual.unseal(token, "a", "owner", "peer")

    def test_peer_roundtrip(self):
        for peer in [types.InputPeerUser(5, 99), types.InputPeerChannel(6, 100), types.InputPeerChat(7)]:
            self.assertEqual(bytes(manual.input_peer(manual.peer_data(peer))), bytes(peer))

    def test_unsafe_link_schemes_not_rendered(self):
        for value in ["javascript:alert(1)", "data:text/html,<script>", "file:///etc/passwd"]:
            self.assertIsNone(manual.safe_url(value))
        self.assertEqual(
            manual.safe_url("https://telegram.org/captcha?actor=x"), "https://telegram.org/captcha?actor=x"
        )

    def test_utf16_link_after_emoji(self):
        body = "😀 human"
        m = SimpleNamespace(
            id=10,
            message=body,
            entities=[types.MessageEntityTextUrl(3, 5, "https://telegram.org/captcha")],
            reply_markup=None,
            out=False,
            date=None,
            media=None,
        )
        result = manual.message_view(m, "a", "owner", {"kind": "chat", "id": 5})
        self.assertEqual(result["links"], [{"text": "human", "url": "https://telegram.org/captcha"}])

    def test_buttons_cannot_switch_account_or_peer(self):
        peer = {"kind": "user", "id": 5, "hash": 9}
        m = SimpleNamespace(
            id=10,
            message="test",
            entities=[],
            reply_markup=types.ReplyInlineMarkup(
                [types.KeyboardButtonRow([types.KeyboardButtonCallback("OK", b"abc")])]
            ),
            out=False,
            date=None,
            media=None,
        )
        result = manual.message_view(m, "a", "owner", peer)
        value = manual.unseal(result["buttons"][0][0]["action"], "a", "owner", "button")
        self.assertEqual(value["callback"], "616263")
        self.assertEqual(value["peer"], peer)
        with self.assertRaises(TelegramGatewayError):
            manual.unseal(result["buttons"][0][0]["action"], "b", "owner", "button")

    def test_url_button_is_link_not_outbound_command(self):
        m = SimpleNamespace(
            id=10,
            message="",
            entities=[],
            reply_markup=types.ReplyInlineMarkup(
                [types.KeyboardButtonRow([types.KeyboardButtonUrl("human", "https://telegram.org/captcha")])]
            ),
            out=False,
            date=None,
            media=None,
        )
        button = manual.message_view(m, "a", "owner", {})["buttons"][0][0]
        self.assertEqual(button["url"], "https://telegram.org/captcha")
        self.assertNotIn("action", button)


class ManualRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_rpc_denied_before_ledger_or_network(self):
        client = object.__new__(manual.ManualClient)
        client.expected_request = b"not-this-query"
        client.writing = False
        query = GetHistoryRequest(types.InputPeerChat(5), 0, None, 0, 50, 0, 0, 0)
        with self.assertRaises(TelegramGatewayError):
            await client._begin_operation(query)


class ManualPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.modules.telegram_accounts.models import TelegramAccount
        from app.modules.telegram_safety.models import TelegramOperation, TelegramSafetyState
        from app.modules.workspaces.models import Workspace

        self.client = object.__new__(manual.ManualClient)
        c = self.client
        c._safety_stopped = False
        c._safety_backend_pid = 7
        c.safety_account_id = "a"
        c.permit_id = "permit"
        c.actor_id = "owner"
        c.safety_fingerprint = "fp"
        c.writing = False
        c.support_only = False
        c._safety_connection = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 7)), commit=AsyncMock()
        )
        self.permit = SimpleNamespace(
            account_id="a",
            target="owner",
            method="operator.manual_telegram",
            outcome="approved",
            created_at=manual.utcnow(),
        )
        self.account = SimpleNamespace(id="a", workspace_id="w", status="spam_blocked", restriction_status="limited")
        self.state = SimpleNamespace(
            reason="original_restriction",
            operation_id="original-operation",
            fingerprint="fp",
            wait_until=None,
            manual_hold=True,
            operator_approved_at=None,
            restriction_status="limited",
        )
        values = {
            TelegramOperation: self.permit,
            TelegramAccount: self.account,
            TelegramSafetyState: self.state,
            Workspace: SimpleNamespace(id="w", owner_user_id="owner"),
        }
        self.db = SimpleNamespace(
            add=MagicMock(),
            get=AsyncMock(side_effect=lambda model, key, **kwargs: values[model]),
            scalar=AsyncMock(return_value=None),
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=self.db)
        context.__aexit__ = AsyncMock(return_value=False)
        self.dbpatch = patch.object(manual, "AsyncSessionLocal", return_value=context)
        self.factory = self.dbpatch.start()
        self.factory.begin.return_value = context
        self.addCleanup(self.dbpatch.stop)
        self.settingspatch = patch.object(
            manual, "get_settings", return_value=SimpleNamespace(telegram_manual_workspaces="w")
        )
        self.settingspatch.start()
        self.addCleanup(self.settingspatch.stop)

    async def test_owner_can_read_held_account_without_releasing_hold(self):
        await self.client._check()
        self.assertTrue(self.state.manual_hold)

    async def test_general_send_stays_blocked_on_restricted_account(self):
        self.client.writing = True
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_verified_support_send_does_not_release_restriction(self):
        self.client.writing = True
        self.client.support_only = True
        await self.client._check()
        self.assertTrue(self.state.manual_hold)
        self.assertEqual(self.state.restriction_status, "limited")

    async def test_wrong_owner_denied(self):
        self.permit.target = "other"
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_expired_permit_denied(self):
        self.permit.created_at -= manual.timedelta(minutes=3)
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_original_flood_wait_kept(self):
        self.state.wait_until = manual.utcnow() + manual.timedelta(minutes=5)
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_unknown_delivery_blocks_support_too(self):
        self.client.support_only = True
        self.client.writing = True
        self.db.scalar.return_value = "pending-op"
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_changed_session_denied(self):
        self.state.fingerprint = "different"
        with self.assertRaises(TelegramGatewayError):
            await self.client._check()

    async def test_manual_read_transport_failure_preserves_original_diagnosis(self):
        await self.client._trip(ConnectionError("proxy refused"))
        self.assertEqual(self.state.reason, "original_restriction")
        self.assertEqual(self.state.operation_id, "original-operation")
        self.assertTrue(self.state.manual_hold)
        self.assertEqual(self.db.add.call_args.args[0].method, "operator.manual_read_failure")

    async def test_manual_write_failure_uses_existing_hold_policy(self):
        from unittest.mock import AsyncMock

        self.client.writing = True
        request = SimpleNamespace()
        with patch.object(manual.GuardedTelegramClient, "_trip", new_callable=AsyncMock) as trip:
            await self.client._trip(ConnectionError("uncertain write"), "op", request)
            trip.assert_awaited_once()
