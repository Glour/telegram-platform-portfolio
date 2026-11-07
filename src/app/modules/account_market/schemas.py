from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

OfferType = Literal["account", "proxy", "bundle"]
OfferStatus = Literal["draft", "published", "reserved", "sold", "archived"]
MarketSort = Literal["recommended", "newest", "price_asc", "price_desc", "warmup_desc", "health_desc"]
RequestStatus = Literal["new", "payment_pending", "paid", "contacted", "reserved", "completed", "cancelled"]


class AccountMarketOfferCreate(BaseModel):
    offer_type: OfferType = "bundle"
    title: str = Field(min_length=2, max_length=180)
    description: str | None = Field(default=None, max_length=4000)
    account_id: str | None = None
    proxy_id: str | None = None
    price_rub: int = Field(ge=0, le=5_000_000)
    module_codes: list[str] = Field(default_factory=list, max_length=20)
    target_segments: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=30)
    warmup_profile: str | None = Field(default=None, max_length=80)
    warmup_days: int | None = Field(default=None, ge=0, le=365)
    warmup_score: int | None = Field(default=None, ge=0, le=100)
    health_score: int | None = Field(default=None, ge=0, le=100)
    quality_label: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=8)
    proxy_country: str | None = Field(default=None, max_length=8)
    proxy_class: str | None = Field(default=None, max_length=32)
    includes_proxy: bool = True
    ready_to_run: bool = True
    stock_count: int = Field(default=1, ge=0, le=10_000)
    sort_rank: int = Field(default=100, ge=0, le=10_000)
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("module_codes", "target_segments", "tags")
    @classmethod
    def normalize_string_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = " ".join(str(item or "").split()).strip(" ,;")
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text[:120])
        return cleaned


class AccountMarketOfferUpdate(BaseModel):
    offer_type: OfferType | None = None
    status: OfferStatus | None = None
    title: str | None = Field(default=None, min_length=2, max_length=180)
    description: str | None = Field(default=None, max_length=4000)
    account_id: str | None = None
    proxy_id: str | None = None
    price_rub: int | None = Field(default=None, ge=0, le=5_000_000)
    module_codes: list[str] | None = Field(default=None, max_length=20)
    target_segments: list[str] | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=30)
    warmup_profile: str | None = Field(default=None, max_length=80)
    warmup_days: int | None = Field(default=None, ge=0, le=365)
    warmup_score: int | None = Field(default=None, ge=0, le=100)
    health_score: int | None = Field(default=None, ge=0, le=100)
    quality_label: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=8)
    proxy_country: str | None = Field(default=None, max_length=8)
    proxy_class: str | None = Field(default=None, max_length=32)
    includes_proxy: bool | None = None
    ready_to_run: bool | None = None
    stock_count: int | None = Field(default=None, ge=0, le=10_000)
    sort_rank: int | None = Field(default=None, ge=0, le=10_000)
    metadata_json: dict | None = None

    @field_validator("module_codes", "target_segments", "tags")
    @classmethod
    def normalize_optional_string_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return AccountMarketOfferCreate.normalize_string_list(value)


class AccountMarketOfferResponse(BaseModel):
    id: str
    seller_workspace_id: str
    offer_type: str
    status: str
    title: str
    description: str | None
    account_id: str | None
    proxy_id: str | None
    account_label: str | None = None
    account_username: str | None = None
    proxy_label: str | None = None
    price_rub: int
    currency: str
    module_codes: list[str]
    target_segments: list[str]
    tags: list[str]
    warmup_profile: str | None
    warmup_days: int | None
    warmup_score: int | None
    health_score: int | None
    quality_label: str | None
    country: str | None
    proxy_country: str | None
    proxy_class: str | None
    includes_proxy: bool
    ready_to_run: bool
    stock_count: int
    sort_rank: int
    metadata_json: dict
    can_manage: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AccountMarketRequestCreate(BaseModel):
    contact: str | None = Field(default=None, max_length=255)
    message: str | None = Field(default=None, max_length=2000)
    quantity: int = Field(default=1, ge=1, le=100)
    module_code: str | None = Field(default=None, max_length=120)


class AccountMarketCheckoutCreate(AccountMarketRequestCreate):
    provider: str = Field(default="yookassa", pattern="^(yookassa|nowpayments)$")
    payment_method: str | None = Field(default=None, max_length=40)


class AccountMarketRequestUpdate(BaseModel):
    status: RequestStatus | None = None
    admin_note: str | None = Field(default=None, max_length=2000)


class AccountMarketRequestResponse(BaseModel):
    id: str
    workspace_id: str
    seller_workspace_id: str | None
    offer_id: str
    requested_by_user_id: str
    payment_order_id: str | None
    status: str
    contact: str | None
    message: str | None
    quantity: int
    module_code: str | None
    provider: str | None
    amount_rub: int | None
    admin_note: str | None
    snapshot: dict
    paid_at: datetime | None = None
    delivered_at: datetime | None = None
    delivery_metadata: dict = Field(default_factory=dict)
    offer: AccountMarketOfferResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
