from pydantic import BaseModel, Field, model_validator


class BroadcastTiming(BaseModel):
    delay_min: int = Field(default=4, ge=4, le=3600)
    delay_max: int = Field(default=7, ge=4, le=3600)
    pause_every_min: int = Field(default=90, ge=30, le=3600)
    pause_every_max: int = Field(default=120, ge=30, le=3600)
    pause_min: int = Field(default=1, ge=1, le=3600)
    pause_max: int = Field(default=8, ge=1, le=3600)
    daily_limit: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def valid_ranges(self):
        for low, high in (
            (self.delay_min, self.delay_max),
            (self.pause_every_min, self.pause_every_max),
            (self.pause_min, self.pause_max),
        ):
            if low > high:
                raise ValueError("Минимальная задержка не может быть больше максимальной.")
        return self


class RecipientSelection(BaseModel):
    user_id: str = Field(pattern=r"^[1-9][0-9]*$")
    name: str = Field(default="", max_length=256)
    use_fallback: bool = False


class ContactBroadcastPreviewRequest(BaseModel):
    account_id: str
    contacts_snapshot_id: str
    media_id: str
    message_text: str = Field(min_length=1, max_length=4000)
    fallback_text: str = Field(default="", max_length=4000)
    recipients: list[RecipientSelection] = Field(min_length=1, max_length=10000)
    recipient_limit: int = Field(default=300, ge=1, le=10000)
    exclusions: str = Field(default="", max_length=200000)
    exclude_previous: bool = True
    timing: BroadcastTiming = Field(default_factory=BroadcastTiming)


class ContactBroadcastDraftRequest(BaseModel):
    preview_id: str
    name: str = Field(default="По сохранённым контактам", min_length=1, max_length=180)


class BroadcastSettingsRequest(BaseModel):
    timing: BroadcastTiming
    exclusions: str | None = Field(default=None, max_length=200000)
