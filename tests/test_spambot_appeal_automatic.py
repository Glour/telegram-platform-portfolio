import importlib.util
import os
import unittest
from pathlib import Path

path = os.environ.get("APPEALS_MODULE_PATH") or str(
    Path(__file__).resolve().parents[1] / "src/app/modules/telegram_safety/appeals.py"
)
spec = importlib.util.spec_from_file_location("appeal_auto_candidate", path)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class AutomaticAppealTest(unittest.TestCase):
    def test_exact_user_template(self):
        self.assertEqual(
            rules.AUTOMATIC_EXPLANATION,
            "Please review this account restriction.",
        )

    def test_initial_requires_confirmation(self):
        with self.assertRaises(ValueError):
            rules.validate_step("automatic", "", None, False)
        self.assertEqual(rules.validate_step("automatic", rules.AUTOMATIC_EXPLANATION, None, True), "/start")

    def test_changed_template_cannot_be_approved(self):
        with self.assertRaises(ValueError):
            rules.validate_step("automatic", "outdated text", None, True)

    def test_no_restrictions_no_appeal(self):
        self.assertIsNone(rules.automatic_next(0, {"classification": "clear"}))

    def test_no_free_text_over_keyboard(self):
        with self.assertRaises(ValueError):
            rules.automatic_next(3, {"reply": "Please explain", "buttons": ["Yes"]})

    def test_unexpected_question_with_yes_is_not_approved(self):
        with self.assertRaises(ValueError):
            rules.automatic_next(1, {"reply": "Would you like another service?", "buttons": ["Yes"]})

    def test_explanation_only_when_requested(self):
        with self.assertRaises(ValueError):
            rules.automatic_next(3, {"reply": "Wait for a decision", "buttons": []})

    def test_russian_buttons(self):
        self.assertEqual(rules.automatic_next(0, {"buttons": ["Это ошибка"]}), ("button", "Это ошибка"))
        self.assertEqual(
            rules.automatic_next(1, {"reply": "Хотите подать жалобу?", "buttons": ["Да"]}), ("button", "Да")
        )


if __name__ == "__main__":
    unittest.main()
