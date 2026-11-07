import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/campaign_diagnostics.py"
spec = importlib.util.spec_from_file_location("campaign_diagnostics_rules", path)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class CampaignSpamBotRulesTest(unittest.TestCase):
    def test_only_write_restrictions_trigger(self):
        self.assertTrue(rules.requires_spambot_check("SendMessageRequest", "UserBannedInChannelError"))
        self.assertTrue(rules.requires_spambot_check("SendMediaRequest", "PeerFloodError"))
        self.assertFalse(rules.requires_spambot_check("GetHistoryRequest", "UserBannedInChannelError"))
        self.assertFalse(rules.requires_spambot_check("SendMessageRequest", "FloodWaitError"))
        self.assertFalse(rules.requires_spambot_check("SendMessageRequest", "ChatAdminRequiredError"))

    def test_only_confirmed_restrictions_stop(self):
        for status in ["limited", "blocked"]:
            self.assertTrue(rules.confirmed_spambot_restriction(status))
        for status in ["clear", "unknown", "timeout", ""]:
            self.assertFalse(rules.confirmed_spambot_restriction(status))


if __name__ == "__main__":
    unittest.main()
