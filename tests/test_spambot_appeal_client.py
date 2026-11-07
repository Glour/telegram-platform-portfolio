import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import InputPeerUser

from app.modules.telegram_gateway.contracts import TelegramGatewayError

path = os.environ.get("SPAMBOT_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/spambot.py"
)
spec = importlib.util.spec_from_file_location("spambot_appeal_client_candidate", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AppealClientTest(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(module.SpamBotDiagnosticClient)
        self.client.bot_id = 1
        self.client.allowed_command = "This is a mistake"
        self.client.rpc_caps = module.CAPS
        self.client.start_message_id = 2

    def test_exact_approved_step(self):
        request = SendMessageRequest(peer=InputPeerUser(1, 2), message="This is a mistake")
        self.assertEqual(self.client._validate_request(request), "SendMessageRequest")

    def test_feature_flag_does_not_enable_general_diagnostics(self):
        settings = SimpleNamespace(telegram_operations_enabled=False, spambot_appeals_enabled=True)
        self.assertTrue(module.diagnostic_execution_allowed(settings, module.PERMIT_METHOD + ".appeal"))
        self.assertFalse(module.diagnostic_execution_allowed(settings, module.PERMIT_METHOD))
        settings.spambot_appeals_enabled = False
        self.assertFalse(module.diagnostic_execution_allowed(settings, module.PERMIT_METHOD + ".appeal"))

    def test_different_command_rejected(self):
        request = SendMessageRequest(peer=InputPeerUser(1, 2), message="Other text")
        with self.assertRaises(TelegramGatewayError):
            self.client._validate_request(request)

    def test_different_recipient_rejected(self):
        request = SendMessageRequest(peer=InputPeerUser(3, 2), message="This is a mistake")
        with self.assertRaises(TelegramGatewayError):
            self.client._validate_request(request)


if __name__ == "__main__":
    unittest.main()
