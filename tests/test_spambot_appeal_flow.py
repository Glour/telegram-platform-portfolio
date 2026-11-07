import importlib.util
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

path = os.environ.get("APPEALS_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/appeals.py"
)
spec = importlib.util.spec_from_file_location("appeal_flow_candidate", path)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class MemorySession:
    def __init__(self):
        self.events, self.operations = [], []
        self.account = SimpleNamespace(
            id="account", workspace_id="workspace", restriction_status="limited", proxy_required=False, proxy_id=None
        )
        self.state = SimpleNamespace(manual_hold=True)
        self.workspace = SimpleNamespace(owner_user_id="owner")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, key, **kwargs):
        return {"TelegramAccount": self.account, "Workspace": self.workspace, "TelegramSafetyState": self.state}[
            model.__name__
        ]

    async def scalar(self, query):
        return self.events[-1] if self.events else None

    async def scalars(self, query):
        if query._for_update_arg is not None:
            return [e for e in self.events if e.raw_error["status"] == "queued"][:1]
        return list(self.events)

    def add(self, row):
        if not row.id:
            row.id = str(uuid4())
        if not row.created_at:
            row.created_at = datetime.now(UTC)
        (self.events if type(row).__name__ == "AccountHealthEvent" else self.operations).append(row)

    async def commit(self):
        pass

    async def refresh(self, row):
        pass


class AppealFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MemorySession()
        self.commands = []
        self.timeout = False
        self.message_id = 100
        self.reply = None
        outer = self

        class Client:
            def __init__(self, *args, appeal_command, **kwargs):
                self.command = appeal_command

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def get_entity(self, username):
                assert username == "SpamBot"
                return SimpleNamespace(id=1, bot=True, verified=True, username="SpamBot")

            async def get_input_entity(self, bot):
                from telethon.tl.types import InputPeerUser

                return InputPeerUser(1, 2)

            async def send_message(self, bot, command):
                from telethon.tl.types import KeyboardButton

                assert bot.id == 1 and command == self.command
                outer.commands.append(command)
                if outer.timeout:
                    raise TimeoutError("Transport result unknown")
                outer.message_id += 2
                buttons = [KeyboardButton("This is a mistake")] if command == "/start" else []
                message = "Please explain." if command == "This is a mistake" else "Reply received."
                if getattr(outer, "automatic", False):
                    steps = {
                        "/start": ("Account limited.", ["This is a mistake"]),
                        "This is a mistake": ("Do you want to submit a complaint?", ["Yes"]),
                        "Yes": ("Have you sent unwanted spam?", ["No, I'll never do any of this!"]),
                        "No, I'll never do any of this!": ("Please write us some details.", []),
                        rules.AUTOMATIC_EXPLANATION: ("Your complaint has been submitted.", []),
                    }
                    message, names = steps[command]
                    if getattr(outer, "unexpected", False) and command == "This is a mistake":
                        names = ["Accept paid service"]
                    buttons = [KeyboardButton(name) for name in names]
                outer.reply = SimpleNamespace(
                    id=outer.message_id + 1,
                    out=False,
                    message=message,
                    from_id=SimpleNamespace(user_id=1),
                    reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=buttons)]),
                )
                return SimpleNamespace(id=outer.message_id)

            async def __call__(self, request):
                return SimpleNamespace(messages=[outer.reply])

        patches = [
            patch.object(rules, "worker_available", AsyncMock(return_value=True)),
            patch("app.db.session.AsyncSessionLocal", lambda: self.db),
            patch("app.modules.telegram_gateway.service.load_account_session", AsyncMock(return_value="")),
            patch("app.modules.telegram_gateway.service.load_proxy", AsyncMock(return_value=None)),
            patch("app.modules.telegram_safety.spambot.SpamBotDiagnosticClient", Client),
            patch("asyncio.sleep", AsyncMock()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_three_owner_confirmed_steps_without_auto_advance(self):
        first = await rules.queue_step(self.db, self.db.account, "owner", "start", "", True, None)
        self.assertEqual(self.commands, [])
        await rules.process_pending_appeals("workspace")
        self.assertEqual(self.commands, ["/start"])
        self.assertEqual(self.db.events[-1].raw_error["buttons"], ["This is a mistake"])
        second = await rules.queue_step(
            self.db, self.db.account, "owner", "button", "This is a mistake", True, first["id"]
        )
        await rules.process_pending_appeals("workspace")
        self.assertEqual(self.commands, ["/start", "This is a mistake"])
        await rules.queue_step(
            self.db,
            self.db.account,
            "owner",
            "explanation",
            "Please review the account restriction.",
            True,
            second["id"],
        )
        await rules.process_pending_appeals("workspace")
        await rules.process_pending_appeals("workspace")
        self.assertEqual(len(self.commands), 3)
        self.assertTrue(self.db.state.manual_hold)
        self.assertEqual(self.db.account.restriction_status, "limited")

    async def test_unknown_delivery_is_never_replayed(self):
        self.timeout = True
        await rules.queue_step(self.db, self.db.account, "owner", "start", "", True, None)
        await rules.process_pending_appeals("workspace")
        await rules.process_pending_appeals("workspace")
        self.assertEqual(self.commands, ["/start"])
        self.assertEqual(self.db.events[-1].raw_error["status"], "unknown_delivery")
        with self.assertRaises(ValueError):
            await rules.queue_step(self.db, self.db.account, "owner", "start", "", True, None)

    async def test_automatic_flow_exact_template_once_and_hold_preserved(self):
        self.automatic = True
        await rules.queue_step(self.db, self.db.account, "owner", "automatic", rules.AUTOMATIC_EXPLANATION, True, None)
        self.assertEqual(self.commands, [])
        for _ in range(7):
            await rules.process_pending_appeals("workspace")
        self.assertEqual(
            self.commands,
            ["/start", "This is a mistake", "Yes", "No, I'll never do any of this!", rules.AUTOMATIC_EXPLANATION],
        )
        self.assertEqual(self.db.events[-1].raw_error["status"], "flow_completed")
        self.assertTrue(self.db.state.manual_hold)
        self.assertEqual(self.db.account.restriction_status, "limited")

    async def test_automatic_stops_on_unknown_keyboard(self):
        self.automatic = self.unexpected = True
        await rules.queue_step(self.db, self.db.account, "owner", "automatic", rules.AUTOMATIC_EXPLANATION, True, None)
        for _ in range(5):
            await rules.process_pending_appeals("workspace")
        self.assertEqual(self.commands, ["/start", "This is a mistake"])
        self.assertEqual(self.db.events[-1].raw_error["status"], "automation_stopped")


if __name__ == "__main__":
    unittest.main()
