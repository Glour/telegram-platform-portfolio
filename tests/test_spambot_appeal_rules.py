import importlib.util
import os
import unittest
from pathlib import Path

path = os.environ.get("APPEALS_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/appeals.py"
)
spec = importlib.util.spec_from_file_location("spambot_appeal_rules", path)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class AppealRulesTest(unittest.TestCase):
    def test_start_is_exact_command(self):
        self.assertEqual(rules.validate_step("start", "", None, True), "/start")
        with self.assertRaises(ValueError):
            rules.validate_step("start", "arbitrary", None, True)

    def test_confirmation_required_for_every_step(self):
        with self.assertRaises(ValueError):
            rules.validate_step("start", "", None, False)

    def test_button_must_come_from_bot(self):
        previous = {"status": "reply_received", "buttons": ["This is a mistake", "OK"]}
        self.assertEqual(rules.validate_step("button", "This is a mistake", previous, True), "This is a mistake")
        with self.assertRaises(ValueError):
            rules.validate_step("button", "fabricated option", previous, True)

    def test_cannot_skip_keyboard_to_explanation(self):
        with self.assertRaises(ValueError):
            rules.validate_step(
                "explanation", "Please review my account.", {"status": "reply_received", "buttons": ["Yes", "No"]}, True
            )

    def test_explanation_requires_received_reply(self):
        self.assertEqual(
            rules.validate_step(
                "explanation", "Please review my account.", {"status": "reply_received", "buttons": []}, True
            ),
            "Please review my account.",
        )
        with self.assertRaises(ValueError):
            rules.validate_step(
                "explanation", "Please review my account.", {"status": "unknown_delivery", "buttons": []}, True
            )


if __name__ == "__main__":
    unittest.main()
