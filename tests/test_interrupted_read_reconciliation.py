import importlib.util
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

path = os.environ.get("READ_RECONCILIATION_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/read_reconciliation.py"
)
spec = importlib.util.spec_from_file_location("read_reconciliation_candidate", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InterruptedReadTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.op = SimpleNamespace(
            method="ResolveUsernameRequest",
            outbound=False,
            outcome="pending",
            created_at=self.now - timedelta(hours=1),
            finished_at=None,
        )

    def test_old_lookup_is_reconcilable(self):
        module.validate_interrupted_lookup(self.op, self.now)

    def test_writes_never_reconciled(self):
        for method in ("SendMessageRequest", "SendMediaRequest", "JoinChannelRequest"):
            self.op.method = method
            with self.assertRaises(ValueError):
                module.validate_interrupted_lookup(self.op, self.now)

    def test_outbound_even_if_labelled_lookup_is_rejected(self):
        self.op.outbound = True
        with self.assertRaises(ValueError):
            module.validate_interrupted_lookup(self.op, self.now)

    def test_recent_lookup_is_rejected(self):
        self.op.created_at = self.now - timedelta(seconds=30)
        with self.assertRaises(ValueError):
            module.validate_interrupted_lookup(self.op, self.now)

    def test_unknown_delivery_is_not_reset(self):
        self.op.outcome = "unknown_delivery"
        with self.assertRaises(ValueError):
            module.validate_interrupted_lookup(self.op, self.now)

    def test_completed_lookup_is_not_reset(self):
        self.op.finished_at = self.now
        with self.assertRaises(ValueError):
            module.validate_interrupted_lookup(self.op, self.now)


if __name__ == "__main__":
    unittest.main()
