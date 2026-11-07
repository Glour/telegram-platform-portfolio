from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.sources.schemas import MessageResponse


class ParseDelaySettings(BaseModel):
    delay_seconds_min: float | None = Field(default=None, ge=0, le=60)
    delay_seconds_max: float | None = Field(default=None, ge=0, le=300)


class ParseSourceRequest(BaseModel):
    source_id: str
    limit: int = Field(default=10, ge=1, le=100)
    seed_text: str | None = None
    telegram_account_id: str | None = None
    ensure_membership: bool = False
    join_discussion: bool = False


class ParseSourceResponse(BaseModel):
    job_id: str | None = None
    source_id: str
    created: int
    messages: list[MessageResponse]


DialogEntityType = Literal["all", "channels", "groups", "users"]
CommentsMode = Literal["any", "open", "closed"]


class ParseDialogsRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    entity_type: DialogEntityType = "all"
    limit: int = Field(default=100, ge=1, le=500)
    keywords: list[str] = Field(default_factory=list)
    min_participants: int | None = Field(default=None, ge=0)
    max_participants: int | None = Field(default=None, ge=0)
    active_days: int | None = Field(default=None, ge=1, le=3650)


class ParsedDialogItem(BaseModel):
    tg_id: str | None
    type: str
    title: str | None
    username: str | None
    participants_count: int | None = None
    unread_count: int = 0
    last_message_at: datetime | None = None
    raw: dict = Field(default_factory=dict)


class ParseDialogsResponse(BaseModel):
    job_id: str | None = None
    account_id: str
    entity_type: DialogEntityType
    count: int
    items: list[ParsedDialogItem]


class ParseUsersRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    source_id: str | None = None
    source_ref: str | None = None
    limit: int = Field(default=200, ge=1, le=5000)
    min_comments: int = Field(default=0, ge=0, le=100000)
    min_messages: int = Field(default=0, ge=0, le=100000)
    username_required: bool = False
    active_days: int | None = Field(default=None, ge=1, le=3650)
    keywords: list[str] = Field(default_factory=list)


class ParsedUserItem(BaseModel):
    tg_user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    is_bot: bool = False
    language_code: str | None = None
    messages_count: int = 0
    comments_count: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_activity_at: datetime | None = None
    sample_messages: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class ParseUsersResponse(BaseModel):
    job_id: str | None = None
    account_id: str
    source_ref: str
    count: int
    users: list[ParsedUserItem]


class ParseCommentsRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    source_id: str | None = None
    source_ref: str | None = None
    post_tg_message_id: str | None = None
    post_limit: int = Field(default=5, ge=1, le=100)
    comments_per_post: int = Field(default=50, ge=1, le=500)
    keywords: list[str] = Field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    ensure_membership: bool = False


class ParsedCommentItem(BaseModel):
    post_tg_message_id: str
    comment_tg_message_id: str
    text: str
    author_tg_id: str | None = None
    author_username: str | None = None
    author_display_name: str | None = None
    published_at: datetime | None = None
    matched_keywords: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class ParseCommentsResponse(BaseModel):
    job_id: str | None = None
    account_id: str
    source_ref: str
    count: int
    comments: list[ParsedCommentItem]


ParsingJobType = Literal[
    "channel_search",
    "channel_validate",
    "group_search",
    "source_posts",
    "source_comments",
    "source_users",
    "message_users",
    "dialogs",
]
ParsingJobStatus = Literal["draft", "queued", "running", "completed", "failed", "cancelled", "paused"]


class ParsingJobResponse(BaseModel):
    id: str
    workspace_id: str
    type: str
    status: str
    account_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    progress_total: int = 0
    progress_done: int = 0
    found_count: int = 0
    saved_count: int = 0
    skipped_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ParsingJobLogResponse(BaseModel):
    id: str
    job_id: str
    level: str
    message: str
    payload: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ParsingResultResponse(BaseModel):
    id: str
    job_id: str
    workspace_id: str
    entity_type: str
    entity_ref: str | None = None
    source_id: str | None = None
    user_id: str | None = None
    message_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    matched_keywords: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ParsingJobDetailResponse(ParsingJobResponse):
    logs: list[ParsingJobLogResponse] = Field(default_factory=list)


class ParsingJobCreateRequest(BaseModel):
    type: ParsingJobType
    telegram_account_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)


class ParseMessageUsersRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    source_id: str | None = None
    source_ref: str | None = None
    keywords: list[str] = Field(default_factory=list)
    keywords_mode: Literal["any", "all", "phrase"] = "any"
    date_from: datetime | None = None
    date_to: datetime | None = None
    message_limit: int = Field(default=500, ge=1, le=10000)
    user_limit: int = Field(default=500, ge=1, le=5000)
    min_messages: int = Field(default=1, ge=1, le=100000)
    username_required: bool = False


class ParseMessageUsersResponse(BaseModel):
    job_id: str | None = None
    account_id: str
    source_ref: str
    count: int
    users: list[ParsedUserItem]


class ChannelSearchRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    keywords: list[str] = Field(default_factory=list)
    keyword_suffixes: list[str] = Field(default_factory=list)
    category_id: int | None = None
    subcategory_id: int | None = None
    min_participants: int | None = Field(default=None, ge=0)
    max_participants: int | None = Field(default=None, ge=0)
    active_days: int | None = Field(default=None, ge=1, le=3650)
    language_code: str | None = None
    country_code: str | None = None
    comments_required: bool = False
    comments_mode: CommentsMode = "any"
    min_engagement_score: int | None = Field(default=None, ge=0, le=100)
    limit: int = Field(default=50, ge=1, le=200)
    source: Literal["atlas", "telegram_search", "mixed"] = "mixed"


class ChannelSearchItem(BaseModel):
    title: str
    username: str | None = None
    ref: str
    tg_id: str | None = None
    participants_count: int | None = None
    language_code: str | None = None
    country_code: str | None = None
    category: str | None = None
    subcategory: str | None = None
    comments_enabled: bool | None = None
    linked_discussion: str | None = None
    last_message_at: datetime | None = None
    engagement_score: int | None = None
    source_risk_level: str = "unknown"
    status: str = "found"
    raw: dict[str, Any] = Field(default_factory=dict)


class ChannelSearchResponse(BaseModel):
    job_id: str | None = None
    count: int
    items: list[ChannelSearchItem]


class ChannelValidateRequest(BaseModel):
    telegram_account_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    comments_required: bool = True
    comments_mode: CommentsMode = "open"
    min_participants: int | None = Field(default=None, ge=0)
    active_days: int | None = Field(default=90, ge=1, le=3650)
    limit: int = Field(default=100, ge=1, le=300)


class GroupSearchRequest(ParseDelaySettings):
    telegram_account_id: str | None = None
    keywords: list[str] = Field(default_factory=list)
    keyword_suffixes: list[str] = Field(default_factory=list)
    min_participants: int | None = Field(default=None, ge=0)
    max_participants: int | None = Field(default=None, ge=0)
    active_days: int | None = Field(default=90, ge=1, le=3650)
    min_activity_score: int | None = Field(default=None, ge=0, le=100)
    access_type: Literal["any", "public", "private"] = "any"
    ai_suggestions_enabled: bool = False
    limit: int = Field(default=50, ge=1, le=300)


class GroupSearchItem(BaseModel):
    title: str | None = None
    username: str | None = None
    ref: str
    tg_id: str | None = None
    participants_count: int | None = None
    last_message_at: datetime | None = None
    activity_score: int | None = None
    source: str = "account_dialogs"
    raw: dict[str, Any] = Field(default_factory=dict)


class GroupSearchResponse(BaseModel):
    job_id: str | None = None
    count: int
    items: list[GroupSearchItem]
