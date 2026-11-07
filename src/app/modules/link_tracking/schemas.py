from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator


class LinkInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    destination_url: str = Field(max_length=2048)
    placement: str = Field(default="", max_length=160)
    campaign_id: str | None = None
    account_id: str | None = None
    enabled: bool = True

    @field_validator("title")
    @classmethod
    def title_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Укажите название ссылки")
        return value.strip()

    @field_validator("destination_url")
    @classmethod
    def telegram_destination(cls, value: str) -> str:
        value = value.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
            raise ValueError("Некорректная ссылка")
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.netloc.lower() not in {"t.me", "telegram.me"}
            or not url.path.strip("/")
            or url.username
            or url.password
        ):
            raise ValueError("Нужна HTTPS-ссылка t.me на канал, чат или бота")
        return urlunsplit(("https", "t.me", url.path, url.query, url.fragment))
