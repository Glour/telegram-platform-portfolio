from datetime import datetime

from pydantic import BaseModel, Field


class SourceCreate(BaseModel):
    type: str = Field(default="unknown")
    username: str | None = None
    url: str | None = None
    title: str | None = None
    folder_id: str | None = None


class SourceBulkCreate(BaseModel):
    refs: str | list[str]
    type: str = Field(default="unknown")
    folder_id: str | None = None


class SourcePatch(BaseModel):
    access_status: str | None = Field(default=None, max_length=32)
    comments_enabled: bool | None = None


class SourceResponse(BaseModel):
    id: str
    workspace_id: str
    type: str
    username: str | None
    title: str | None
    url: str | None
    folder_id: str | None = None
    comments_enabled: bool
    access_status: str
    source_metadata: dict
    source_risk_level: str = "unknown"
    safe_for_new_accounts: bool = False
    safe_for_trusted_accounts: bool = False
    source_risk_reasons: list[str] = Field(default_factory=list)
    source_recommended_action: str | None = None

    model_config = {"from_attributes": True}


class SourceFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    color: str | None = Field(default=None, max_length=32)
    sort_order: int = 0


class SourceFolderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    color: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None


class SourceFolderAssignRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)


class SourceFolderResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    color: str | None
    sort_order: int
    sources_count: int = 0

    model_config = {"from_attributes": True}


class SourceBlacklistRequest(BaseModel):
    reason: str = Field(default="manual")
    error_code: str | None = None
    error_message: str | None = None
    details: dict = Field(default_factory=dict)


class SourceBlacklistEntryResponse(BaseModel):
    id: str
    workspace_id: str
    source_id: str
    source_ref: str | None
    source_type: str | None
    status: str
    reason: str
    error_code: str | None
    error_message: str | None
    campaign_id: str | None
    account_id: str | None
    blocked_by: str
    first_blocked_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    details: dict
    source: SourceResponse | None = None

    model_config = {"from_attributes": True}


class SourceSafetyResponse(BaseModel):
    source_id: str
    source_risk_level: str
    safe_for_new_accounts: bool
    safe_for_trusted_accounts: bool
    reasons: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


class SourceSafetySummaryResponse(BaseModel):
    total: int
    safe: int
    medium: int
    high: int
    critical: int
    unknown: int
    invalid: int
    archived: int
    blacklisted: int
    comments_closed: int
    private: int
    join_request: int
    write_forbidden: int
    not_found: int
    not_channel: int


class SourceBulkArchiveInvalidRequest(BaseModel):
    source_ids: list[str] | None = None


class SourceBulkArchiveInvalidResponse(BaseModel):
    status: str
    archived: int
    source_ids: list[str]


class SourceBulkItemResponse(BaseModel):
    ref: str
    status: str
    source: SourceResponse


class SourceBulkCreateResponse(BaseModel):
    created: int
    reused: int
    items: list[SourceBulkItemResponse]


class MessageResponse(BaseModel):
    id: str
    source_id: str
    tg_message_id: str
    text: str | None
    published_at: datetime | None
    replies_count: int

    model_config = {"from_attributes": True}
