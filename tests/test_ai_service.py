from types import SimpleNamespace

import httpx
import pytest

from app.modules.ai.humanizer import (
    HUMANIZER_PROMPT_CACHE_KEY,
    build_comment_user_prompt,
    build_humanizer_system_prompt,
    enforce_humanizer_output,
)
from app.modules.ai.service import (
    _generate_with_openai,
    build_responses_url,
    generate_chat_reply_text,
    generate_comment_text,
)

AUTHOR_GENDER_PROMPT_EXPECTATIONS = [
    (
        "masculine",
        [
            "Авторский род: мужской.",
            "Пиши от лица автора-мужчины.",
            "Если нужна форма первого лица в прошедшем времени или краткое прилагательное, "
            "используй мужской род: написал, готов, уверен.",
        ],
    ),
    (
        "feminine",
        [
            "Авторский род: женский.",
            "Пиши от лица автора-женщины.",
            "Если нужна форма первого лица в прошедшем времени или краткое прилагательное, "
            "используй женский род: написала, готова, уверена.",
        ],
    ),
    (
        "neutral",
        [
            "Авторский род: нейтральный.",
            "Пиши нейтральным авторским голосом.",
            "Не раскрывай пол автора через формы вроде написал/написала, готов/готова, уверен/уверена.",
        ],
    ),
]


def test_responses_url_accepts_proxy_base_url() -> None:
    assert build_responses_url("http://openai-proxy:8787/v1/") == "http://openai-proxy:8787/v1/responses"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_effort"),
    [("gpt-6-luna", "low"), ("gpt-6-sol", "low"), ("gpt-6.1-sol", "low"), ("gpt-6-astra", "low")],
)
async def test_gpt6_request_uses_oauth_proxy_and_compatible_reasoning(
    monkeypatch: pytest.MonkeyPatch, model: str, expected_effort: str
) -> None:
    from app.modules.ai import service

    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        import json

        requests.append(json.loads(request.content))
        assert str(request.url) == "http://openai-proxy:8787/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"id": "resp-test", "output_text": "Готово", "usage": {}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            openai_base_url="http://openai-proxy:8787/v1",
            openai_proxy_bearer_token="test-token",
            openai_timeout_seconds=45,
            openai_prompt_cache_retention=None,
        ),
    )
    monkeypatch.setattr(
        service.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs)
    )

    output, usage = await _generate_with_openai(
        system_prompt="system", user_prompt="user", model=model, temperature=0.7, max_output_tokens=120
    )

    assert output == "Готово"
    assert usage["model"] == model
    assert requests[0]["reasoning"] == {"effort": expected_effort}
    assert requests[0]["max_output_tokens"] == 120
    assert "temperature" not in requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_model", "configured_model", "expected_model"),
    [
        ("gpt-5.4-mini", "gpt-6-luna", "gpt-6-luna"),
        ("gpt-4.1-mini", "gpt-6-luna", "gpt-6-luna"),
        ("gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna"),
        ("gpt-6-luna", "gpt-6-luna", "gpt-6-luna"),
        ("gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-luna"),
        ("gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-sol"),
        ("gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"),
        ("gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-sol"),
    ],
)
async def test_comment_profile_default_tracks_configured_model(
    monkeypatch: pytest.MonkeyPatch, profile_model: str, configured_model: str, expected_model: str
) -> None:
    from app.modules.ai import service

    async def load_profile(*args: object) -> SimpleNamespace:
        return SimpleNamespace(model=profile_model, system_prompt="", safety_prompt=None, temperature="0.7")

    async def load_examples(*args: object, **kwargs: object) -> list:
        return []

    captured: dict = {}

    async def generate(**kwargs: object) -> tuple[str, dict]:
        captured.update(kwargs)
        return "Точный ответ по теме.", {}

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(openai_model=configured_model))
    monkeypatch.setattr(service, "_load_profile", load_profile)
    monkeypatch.setattr(service, "load_style_examples", load_examples)
    monkeypatch.setattr(service, "_generate_with_openai", generate)
    prompt = SimpleNamespace(
        workspace_id="workspace", prompt_text="Ответь по теме", product_context=None, max_length=200
    )

    await generate_comment_text(None, prompt, "Текст поста", "Канал")

    assert captured["model"] == expected_model


def test_humanizer_foundation_is_stable_prefix() -> None:
    system_prompt = build_humanizer_system_prompt(profile_system_prompt="Пиши язвительно, но безопасно.")

    assert system_prompt.startswith("Humanizer RU + ru-text foundation.")
    assert len(system_prompt) > 6000
    assert "Пиши язвительно, но безопасно." in system_prompt
    assert HUMANIZER_PROMPT_CACHE_KEY == "demo-platform:humanizer-ru:v5:2026-06-12"


@pytest.mark.parametrize(("author_gender", "expected_phrases"), AUTHOR_GENDER_PROMPT_EXPECTATIONS)
def test_humanizer_prompts_include_author_gender_instructions(
    author_gender: str,
    expected_phrases: list[str],
) -> None:
    system_prompt = build_humanizer_system_prompt(author_gender=author_gender)
    user_prompt = build_comment_user_prompt(
        post_text="Я запустил кампанию и проверил ответы",
        prompt_text="Ответь живо и коротко",
        channel_title="Канал",
        product_context="Контекст",
        max_length=200,
        author_gender=author_gender,
    )

    combined_prompt = f"{system_prompt}\n{user_prompt}"
    for phrase in expected_phrases:
        assert phrase in combined_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("is_dialogue, expected_model", [(False, "gpt-6-luna"), (True, "gpt-6.1-sol")])
async def test_chat_and_dialogue_use_different_model_tiers(monkeypatch, is_dialogue, expected_model) -> None:
    from app.modules.ai import service

    captured = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        return "Отвечу по делу.", {}

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            openai_model="gpt-6-luna",
            openai_quality_model="gpt-6.1-sol",
        ),
    )
    monkeypatch.setattr(service, "_generate_with_openai", generate)
    await service.generate_chat_reply_text(
        None,
        workspace_id="workspace",
        prompt_text="Ответь",
        incoming_text="Привет",
        is_dialogue=is_dialogue,
    )
    assert captured["model"] == expected_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, expected_model, effort",
    [
        (None, "gpt-6-luna", "low"),
        ("gpt-6.1-sol", "gpt-6.1-sol", "medium"),
    ],
)
async def test_assistant_and_content_use_different_reasoning(monkeypatch, model, expected_model, effort) -> None:
    from app.modules.ai import service

    captured = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        return "Готово", {}

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            openai_model="gpt-6-luna",
            openai_quality_model="gpt-6.1-sol",
        ),
    )
    monkeypatch.setattr(service, "_generate_with_openai", generate)
    await service.generate_text_response(system_prompt="system", user_prompt="user", model=model)
    assert captured["model"] == expected_model
    assert captured["reasoning_effort"] == effort


@pytest.mark.asyncio
async def test_generate_chat_reply_text_passes_author_gender_into_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def fake_generate_with_openai(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return "Я ответила по делу.", {}

    monkeypatch.setattr("app.modules.ai.service._generate_with_openai", fake_generate_with_openai)

    await generate_chat_reply_text(
        None,
        workspace_id="workspace-id",
        prompt_text="Ответь как живой человек",
        incoming_text="Сможешь помочь с запуском?",
        chat_title="Диалог",
        tone="живой",
        max_length=180,
        author_gender="feminine",
    )

    combined_prompt = f"{captured['system_prompt']}\n{captured['user_prompt']}"
    assert "Авторский род: женский." in combined_prompt
    assert "Пиши от лица автора-женщины." in combined_prompt
    assert "строго соблюдай его имя, username и род" in combined_prompt
    assert "Не придумывай себе другое имя" in combined_prompt
    assert "Не упоминай tg.demo-platform" in combined_prompt


@pytest.mark.asyncio
async def test_generate_chat_reply_text_prompts_for_simple_dialog_greetings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_generate_with_openai(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return "Привет, нормально. Как у тебя?", {}

    monkeypatch.setattr("app.modules.ai.service._generate_with_openai", fake_generate_with_openai)

    await generate_chat_reply_text(
        None,
        workspace_id="workspace-id",
        prompt_text="Отвечай коротко и по контексту",
        incoming_text="Привет, как ты?",
        chat_title="Диалог",
        tone="доброжелательный",
        max_length=180,
        allow_skip=False,
    )

    combined_prompt = f"{captured['system_prompt']}\n{captured['user_prompt']}"
    assert "похоже на приветствие или простой small talk" in combined_prompt
    assert "не превращай короткое сообщение в комментарий к теме" in combined_prompt
    assert "Для короткого личного сообщения достаточно" in combined_prompt


@pytest.mark.asyncio
async def test_generate_chat_reply_text_can_prompt_without_follow_up_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_generate_with_openai(
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: object,
    ) -> tuple[str, dict]:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return "Можно смотреть demo-platform: там лиды, источники и AI-ответы собраны в один контур.", {}

    monkeypatch.setattr("app.modules.ai.service._generate_with_openai", fake_generate_with_openai)

    await generate_chat_reply_text(
        None,
        workspace_id="workspace-id",
        prompt_text=(
            "Это AI-чат в группе по ключевым словам. "
            "Не задавай вопросов и не пытайся развивать переписку. "
            "Сделай один короткий самостоятельный ответ в стиле мягкой непрямой рекламы."
        ),
        incoming_text="Интересна лидогенерация из Telegram",
        chat_title="Группа",
        tone="нативный",
        max_length=180,
    )

    combined_prompt = f"{captured['system_prompt']}\n{captured['user_prompt']}"
    assert "Не задавай вопросов" in combined_prompt
    assert "мягкой непрямой рекламы" in combined_prompt


def test_humanizer_cleanup_removes_forbidden_typography() -> None:
    output = enforce_humanizer_output("Конечно, «это» — тест “кавычек”", max_length=200)

    assert "Конечно" not in output
    assert "«" not in output
    assert "»" not in output
    assert "—" not in output
    assert "“" not in output
    assert "”" not in output


def test_comment_prompt_keeps_variable_post_outside_foundation() -> None:
    user_prompt = build_comment_user_prompt(
        post_text="Текст поста",
        prompt_text="Пиши коротко",
        channel_title="Канал",
        product_context="Контекст",
        max_length=200,
    )

    assert "Текст поста" in user_prompt
    assert "Пиши коротко" in user_prompt


def test_comment_prompt_sends_length_range_to_model() -> None:
    user_prompt = build_comment_user_prompt(
        post_text="Текст поста",
        prompt_text="Пиши коротко",
        channel_title="Канал",
        product_context="Контекст",
        max_length=100,
    )

    assert "Length range: 80-100 characters." in user_prompt
    assert "Do not rely on truncation." in user_prompt


def test_humanizer_cleanup_does_not_truncate_mid_sentence() -> None:
    text = "Это длинный комментарий, который раньше резался посередине фразы и превращался в бессмыслицу."

    output = enforce_humanizer_output(text, max_length=40)

    assert output == text
    assert "…" not in output
