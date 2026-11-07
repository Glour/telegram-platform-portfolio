from app.modules.telegram_gateway import service


class ChatWriteForbiddenError(Exception):
    pass


class FakeButton:
    def __init__(self, text: str, url: str | None = None) -> None:
        self.text = text
        self.url = url


class FakeMessage:
    def __init__(
        self,
        text: str,
        *,
        buttons: list[list[FakeButton]] | None = None,
        out: bool = False,
    ) -> None:
        self.message = text
        self.buttons = buttons or []
        self.out = out


def test_comment_write_forbidden_triggers_discussion_join_retry() -> None:
    error = ChatWriteForbiddenError("You can't write in this chat unless joining the discussion group")

    assert service._requires_discussion_join(error) is True


def test_unrelated_comment_error_does_not_trigger_discussion_join_retry() -> None:
    error = RuntimeError("Message id invalid")

    assert service._requires_discussion_join(error) is False


def test_numeric_source_ref_is_resolved_as_telegram_entity_id() -> None:
    assert service._normalize_source_ref("-5166123290") == -5166123290
    assert service._normalize_source_ref("5166123290") == 5166123290


def test_join_captcha_detects_safe_human_button() -> None:
    message = FakeMessage(
        "Подтвердите, что вы человек",
        buttons=[[FakeButton("Я человек"), FakeButton("Открыть сайт", url="https://example.com")]],
    )

    assert service._join_captcha_button_coords(message) == (0, 0, "Я человек")


def test_join_captcha_ignores_url_buttons() -> None:
    message = FakeMessage(
        "Подтвердите вход",
        buttons=[[FakeButton("Я человек", url="https://example.com")]],
    )

    assert service._join_captcha_button_coords(message) is None


def test_join_captcha_solves_simple_math() -> None:
    assert service._join_captcha_math_answer("Решите пример: 7 + 5") == "12"
    assert service._join_captcha_math_answer("Сколько будет 9×3?") == "27"
    assert service._join_captcha_math_answer("solve 8 / 2") == "4"


def test_join_captcha_skips_unsafe_math() -> None:
    assert service._join_captcha_math_answer("solve 8 / 3") is None
    assert service._join_captcha_math_answer("just hello") is None
