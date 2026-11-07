# ruff: noqa: E501
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ContentFactoryStatus = Literal["draft", "active", "paused", "archived"]
ContentFactoryMode = Literal[
    "drafts_only", "approval_required", "auto_publish", "auto_publish_safe", "auto_publish_full"
]
ContentPostStatus = Literal["draft", "needs_review", "approved", "scheduled", "published", "failed", "rejected"]


class ContentFactorySourceCreate(BaseModel):
    source_id: str | None = None
    source_ref: str | None = Field(default=None, max_length=255)
    source_type: str = "telegram_channel"
    enabled: bool = True
    weight: int = Field(default=1, ge=1, le=100)
    settings: dict = Field(default_factory=dict)


class ContentFactoryKnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    kind: str = "text"
    content_text: str | None = None
    file_path: str | None = None
    metadata: dict = Field(default_factory=dict)
    enabled: bool = True


class ContentFactorySiteContextRequest(BaseModel):
    site_url: str | None = Field(default=None, max_length=500)
    max_pages: int = Field(default=8, ge=1, le=20)


class ContentFactoryRunRequest(BaseModel):
    topic_prompt: str | None = Field(default=None, max_length=2000)


class ContentFactoryRubricCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    target_share_percent: int = Field(default=0, ge=0, le=100)
    format_preset: str | None = Field(default=None, max_length=80)
    prompt_override: str | None = None
    enabled: bool = True


class ContentFactoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    target_type: str = "channel"
    target_ref: str | None = Field(default=None, max_length=255)
    publisher_account_id: str | None = None
    mode: ContentFactoryMode = "approval_required"
    schedule_config: dict = Field(default_factory=dict)
    content_config: dict = Field(default_factory=dict)
    image_config: dict = Field(default_factory=dict)
    quality_config: dict = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    manual_source_refs: list[str] = Field(default_factory=list)
    knowledge_items: list[ContentFactoryKnowledgeCreate] = Field(default_factory=list)
    rubrics: list[ContentFactoryRubricCreate] = Field(default_factory=list)
    activate: bool = False


class ContentFactoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    status: ContentFactoryStatus | None = None
    target_type: str | None = None
    target_ref: str | None = Field(default=None, max_length=255)
    publisher_account_id: str | None = None
    mode: ContentFactoryMode | None = None
    schedule_config: dict | None = None
    content_config: dict | None = None
    image_config: dict | None = None
    quality_config: dict | None = None

    @field_validator("name", "target_type", "mode")
    @classmethod
    def required_patch_value_must_not_be_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field may not be null")
        return value


class ContentFactorySourceResponse(BaseModel):
    id: str
    factory_id: str
    source_id: str | None
    source_ref: str | None
    source_type: str
    enabled: bool
    weight: int
    last_parsed_at: datetime | None
    settings: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentFactoryKnowledgeResponse(BaseModel):
    id: str
    factory_id: str
    title: str
    kind: str
    content_text: str | None
    file_path: str | None
    metadata: dict = Field(alias="item_metadata")
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ContentFactoryRubricResponse(BaseModel):
    id: str
    factory_id: str
    name: str
    description: str | None
    target_share_percent: int
    format_preset: str | None
    prompt_override: str | None
    enabled: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentPostUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=220)
    text: str | None = Field(default=None, min_length=1)
    scheduled_at: datetime | None = None


class ContentPostResponse(BaseModel):
    id: str
    factory_id: str
    run_id: str | None
    status: str
    title: str | None
    text: str
    source_summary: str | None
    source_refs: list
    image_prompt: str | None
    image_attachment_id: str | None
    telegram_message_id: str | None
    telegram_link: str | None
    scheduled_at: datetime | None
    published_at: datetime | None
    quality_score: int | None
    quality_notes: list
    metadata: dict = Field(alias="post_metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ContentFactoryRunResponse(BaseModel):
    id: str
    factory_id: str
    status: str
    trigger_type: str
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    metrics: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentFactoryEventResponse(BaseModel):
    id: str
    factory_id: str
    run_id: str | None
    post_id: str | None
    level: str
    event_type: str
    message: str
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class ContentFactorySourcePostResponse(BaseModel):
    id: str
    source_id: str
    source_title: str | None
    source_username: str | None
    source_url: str | None
    tg_message_id: str
    text: str | None
    published_at: datetime | None
    views_count: int
    forwards_count: int
    replies_count: int
    created_at: datetime


class ContentFactoryResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    status: str
    target_type: str
    target_ref: str | None
    publisher_account_id: str | None
    mode: str
    schedule_config: dict
    content_config: dict
    image_config: dict
    quality_config: dict
    drafts_count: int = 0
    published_24h: int = 0
    published_7d: int = 0
    errors_count: int = 0
    next_run_at: datetime | None = None
    last_successful_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContentFactoryDetailResponse(ContentFactoryResponse):
    sources: list[ContentFactorySourceResponse] = Field(default_factory=list)
    knowledge_items: list[ContentFactoryKnowledgeResponse] = Field(default_factory=list)
    rubrics: list[ContentFactoryRubricResponse] = Field(default_factory=list)
    recent_runs: list[ContentFactoryRunResponse] = Field(default_factory=list)
    recent_events: list[ContentFactoryEventResponse] = Field(default_factory=list)
    source_posts: list[ContentFactorySourcePostResponse] = Field(default_factory=list)


class ContentFactoryRunResult(BaseModel):
    run: ContentFactoryRunResponse
    post: ContentPostResponse | None = None
