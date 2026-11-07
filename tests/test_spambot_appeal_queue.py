import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

path = os.environ.get("APPEALS_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/appeals.py"
)
spec = importlib.util.spec_from_file_location("appeal_queue_candidate", path)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class QueueAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.modules.workspaces.models import Workspace

        self.account = SimpleNamespace(id="account", workspace_id="workspace", restriction_status="limited")
        self.state = SimpleNamespace(manual_hold=True)
        self.workspace = SimpleNamespace(owner_user_id="owner")
        self.db = SimpleNamespace(
            get=AsyncMock(),
            scalar=AsyncMock(return_value=None),
            scalars=AsyncMock(return_value=[]),
            add=Mock(),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        self.db.get.side_effect = lambda model, key, **kw: self.workspace if model is Workspace else self.state
        self.worker = patch.object(rules, "worker_available", AsyncMock(return_value=True))
        self.worker.start()
        self.addCleanup(self.worker.stop)

    async def test_non_owner_cannot_queue(self):
        with self.assertRaises(ValueError):
            await rules.queue_step(self.db, self.account, "other", "start", "", True, None)
        self.db.add.assert_not_called()

    async def test_unavailable_worker_does_not_accept_job(self):
        rules.worker_available.return_value = False
        with self.assertRaises(ValueError):
            await rules.queue_step(self.db, self.account, "owner", "start", "", True, None)
        self.db.add.assert_not_called()

    async def test_hold_is_not_created_by_queue(self):
        self.state.manual_hold = False
        with self.assertRaises(ValueError):
            await rules.queue_step(self.db, self.account, "owner", "start", "", True, None)
        self.assertFalse(self.state.manual_hold)
        self.db.add.assert_not_called()

    async def test_unknown_delivery_not_retried(self):
        self.db.scalar.return_value = SimpleNamespace(raw_error={"status": "unknown_delivery"})
        with self.assertRaises(ValueError):
            await rules.queue_step(self.db, self.account, "owner", "start", "", True, None)
        self.db.add.assert_not_called()

    async def test_owner_can_queue_one_confirmed_step(self):
        def add(event):
            event.id = "request"

        self.db.add.side_effect = add
        result = await rules.queue_step(self.db, self.account, "owner", "start", "", True, None)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["command"], "/start")
        self.assertTrue(self.state.manual_hold)
        self.db.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
