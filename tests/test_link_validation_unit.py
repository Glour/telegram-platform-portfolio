"""Isolated link validation and request classification tests."""

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.modules.link_tracking.schemas import LinkInput
from app.modules.link_tracking.service import classify_request


@pytest.mark.parametrize(
    "url",
    [
        "http://t.me/channel",
        "https://t.me.evil.test/channel",
        "https://t.me@evil.test/channel",
        "https://t.me:443/channel",
        "https://t.me/",
        "https://t.me/channel\r\nInjected: header",
    ],
)
def test_destination_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        LinkInput(title="Sample", destination_url=url)


def test_destination_normalizes_supported_host() -> None:
    link = LinkInput(title=" Sample ", destination_url="https://telegram.me/channel?start=demo")
    assert link.title == "Sample"
    assert link.destination_url == "https://t.me/channel?start=demo"


@pytest.mark.parametrize(
    ("method", "headers", "expected"),
    [
        ("GET", [(b"user-agent", b"Mozilla/5.0")], "transition"),
        ("GET", [(b"user-agent", b"TelegramBot")], "technical"),
        ("GET", [], "technical"),
        ("HEAD", [(b"user-agent", b"Mozilla/5.0")], "head"),
        ("GET", [(b"sec-purpose", b"prefetch")], "prefetch"),
    ],
)
def test_request_classification(method: str, headers: list[tuple[bytes, bytes]], expected: str) -> None:
    request = Request({"type": "http", "method": method, "headers": headers})
    assert classify_request(request) == expected
