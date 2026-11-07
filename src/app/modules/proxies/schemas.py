from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PROXY_CLASS_PATTERN = "^(datacenter|static_residential|isp|mobile|rotating_residential|unknown)$"
PROXY_EXCLUSIVITY_PATTERN = "^(dedicated|private_1_2|shared|unknown)$"


class ProxyCreate(BaseModel):
    type: str = Field(pattern="^(http|socks5)$")
    host: str
    port: int = Field(ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    country: str | None = None
    provider: str | None = None
    city_state: str | None = None
    proxy_class: str | None = Field(default=None, pattern=PROXY_CLASS_PATTERN)
    exclusivity: str | None = Field(default=None, pattern=PROXY_EXCLUSIVITY_PATTERN)
    sticky_until: datetime | None = None
    max_accounts: int = Field(default=1, ge=1, le=100)
    note: str | None = None


class ProxyUpdate(BaseModel):
    type: str | None = Field(default=None, pattern="^(http|socks5)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    country: str | None = None
    provider: str | None = None
    city_state: str | None = None
    proxy_class: str | None = Field(default=None, pattern=PROXY_CLASS_PATTERN)
    exclusivity: str | None = Field(default=None, pattern=PROXY_EXCLUSIVITY_PATTERN)
    sticky_until: datetime | None = None
    max_accounts: int | None = Field(default=None, ge=1, le=100)
    note: str | None = None

    @field_validator("type", "host", "port", "proxy_class", "exclusivity", "max_accounts")
    @classmethod
    def required_patch_value_must_not_be_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field may not be null")
        return value


class ProxyResponse(BaseModel):
    id: str
    workspace_id: str
    type: str
    host: str
    port: int
    status: str
    country: str | None
    provider: str | None
    city_state: str | None = None
    proxy_class: str | None = None
    exclusivity: str | None = None
    sticky_until: datetime | None = None
    last_ip_detected: str | None = None
    last_country_detected: str | None = None
    telegram_connectivity_status: str | None = None
    max_accounts: int
    note: str | None
    account_count: int = 0
    latest_latency_ms: int | None = None
    latest_telegram_latency_ms: int | None = None
    latest_checked_at: datetime | None = None

    model_config = {"from_attributes": True}


class ProxyCheckResponse(BaseModel):
    id: str
    proxy_id: str
    status: str
    latency_ms: int | None = None
    ip_detected: str | None = None
    telegram_ok: bool | None = None
    telegram_latency_ms: int | None = None
    geo_country: str | None = None
    error_code: str | None
    error_message: str | None

    model_config = {"from_attributes": True}


class ProxyBulkCheckRequest(BaseModel):
    raw_text: str = Field(default="", max_length=200_000)
    default_type: str = Field(default="socks5", pattern="^(http|socks5)$")
    provider: str | None = None
    proxy_class: str | None = Field(default=None, pattern=PROXY_CLASS_PATTERN)
    exclusivity: str | None = Field(default=None, pattern=PROXY_EXCLUSIVITY_PATTERN)
    max_accounts: int = Field(default=1, ge=1, le=100)
    note: str | None = None
    save_valid: bool = False
    check_telegram: bool = True


class ProxyParsedItem(BaseModel):
    raw: str
    type: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    country: str | None = None
    status: str
    latency_ms: int | None = None
    telegram_ok: bool | None = None
    telegram_latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    proxy_id: str | None = None


class ProxyBulkCheckResponse(BaseModel):
    total: int
    parsed: int
    valid: int
    saved: int
    items: list[ProxyParsedItem]


class ProxyBulkActionRequest(BaseModel):
    proxy_ids: list[str] = Field(min_length=1, max_length=500)


class ProxyBulkActionResponse(BaseModel):
    status: str
    action: str
    proxy_count: int
    target_count: int


class ProxyAssignAccount(BaseModel):
    telegram_account_id: str


class ProxyAssignCampaign(BaseModel):
    campaign_id: str


class ProxyUsageAccount(BaseModel):
    id: str
    username: str | None
    display_name: str | None
    status: str


class ProxyUsageCampaign(BaseModel):
    id: str
    name: str
    status: str


class ProxyUsageResponse(BaseModel):
    proxy_id: str
    accounts: list[ProxyUsageAccount]
    campaigns: list[ProxyUsageCampaign]
    account_count: int
    campaign_count: int
