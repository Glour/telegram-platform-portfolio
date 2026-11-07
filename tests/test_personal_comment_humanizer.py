from types import SimpleNamespace

import pytest

from app.modules.ai import service
from app.modules.ai.humanizer import (
    PERSONAL_CHAT_STYLE,
    PERSONAL_CHAT_VERSION,
    build_comment_user_prompt,
    build_humanizer_system_prompt,
    enforce_humanizer_output,
    humanizer_violations,
)


def test_personal_style_preserves_critical_literals_and_does_not_add_fillers():
    text = "Проверка Alipay: 1 300,50 руб, @China_Name, User@Example.com и `GET /API`. https://Example.com/a?q=One&x=2"
    output = enforce_humanizer_output(text, style_mode=PERSONAL_CHAT_STYLE)
    for literal in [
        "Alipay",
        "1 300,50",
        "@China_Name",
        "User@Example.com",
        "`GET /API`",
        "https://Example.com/a?q=One&x=2",
    ]:
        assert literal in output
    assert output.startswith("проверка Alipay")
    assert humanizer_violations(output, style_mode=PERSONAL_CHAT_STYLE) == []
    assert enforce_humanizer_output(output, style_mode=PERSONAL_CHAT_STYLE) == output


@pytest.mark.parametrize(
    "text", ["Ну, это долго.", "Короче, решили.", "В общем, понятно.", "Как бы работает.", "По сути все так."]
)
def test_personal_fillers_are_detected(text):
    assert "filler_words" in humanizer_violations(text, style_mode=PERSONAL_CHAT_STYLE)


@pytest.mark.parametrize(
    "text", ["Интересно, сколько это занимает?", "Любопытно, что будет дальше?", "Груз стоит. Интересно, как долго?"]
)
def test_personal_generic_starters_are_detected(text):
    assert "generic_starter" in humanizer_violations(text, style_mode=PERSONAL_CHAT_STYLE)


def test_personal_prompt_is_opt_in_and_does_not_force_padding():
    assert "без слов-паразитов" in build_humanizer_system_prompt(style_mode=PERSONAL_CHAT_STYLE)
    assert build_humanizer_system_prompt().startswith("Humanizer RU + ru-text foundation.")
    prompt = build_comment_user_prompt(
        post_text="Пост",
        prompt_text="Ответ",
        channel_title="Канал",
        product_context=None,
        max_length=220,
        style_mode=PERSONAL_CHAT_STYLE,
    )
    assert "No minimum" in prompt
    assert "Length range: 154-220" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", ["Сроки заметно сократились.", "Ну, сроки сократились."])
async def test_personal_generator_repairs_fillers_or_fails_closed(monkeypatch, repair):
    async def profile(*args):
        return SimpleNamespace(
            tone=PERSONAL_CHAT_STYLE, model="gpt-6-luna", temperature="0.7", system_prompt="", safety_prompt=""
        )

    async def examples(*args, **kwargs):
        return []

    responses = iter(["Ну, сроки сократились.", repair])

    async def generate(**kwargs):
        return next(responses), {}

    monkeypatch.setattr(service, "_load_profile", profile)
    monkeypatch.setattr(service, "load_style_examples", examples)
    monkeypatch.setattr(service, "_generate_with_openai", generate)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(openai_model="gpt-6-luna"))
    prompt = SimpleNamespace(workspace_id="test", prompt_text="По теме", product_context=None, max_length=220)
    if repair.startswith("Ну"):
        with pytest.raises(service.AiProviderError, match="HUMANIZER_QUALITY_CHECK_FAILED"):
            await service.generate_comment_text(None, prompt, "Пост", "Канал")
    else:
        text, usage = await service.generate_comment_text(None, prompt, "Пост", "Канал")
        assert text == "сроки заметно сократились"
        assert usage["humanizer_prompt_version"] == PERSONAL_CHAT_VERSION


def test_aggressive_texture_is_sparse_stable_and_protects_literals():
    outputs = []
    for number in range(60):
        text = f"Например, получается доставка {number}. https://Example.com/например. @Some_Name"
        output = enforce_humanizer_output(text, style_mode=PERSONAL_CHAT_STYLE)
        assert "https://Example.com/например." in output
        assert "@Some_Name" in output
        assert str(number) in output
        assert enforce_humanizer_output(output, style_mode=PERSONAL_CHAT_STYLE) == output
        assert sum(output.count(word) for word in ["напрмер", "получаеться"]) <= 1
        outputs.append(output)
    changed = sum("напрмер" in text or "получаеться" in text for text in outputs)
    assert 0 < changed < len(outputs) // 2


def test_aggressive_style_does_not_change_default_or_url_final_period():
    assert enforce_humanizer_output("Например, работает.") == "Например, работает."
    assert enforce_humanizer_output("Работает.", style_mode=PERSONAL_CHAT_STYLE) == "работает"
    assert (
        enforce_humanizer_output("https://Example.com/file.", style_mode=PERSONAL_CHAT_STYLE)
        == "https://Example.com/file."
    )
    assert (
        enforce_humanizer_output("Love Republic и POCO X8", style_mode=PERSONAL_CHAT_STYLE) == "Love Republic и POCO X8"
    )
