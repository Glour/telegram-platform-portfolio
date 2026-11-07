from __future__ import annotations

import base64

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.ai.humanizer import (
    DEFAULT_AUTHOR_GENDER,
    HUMANIZER_PROMPT_CACHE_KEY,
    HUMANIZER_PROMPT_VERSION,
    PERSONAL_CHAT_STYLE,
    PERSONAL_CHAT_VERSION,
    PERSONAL_PROTECTED_RE,
    AuthorGender,
    author_gender_prompt_block,
    build_comment_user_prompt,
    build_humanizer_system_prompt,
    enforce_humanizer_output,
    humanizer_violations,
)
from app.modules.ai.models import AiGeneration, AiProfile, CommentPromptTemplate
from app.modules.datasets.service import load_style_examples


class AiProviderError(RuntimeError):
    pass


def build_responses_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/responses"


def build_images_generations_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/images/generations"


async def generate_comment_text(
    session: AsyncSession,
    prompt: CommentPromptTemplate,
    post_text: str,
    channel_title: str | None,
    source_id: str | None = None,
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER,
) -> tuple[str, dict]:
    profile = await _load_profile(session, prompt)
    settings = get_settings()
    style_mode = PERSONAL_CHAT_STYLE if getattr(profile, "tone", None) == PERSONAL_CHAT_STYLE else None
    style_examples = (
        []
        if style_mode == PERSONAL_CHAT_STYLE
        else await load_style_examples(session, prompt.workspace_id, source_id, limit=12)
    )
    system_prompt = build_humanizer_system_prompt(
        profile_system_prompt=profile.system_prompt if profile else None,
        safety_prompt=profile.safety_prompt if profile else None,
        author_gender=author_gender,
        style_mode=style_mode,
    )
    user_prompt = build_comment_user_prompt(
        post_text=post_text,
        prompt_text=prompt.prompt_text,
        channel_title=channel_title,
        product_context=prompt.product_context,
        max_length=prompt.max_length,
        style_examples=[(item.post_text, item.comment_text) for item in style_examples],
        author_gender=author_gender,
        style_mode=style_mode,
    )

    profile_model = profile.model if profile else None
    legacy_defaults = {
        "gpt-4.1-mini",
        "gpt-5.4-mini",
        "gpt-5.6-luna",
        "gpt-6-luna",
    }
    model = profile_model if profile_model and profile_model not in legacy_defaults else settings.openai_model
    if model in {"gpt-5.5", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-sol"}:
        model = getattr(settings, "openai_quality_model", "gpt-6.1-sol")
    temperature = float(profile.temperature) if profile else 0.7
    generated, usage = await _generate_with_openai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        temperature=temperature,
        max_output_tokens=max(80, min(600, prompt.max_length * 2)),
    )

    if not generated:
        raise AiProviderError("AI provider returned empty output")

    cleaned = enforce_humanizer_output(generated, prompt.max_length, style_mode=style_mode)
    violations = humanizer_violations(cleaned, style_mode=style_mode)
    if len(cleaned) > prompt.max_length:
        violations.append(f"length_over_max:{len(cleaned)}>{prompt.max_length}")
    if violations:
        repaired, repair_usage = await _repair_humanized_comment(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            bad_comment=cleaned,
            violations=violations,
            model=model,
            max_length=prompt.max_length,
        )
        if repaired:
            cleaned = enforce_humanizer_output(repaired, prompt.max_length, style_mode=style_mode)
            if style_mode == PERSONAL_CHAT_STYLE:
                protected = [m.group(0) for m in PERSONAL_PROTECTED_RE.finditer(generated)]
                if any(value not in cleaned for value in protected):
                    raise AiProviderError("HUMANIZER_PROTECTED_LITERAL_CHANGED")
            if len(cleaned) > prompt.max_length:
                usage = {
                    **usage,
                    "repair": repair_usage,
                    "repair_violations": violations,
                    "length_violation_after_repair": len(cleaned),
                }
            else:
                usage = {**usage, "repair": repair_usage, "repair_violations": violations}

    if style_mode == PERSONAL_CHAT_STYLE:
        if not cleaned or len(cleaned) > prompt.max_length or humanizer_violations(cleaned, style_mode=style_mode):
            raise AiProviderError("HUMANIZER_QUALITY_CHECK_FAILED")
        usage = {
            **usage,
            "prompt_version": PERSONAL_CHAT_VERSION,
            "humanizer_prompt_version": PERSONAL_CHAT_VERSION,
            "humanizer_style": style_mode,
        }
    return cleaned, usage


async def generate_chat_reply_text(
    session: AsyncSession,
    *,
    workspace_id: str,
    prompt_text: str,
    incoming_text: str,
    chat_title: str | None = None,
    context_text: str | None = None,
    tone: str = "живой",
    max_length: int = 280,
    allow_skip: bool = True,
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER,
    is_dialogue: bool = False,
) -> tuple[str, dict]:
    settings = get_settings()
    skip_rule = (
        "Если сообщение слабое, рекламное, токсичное или непонятное, верни ровно SKIP."
        if allow_skip
        else "На нормальное личное сообщение обязательно напиши короткий живой ответ. Не возвращай SKIP."
    )
    final_rule = (
        "Если отвечать не стоит, верни SKIP. Без кавычек, без вариантов, без пояснений."
        if allow_skip
        else "Верни только текст ответа. Не используй SKIP, кавычки и пояснения."
    )
    system_prompt = build_humanizer_system_prompt(
        profile_system_prompt=(
            "Ты пишешь короткие живые ответы в Telegram от лица реального пользователя. "
            "Не звучать как бот, не продавать в лоб, не использовать канцелярит. "
            "Отвечай уместно, цепляйся за конкретную мысль сообщения, не спорь ради спора. "
            "Если в контексте указан профиль Telegram-аккаунта, строго соблюдай его имя, username и род. "
            "Не придумывай себе другое имя, компанию, домен, должность или прошлое знакомство. "
            "Не упоминай tg.demo-platform, demo-platform TG, Tg.ai и другие старые брендовые названия. "
            "Если это личный диалог и входящее сообщение похоже на приветствие или простой small talk, "
            "ответь прямо как в личке: поздоровайся, коротко ответь на вопрос "
            "и задай один естественный встречный вопрос. "
            "Не выдумывай контекст, не превращай короткое сообщение в комментарий к теме, не добавляй большие выводы. "
            f"{skip_rule}"
        ),
        safety_prompt=(
            "Не генерируй угрозы, травлю, дискриминацию, доксинг, сексуальный контент, "
            "обман, финансовые обещания и агрессивный спам."
        ),
        author_gender=author_gender,
    )
    user_prompt = "\n".join(
        part
        for part in [
            f"Чат: {chat_title or 'Telegram'}",
            f"Тон: {tone}",
            f"Авторский род:\n{author_gender_prompt_block(author_gender)}",
            f"Задача: {prompt_text}",
            f"Контекст: {context_text}" if context_text else None,
            "Входящее сообщение:",
            incoming_text.strip(),
            (
                f"Напиши один ответ до {max_length} символов. "
                "Для короткого личного сообщения достаточно 1 короткой фразы или 1 вопроса. "
                "Если спрашивают кто ты, называй только имя/username из профиля аккаунта в контексте. "
                f"{final_rule}"
            ),
        ]
        if part
    )
    generated, usage = await _generate_with_openai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=settings.openai_quality_model if is_dialogue else settings.openai_model,
        temperature=0.75,
        max_output_tokens=max(80, min(700, max_length * 2)),
    )
    if not generated:
        raise AiProviderError("AI provider returned empty output")
    text = enforce_humanizer_output(generated, max_length)
    return text, usage


async def generate_text_response(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float = 0.55,
    max_output_tokens: int = 1200,
) -> tuple[str | None, dict]:
    settings = get_settings()
    selected_model = model or settings.openai_model
    return await _generate_with_openai(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=selected_model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        reasoning_effort="medium" if model and model == settings.openai_quality_model else "low",
    )


async def generate_profile_avatar_image(prompt: str) -> tuple[bytes, dict]:
    settings = get_settings()
    bearer_token = settings.openai_proxy_bearer_token
    if not bearer_token:
        raise AiProviderError("AI_PROVIDER_NOT_CONFIGURED: set OPENAI_BASE_URL with OPENAI_PROXY_BEARER_TOKEN")

    payload: dict = {
        "model": settings.openai_image_model,
        "prompt": prompt,
        "n": 1,
        "size": "512x512",
        "output_format": "jpeg",
    }
    image_timeout_seconds = max(settings.openai_timeout_seconds, 180)

    try:
        async with httpx.AsyncClient(timeout=image_timeout_seconds) as client:
            response = await client.post(
                build_images_generations_url(settings.openai_base_url),
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:500] if exc.response is not None else ""
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise AiProviderError(f"AI_IMAGE_PROVIDER_HTTP_ERROR: {status_code} {response_text}") from exc
    except httpx.HTTPError as exc:
        raise AiProviderError(f"AI_IMAGE_PROVIDER_REQUEST_ERROR: {exc}") from exc

    data = response.json()
    item = (data.get("data") or [None])[0]
    if not isinstance(item, dict):
        raise AiProviderError("AI image provider returned no image data")

    image_bytes: bytes | None = None
    if item.get("b64_json"):
        try:
            image_bytes = base64.b64decode(str(item["b64_json"]))
        except ValueError as exc:
            raise AiProviderError("AI image provider returned invalid base64 image") from exc
    elif item.get("url"):
        try:
            async with httpx.AsyncClient(timeout=image_timeout_seconds) as client:
                image_response = await client.get(str(item["url"]))
                image_response.raise_for_status()
                image_bytes = image_response.content
        except httpx.HTTPError as exc:
            raise AiProviderError(f"AI_IMAGE_DOWNLOAD_ERROR: {exc}") from exc

    if not image_bytes:
        raise AiProviderError("AI image provider returned empty image")

    return image_bytes, {
        "mode": "openai_images",
        "base_url": settings.openai_base_url.rstrip("/"),
        "model": settings.openai_image_model,
        "output_format": payload["output_format"],
        "size": payload["size"],
        "usage": data.get("usage"),
        "created": data.get("created"),
        "proxy": data.get("_proxy"),
    }


async def _load_profile(session: AsyncSession, prompt: CommentPromptTemplate) -> AiProfile | None:
    if not prompt.ai_profile_id:
        return None
    profile = await session.get(AiProfile, prompt.ai_profile_id)
    if not profile or profile.workspace_id != prompt.workspace_id:
        return None
    return profile


async def _generate_with_openai(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    max_output_tokens: int,
    reasoning_effort: str = "low",
) -> tuple[str | None, dict]:
    settings = get_settings()
    bearer_token = settings.openai_proxy_bearer_token
    if not bearer_token:
        raise AiProviderError("AI_PROVIDER_NOT_CONFIGURED: set OPENAI_BASE_URL with OPENAI_PROXY_BEARER_TOKEN")

    payload: dict = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": max_output_tokens,
        "prompt_cache_key": HUMANIZER_PROMPT_CACHE_KEY,
    }
    is_gpt6 = model.startswith(("gpt-6-", "gpt-6.1-"))
    if not is_gpt6:
        payload["temperature"] = temperature
    else:
        payload["reasoning"] = {"effort": reasoning_effort}
    if settings.openai_prompt_cache_retention and not is_gpt6:
        payload["prompt_cache_retention"] = settings.openai_prompt_cache_retention

    try:
        async with httpx.AsyncClient(timeout=settings.openai_timeout_seconds) as client:
            response = await client.post(
                build_responses_url(settings.openai_base_url),
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:500] if exc.response is not None else ""
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise AiProviderError(f"AI_PROVIDER_HTTP_ERROR: {status_code} {response_text}") from exc
    except httpx.HTTPError as exc:
        raise AiProviderError(f"AI_PROVIDER_REQUEST_ERROR: {exc}") from exc

    data = response.json()
    output = _extract_response_text(data)
    return output, {
        "mode": "openai_responses",
        "prompt_version": HUMANIZER_PROMPT_VERSION,
        "prompt_cache_key": HUMANIZER_PROMPT_CACHE_KEY,
        "base_url": settings.openai_base_url.rstrip("/"),
        "model": model,
        "reasoning_effort": reasoning_effort if is_gpt6 else None,
        "usage": data.get("usage"),
        "response_id": data.get("id"),
    }


async def _repair_humanized_comment(
    *,
    system_prompt: str,
    user_prompt: str,
    bad_comment: str,
    violations: list[str],
    model: str,
    max_length: int,
) -> tuple[str | None, dict]:
    repair_prompt = "\n".join(
        [
            user_prompt,
            "",
            "The previous answer violated hard humanizer rules.",
            f"Violations: {', '.join(violations)}",
            "Bad comment:",
            bad_comment,
            "",
            "Rewrite it once. Keep the same post context, but remove all violations.",
            f"Length rule: final comment must be a complete thought within {max_length} characters.",
            "If the old thought is too long, choose a shorter complete thought. Do not cut a sentence.",
            "Hard rules: no quotes, no 'не X, а Y' contrast template, no generic model starter, no explanations.",
            "Return only one final Russian Telegram comment.",
        ]
    )
    generated, usage = await _generate_with_openai(
        system_prompt=system_prompt,
        user_prompt=repair_prompt,
        model=model,
        temperature=0.35,
        max_output_tokens=max(80, min(600, max_length * 2)),
    )
    return generated, usage


def _extract_response_text(data: dict) -> str | None:
    if data.get("output_text"):
        return str(data["output_text"]).strip()
    chunks: list[str] = []
    for output_item in data.get("output", []) or []:
        for content in output_item.get("content", []) or []:
            text = content.get("text")
            if text:
                chunks.append(str(text))
    return "\n".join(chunks).strip() or None


async def create_generation(
    session: AsyncSession,
    workspace_id: str,
    prompt: CommentPromptTemplate,
    post_text: str,
    channel_title: str | None,
    profile_id: str | None = None,
    source_id: str | None = None,
) -> AiGeneration:
    output, token_usage = await generate_comment_text(session, prompt, post_text, channel_title, source_id)
    lowered = output.lower()
    blocked = [word for word in prompt.blacklist_words if word.lower() in lowered]
    generation = AiGeneration(
        workspace_id=workspace_id,
        profile_id=profile_id or prompt.ai_profile_id,
        comment_prompt_template_id=prompt.id,
        input={
            "post_text": post_text,
            "channel_title": channel_title,
            "prompt_version": prompt.version,
            "humanizer_prompt_version": HUMANIZER_PROMPT_VERSION,
        },
        output_text=output,
        moderation_status="blocked" if blocked else "passed",
        token_usage=token_usage,
    )
    session.add(generation)
    await session.commit()
    await session.refresh(generation)
    return generation
