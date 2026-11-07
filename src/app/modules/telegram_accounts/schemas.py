from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.account_trust.schemas import AccountTrustSnapshotResponse

AccountOrigin = Literal["manual_phone_registered", "market_session", "tdata", "sms_service_manual"]


class TelegramAccountImport(BaseModel):
    session_blob: str = Field(description="Base64 or raw Telethon session string for MVP API testing")
    phone: str | None = None
    username: str | None = None
    display_name: str | None = None
    proxy_id: str | None = None
    new_proxy_raw: str | None = None
    new_proxy_max_accounts: int = Field(default=1, ge=1, le=100)
    proxy_required: bool = True
    warmup_status: str | None = Field(default=None, max_length=32)
    warmup_score: int | None = Field(default=None, ge=0, le=100)


class TelegramTdataImportMeta(BaseModel):
    phone: str | None = None
    username: str | None = None
    display_name: str | None = None
    proxy_id: str | None = None
    new_proxy_raw: str | None = None
    new_proxy_max_accounts: int = Field(default=1, ge=1, le=100)
    proxy_required: bool = True
    password: str | None = None
    warmup_status: str | None = Field(default=None, max_length=32)
    warmup_score: int | None = Field(default=None, ge=0, le=100)


class TelegramSessionFileImportMeta(BaseModel):
    phone: str | None = None
    username: str | None = None
    display_name: str | None = None
    proxy_id: str | None = None
    new_proxy_raw: str | None = None
    new_proxy_max_accounts: int = Field(default=1, ge=1, le=100)
    proxy_required: bool = True
    warmup_status: str | None = Field(default=None, max_length=32)
    warmup_score: int | None = Field(default=None, ge=0, le=100)


class TelegramSessionArchiveImportItem(BaseModel):
    file_name: str
    status: str
    account: TelegramAccountResponse | None = None
    error: str | None = None


class TelegramSessionArchiveImportResponse(BaseModel):
    imported: int
    skipped: int = 0
    failed: int
    items: list[TelegramSessionArchiveImportItem]


class TelegramAccountResponse(BaseModel):
    id: str
    workspace_id: str
    username: str | None
    display_name: str | None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    profile_photo_data_url: str | None = None
    is_premium: bool = False
    status: str
    proxy_id: str | None
    proxy_required: bool
    health_score: int
    warmup_score: int
    warmup_status: str
    origin: AccountOrigin | None = None
    claimed_age_days: int | None = None
    real_age_days: int | None = None
    system_age_days: int = 0
    trust_age_bonus: int = 0
    incubator_mode: bool = False
    incubator_until_at: str | None = None
    immortal_candidate: bool = False
    owner_attested: bool = False
    telegram_manual_hold: bool | None = None
    telegram_safety_reason: str | None = None
    restriction_status: str | None = None
    restriction_label: str | None = None
    restriction_checked_at: str | None = None
    restriction_until_text: str | None = None
    restriction_until_at: str | None = None
    restriction_raw_text: str | None = None
    quality_score: dict | None = None
    import_batch_id: str | None = None
    import_batch_size: int | None = None
    import_batch_at: str | None = None
    latest_trust: AccountTrustSnapshotResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AccountRiskProfileResponse(BaseModel):
    risk_level: str
    score: int
    reasons: list[str] = Field(default_factory=list)
    hard_blocks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommended_next_step: str | None = None


class AccountModuleEligibilityResponse(BaseModel):
    module: str
    status: str
    reason: str | None = None
    message: str | None = None
    allowed_limits: dict | None = None
    risk_profile: AccountRiskProfileResponse | None = None


class AccountRiskResponse(BaseModel):
    account_id: str
    risk_profile: AccountRiskProfileResponse
    module_eligibility: dict[str, AccountModuleEligibilityResponse]


class AccountRiskSummaryResponse(BaseModel):
    total: int
    risk_levels: dict[str, int]
    module_eligibility: dict[str, dict[str, int]]
    restriction_statuses: dict[str, int]
    hard_blocks: dict[str, int]


class AccountQualityCheckCreate(BaseModel):
    provider: str = "manual"
    rating: float | None = Field(default=None, ge=0, le=10)
    potential_30d: float | None = Field(default=None, ge=0, le=10)
    label: str | None = None
    potential_label: str | None = None
    raw: dict = Field(default_factory=dict)


class AccountQualityCheckResponse(BaseModel):
    id: str
    account_id: str
    provider: str
    rating: float | None = None
    potential_30d: float | None = None
    label: str | None = None
    potential_label: str | None = None
    raw: dict = Field(default_factory=dict)
    checked_at: datetime


class RiskDecisionResponse(BaseModel):
    id: str
    workspace_id: str
    account_id: str | None = None
    module: str
    decision: str
    reasons: list[str] = Field(default_factory=list)
    limits: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountImportBatchResponse(BaseModel):
    batch_id: str
    imported_at: str | None = None
    declared_count: int | None = None
    seller: str | None = None
    purchase_date: str | None = None
    declared_geo: str | None = None
    declared_age: str | None = None
    notes: str | None = None
    status: str | None = None
    account_count: int
    clear_count: int
    limited_count: int
    blocked_count: int
    unknown_count: int
    invalid_count: int
    quarantined_count: int
    active_count: int
    spam_block_rate: float
    survival_rate: float
    proxy_providers: list[str] = Field(default_factory=list)
    verdict: str


class AccountImportBatchPatch(BaseModel):
    seller: str | None = None
    purchase_date: str | None = None
    declared_geo: str | None = None
    declared_age: str | None = None
    notes: str | None = None
    status: str | None = None


class TelegramAccountPatch(BaseModel):
    username: str | None = None
    display_name: str | None = None
    proxy_id: str | None = None
    proxy_required: bool | None = None
    status: str | None = None
    warmup_score: int | None = Field(default=None, ge=0, le=100)
    warmup_status: str | None = Field(default=None, max_length=32)
    origin: AccountOrigin | None = None
    claimed_age_days: int | None = Field(default=None, ge=0, le=3650)
    real_age_days: int | None = Field(default=None, ge=0, le=3650)
    incubator_mode: bool | None = None
    incubator_until_at: str | None = None


class TelegramAccountOrderUpdate(BaseModel):
    account_ids: list[str] = Field(min_length=1)


class ImmortalCandidateResponse(BaseModel):
    account: TelegramAccountResponse
    candidate_score: int
    reasons: list[str] = Field(default_factory=list)


class AccountHealthEventResponse(BaseModel):
    id: str
    event_type: str
    severity: str
    message: str
    raw_error: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TelegramAccountProfileResponse(BaseModel):
    account_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    bio: str | None = None
    about: str | None = None
    phone: str | None = None
    photo_data_url: str | None = None
    status: str
    health_score: int
    can_edit_security: bool


class TelegramAccountProfilePhotoResponse(BaseModel):
    photo_id: str
    date: datetime | None = None
    photo_data_url: str | None = None
    is_current: bool = False


class TelegramAccountProfilePhotosResponse(BaseModel):
    photos: list[TelegramAccountProfilePhotoResponse] = Field(default_factory=list)


class TelegramAccountLoginCodeResponse(BaseModel):
    status: str
    account_id: str
    phone: str | None = None
    login_code: str | None = None
    message_date: datetime | None = None
    message: str


class TelegramAccountProfilePatch(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    bio: str | None = None
    about: str | None = None

    @field_validator("first_name")
    @classmethod
    def first_name_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("first_name must not be empty")
        return value

    @field_validator("last_name", "bio", "about")
    @classmethod
    def optional_text_allow_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("username")
    @classmethod
    def optional_username_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TelegramAccountCloneProfileRequest(BaseModel):
    source_ref: str | None = None
    source_account_id: str | None = None
    copy_name: bool = True
    copy_bio: bool = True
    copy_photo: bool = True
    copy_stories: bool = False
    pin_stories: bool = False
    delete_old_stories: bool = False
    story_limit: int = Field(default=3, ge=1, le=20)

    @field_validator("source_ref", "source_account_id")
    @classmethod
    def optional_text_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TelegramAccountCloneProfileResponse(BaseModel):
    status: str
    account_id: str
    source_ref: str | None = None
    source_account_id: str | None = None
    mode: str
    copied_fields: list[str] = Field(default_factory=list)
    skipped_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    profile: TelegramAccountProfileResponse


class TelegramAccountTwoFaUpdate(BaseModel):
    current_password: str | None = None
    new_password: str | None = None
    hint: str | None = None
    confirmation_text: str

    @field_validator("current_password", "new_password", "hint")
    @classmethod
    def optional_secret_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value or None


class AccountBulkActionResponse(BaseModel):
    status: str
    action: str
    account_count: int
    target_count: int
    message: str
    warnings: list[str] = Field(default_factory=list)
    request_id: str | None = None


class AccountBulkStatusRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1)


class AccountBulkDetachOverloadedProxiesRequest(BaseModel):
    proxy_ids: list[str] = Field(default_factory=list)


class AccountBulkJoinChatsRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    target_refs: list[str] = Field(min_length=1)
    delay_seconds_min: int = Field(default=60, ge=30, le=86400)
    delay_seconds_max: int = Field(default=180, ge=30, le=86400)
    split_folder_links: bool = True


class AccountBulkCopyStoriesRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    source_ref: str | None = None
    source_account_id: str | None = None
    story_ids: list[str] = Field(default_factory=list)
    pin_on_top: bool = False
    delete_old_stories: bool = False
    delay_seconds_min: int = Field(default=45, ge=15, le=86400)
    delay_seconds_max: int = Field(default=180, ge=15, le=86400)

    @field_validator("source_ref")
    @classmethod
    def source_ref_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AccountBulkCloneProfileRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    source_ref: str | None = None
    source_account_id: str | None = None
    copy_name: bool = True
    copy_bio: bool = True
    copy_photo: bool = True
    copy_stories: bool = False
    pin_stories: bool = False
    delete_old_stories: bool = False
    story_limit: int = Field(default=3, ge=1, le=20)

    @field_validator("source_ref", "source_account_id")
    @classmethod
    def optional_text_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AccountBulkAiProfilesRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    generate_first_last_name: bool = True
    generate_bio: bool = True
    generate_photo: bool = False
    profile_gender: Literal["female", "male"] = "female"
    style_prompt: str = Field(default="естественный живой профиль")
    custom_first_name: str | None = None
    custom_last_name: str | None = None
    custom_bio: str | None = None
    delay_seconds_min: int = Field(default=15, ge=15, le=86400)
    delay_seconds_max: int = Field(default=30, ge=15, le=86400)


class TelegramPhoneLoginStart(BaseModel):
    phone: str
    username: str | None = None
    display_name: str | None = None
    proxy_id: str | None = None
    new_proxy_raw: str | None = None
    new_proxy_max_accounts: int = Field(default=1, ge=1, le=100)
    proxy_required: bool = True
    warmup_status: str | None = Field(default=None, max_length=32)
    warmup_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("phone")
    @classmethod
    def phone_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("phone must not be empty")
        return value


class TelegramPhoneLoginStartResponse(BaseModel):
    status: str
    auth_token: str
    phone: str


class TelegramPhoneLoginCodeConfirm(BaseModel):
    auth_token: str
    code: str

    @field_validator("auth_token", "code")
    @classmethod
    def text_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class TelegramPhoneLoginPasswordConfirm(BaseModel):
    auth_token: str
    password: str

    @field_validator("auth_token", "password")
    @classmethod
    def secret_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class TelegramPhoneLoginCompleteResponse(BaseModel):
    status: str
    account: TelegramAccountResponse | None = None
    auth_token: str | None = None
    message: str | None = None
