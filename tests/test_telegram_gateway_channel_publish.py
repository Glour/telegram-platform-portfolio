from contextlib import asynccontextmanager

import pytest
from app.modules.telegram_gateway.service import TelegramGatewayError, send_channel_post_with_account, utf16_length

from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_gateway import service as gateway_service


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_files: list[dict] = []
        self.sent_messages: list[dict] = []
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_entity(self, peer_ref: str) -> str:
        return peer_ref

    async def send_file(
        self,
        entity: str,
        image_path: str,
        caption: str | None = None,
        formatting_entities: list | None = None,
        **kwargs: object,
    ) -> FakeMessage:
        self.sent_files.append(
            {
                "entity": entity,
                "image_path": image_path,
                "caption": caption,
                "formatting_entities": formatting_entities,
                "kwargs": kwargs,
            }
        )
        return FakeMessage(11)

    async def send_message(
        self,
        entity: str,
        text: str,
        formatting_entities: list | None = None,
        **kwargs: object,
    ) -> FakeMessage:
        self.sent_messages.append(
            {
                "entity": entity,
                "text": text,
                "formatting_entities": formatting_entities,
                "kwargs": kwargs,
            }
        )
        return FakeMessage(12)


class FakeTelegramClientWithTargetError(FakeTelegramClient):
    async def get_entity(self, peer_ref: str) -> str:
        raise ValueError(f"Cannot find any entity corresponding to {peer_ref}")


def test_unknown_premium_emoji_gets_stable_alias() -> None:
    alias = gateway_service._premium_emoji_alias_for_document(
        "🫆",
        "5368324170671202299",
        set(),
        dict(gateway_service.PREMIUM_EMOJI_SEARCH_EMOJIS),
    )

    assert alias == "emoji_0671202299"


@pytest.mark.asyncio
async def test_send_channel_post_splits_long_image_caption(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeTelegramClient()

    @asynccontextmanager
    async def fake_client_context(*args: object, **kwargs: object):
        yield client

    monkeypatch.setattr(gateway_service, "telegram_client_for_account", fake_client_context)
    monkeypatch.setattr(gateway_service, "Message", FakeMessage)

    account = TelegramAccount(workspace_id="workspace", username="publisher", status="active")
    result = await send_channel_post_with_account(
        None,  # type: ignore[arg-type]
        account,
        "@target",
        "x" * 1200,
        image_path="/tmp/image.jpg",
    )

    assert result.tg_message_id == "12"
    assert result.raw["action"] == "send_file_then_message"
    assert result.raw["image_message_id"] == "11"
    assert client.sent_files[0]["caption"] is None
    assert client.sent_messages[0]["text"] == "x" * 1200
    assert client.sent_messages[0]["kwargs"]["link_preview"] is False


@pytest.mark.asyncio
async def test_send_channel_post_allows_longer_premium_image_caption(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeTelegramClient()

    @asynccontextmanager
    async def fake_client_context(*args: object, **kwargs: object):
        yield client

    monkeypatch.setattr(gateway_service, "telegram_client_for_account", fake_client_context)
    monkeypatch.setattr(gateway_service, "Message", FakeMessage)

    account = TelegramAccount(
        workspace_id="workspace",
        username="publisher",
        status="active",
        daily_limits={"profile": {"is_premium": True}},
    )
    result = await send_channel_post_with_account(
        None,  # type: ignore[arg-type]
        account,
        "@target",
        "x" * 1200,
        image_path="/tmp/image.jpg",
    )

    assert result.tg_message_id == "11"
    assert result.raw["action"] == "send_file"
    assert client.sent_files[0]["caption"] == "x" * 1200
    assert client.sent_files[0]["kwargs"]["link_preview"] is False
    assert client.sent_messages == []


@pytest.mark.asyncio
async def test_send_channel_post_passes_custom_emoji_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeTelegramClient()

    @asynccontextmanager
    async def fake_client_context(*args: object, **kwargs: object):
        yield client

    monkeypatch.setattr(gateway_service, "telegram_client_for_account", fake_client_context)
    monkeypatch.setattr(gateway_service, "Message", FakeMessage)

    account = TelegramAccount(workspace_id="workspace", username="publisher", status="active")
    result = await send_channel_post_with_account(
        None,  # type: ignore[arg-type]
        account,
        "@target",
        "💡 Идея поста",
        formatting_entities=[
            {
                "type": "custom_emoji",
                "offset": 0,
                "length": 2,
                "custom_emoji_id": "5368324170671202286",
            }
        ],
    )

    assert result.raw["custom_emoji_entities"] == 1
    assert client.sent_messages[0]["kwargs"]["link_preview"] is False
    assert client.sent_messages[0]["formatting_entities"]
    assert client.sent_messages[0]["formatting_entities"][0].document_id == 5368324170671202286


@pytest.mark.asyncio
async def test_send_channel_post_parses_markdown_with_custom_emoji(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeTelegramClient()

    @asynccontextmanager
    async def fake_client_context(*args: object, **kwargs: object):
        yield client

    monkeypatch.setattr(gateway_service, "telegram_client_for_account", fake_client_context)
    monkeypatch.setattr(gateway_service, "Message", FakeMessage)

    text = "🔥 **Прогрев — это этап**\n\nСмотри [платформу](https://demo.invalid)"
    account = TelegramAccount(workspace_id="workspace", username="publisher", status="active")
    result = await send_channel_post_with_account(
        None,  # type: ignore[arg-type]
        account,
        "@target",
        text,
        formatting_entities=[
            {
                "type": "custom_emoji",
                "offset": 0,
                "length": utf16_length("🔥"),
                "emoji": "🔥",
                "custom_emoji_id": "5368324170671202287",
            }
        ],
    )

    sent_text = client.sent_messages[0]["text"]
    entity_types = {type(entity).__name__ for entity in client.sent_messages[0]["formatting_entities"]}

    assert result.raw["custom_emoji_entities"] == 1
    assert sent_text == "🔥 Прогрев — это этап\n\nСмотри платформу"
    assert "**" not in sent_text
    assert "[платформу]" not in sent_text
    assert "MessageEntityBold" in entity_types
    assert "MessageEntityTextUrl" in entity_types
    assert "MessageEntityCustomEmoji" in entity_types


@pytest.mark.asyncio
async def test_send_channel_post_does_not_mark_operation_value_error_as_invalid_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTelegramClientWithTargetError()

    async def fake_build_client(*args: object, **kwargs: object) -> FakeTelegramClientWithTargetError:
        return client

    monkeypatch.setattr(gateway_service, "build_telegram_client_for_account", fake_build_client)

    account = TelegramAccount(workspace_id="workspace", username="publisher", status="active")
    with pytest.raises(TelegramGatewayError) as error:
        await send_channel_post_with_account(
            None,  # type: ignore[arg-type]
            account,
            "@missing_target",
            "text",
        )

    assert error.value.code == "TELEGRAM_OPERATION_VALUE_ERROR"
    assert error.value.code != "SESSION_INVALID"
    assert "missing_target" in str(error.value)
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_send_channel_post_marks_client_build_value_error_as_invalid_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_client(*args: object, **kwargs: object) -> FakeTelegramClient:
        raise ValueError("Not a valid string session")

    monkeypatch.setattr(gateway_service, "build_telegram_client_for_account", fake_build_client)

    account = TelegramAccount(workspace_id="workspace", username="publisher", status="active")
    with pytest.raises(TelegramGatewayError) as error:
        await send_channel_post_with_account(
            None,  # type: ignore[arg-type]
            account,
            "@target",
            "text",
        )

    assert error.value.code == "SESSION_INVALID"
