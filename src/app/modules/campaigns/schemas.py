from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.ai.humanizer import DEFAULT_AUTHOR_GENDER, AuthorGender

CampaignMode = Literal["comment_new_posts", "comment_recent_posts", "draft_only", "generate_and_send"]
CampaignPostSelectionMode = Literal["new_posts", "historical_posts", "latest_post"]
CampaignDeliveryMode = Literal["draft_only", "generate_and_send"]
CampaignSourceAssignmentMode = Literal["standard", "parallel_pool", "multi_account_split", "manual_pinned"]


class CampaignInviteTrackingConfig(BaseModel):
    invite_link: str = Field(min_length=8, max_length=512)
    source_ref: str = Field(min_length=1, max_length=255)
    account_id: str | None = None

    @field_validator("invite_link", "source_ref")
    @classmethod
    def stripped_value_must_not_be_empty(cls, value: str) -> str:
        text = " ".join(value.split()).strip()
        if not text:
            raise ValueError("Field may not be empty")
        return text


class CampaignStopConditions(BaseModel):
    max_comments: int | None = Field(default=None, ge=1)
    run_for_minutes: int | None = Field(default=None, ge=1)
    stop_at: datetime | None = None
    active_window: dict | None = None
    manual_stop: bool = True


class CommentPostsCampaignCreate(BaseModel):
    name: str
    folder: str | None = Field(default=None, max_length=80)
    source_ids: list[str] = Field(default_factory=list)
    source_folder_ids: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(min_length=1)
    source_assignment_mode: CampaignSourceAssignmentMode = "standard"
    comment_prompt_template_id: str
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER
    latest_posts_per_source: int = Field(default=0, ge=0, le=500)
    post_selection_mode: CampaignPostSelectionMode = "new_posts"
    delivery_mode: CampaignDeliveryMode = "draft_only"
    mode: CampaignMode | None = None
    stop_conditions: CampaignStopConditions = Field(default_factory=CampaignStopConditions)
    policy: dict = Field(default_factory=dict)
    invite_tracking: CampaignInviteTrackingConfig | None = None


class CampaignAccountReplacementRequest(BaseModel):
    old_account_id: str = Field(min_length=1)
    new_account_id: str = Field(min_length=1)
    join_interval_seconds: int = Field(default=300, ge=60, le=86400)
    resume_when_ready: bool = False
    request_id: str = Field(min_length=1, max_length=80)


class CommentPostsCampaignUpdate(BaseModel):
    name: str | None = None
    folder: str | None = Field(default=None, max_length=80)
    source_ids: list[str] | None = Field(default=None, min_length=1)
    source_folder_ids: list[str] | None = None
    account_ids: list[str] | None = Field(default=None, min_length=1)
    source_assignment_mode: CampaignSourceAssignmentMode | None = None
    source_assignment: dict[str, list[str]] | None = None
    comment_prompt_template_id: str | None = None
    latest_posts_per_source: int | None = Field(default=None, ge=0, le=500)
    post_selection_mode: CampaignPostSelectionMode | None = None
    delivery_mode: CampaignDeliveryMode | None = None
    mode: CampaignMode | None = None
    stop_conditions: dict | None = None
    policy: dict | None = None
    prompt_text: str | None = None
    prompt_name: str | None = None
    product_context: str | None = None
    max_length: int | None = Field(default=None, ge=40, le=1000)
    prompt_tone: str | None = None
    author_gender: AuthorGender | None = None
    tone: str | None = None
    trigger_keywords: list[str] | None = None
    keyword_mode: Literal["any"] | None = None
    messages_per_source: int | None = Field(default=None, ge=1, le=200)
    messages_per_dialog: int | None = Field(default=None, ge=1, le=100)
    max_replies_per_tick: int | None = Field(default=None, ge=1, le=50)
    min_quality_score: int | None = Field(default=None, ge=0, le=100)
    reply_to_only: bool | None = None
    only_reply_to_incoming: bool | None = None
    reactions: list[str] | None = Field(default=None, min_length=1)
    target_scope: Literal["posts", "comments", "chat_messages"] | None = None
    reaction_mode: Literal["fixed", "random", "round_robin", "weighted", "ai_context"] | None = None
    reaction_weights: dict[str, int] | None = None
    comments_per_post: int | None = Field(default=None, ge=1, le=200)
    messages_per_source: int | None = Field(default=None, ge=1, le=500)
    react_as: Literal["account"] | None = None
    reaction_probability: int | None = Field(default=None, ge=1, le=100)
    max_reactions: int | None = Field(default=None, ge=1)
    message_text: str | None = Field(default=None, min_length=1, max_length=4000)
    max_messages: int | None = Field(default=None, ge=1)
    daily_limit_per_account: int | None = Field(default=None, ge=1, le=5000)
    delay_seconds_min: int | None = Field(default=None, ge=1, le=86400)
    delay_seconds_max: int | None = Field(default=None, ge=1, le=86400)
    invite_tracking: CampaignInviteTrackingConfig | None = None

    @field_validator(
        "source_ids",
        "account_ids",
        "source_folder_ids",
        "source_assignment_mode",
        "source_assignment",
        "comment_prompt_template_id",
        "latest_posts_per_source",
        "post_selection_mode",
        "delivery_mode",
        "mode",
        "tone",
        "trigger_keywords",
        "keyword_mode",
        "messages_per_source",
        "messages_per_dialog",
        "max_replies_per_tick",
        "min_quality_score",
        "reactions",
        "target_scope",
        "reaction_mode",
        "reaction_weights",
        "comments_per_post",
        "messages_per_source",
        "react_as",
        "reaction_probability",
        "max_reactions",
        "message_text",
        "max_messages",
        "daily_limit_per_account",
        "delay_seconds_min",
        "delay_seconds_max",
        "max_length",
        "author_gender",
    )
    @classmethod
    def required_patch_value_must_not_be_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field may not be null")
        return value


class NeuroChatCampaignCreate(BaseModel):
    name: str
    folder: str | None = Field(default=None, max_length=80)
    source_ids: list[str] = Field(default_factory=list)
    source_folder_ids: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(min_length=1)
    source_assignment_mode: CampaignSourceAssignmentMode = "standard"
    prompt_text: str = Field(min_length=10)
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER
    tone: str = "спокойный"
    delivery_mode: CampaignDeliveryMode = "draft_only"
    trigger_keywords: list[str] = Field(default_factory=list)
    keyword_mode: Literal["any"] = "any"
    messages_per_source: int = Field(default=30, ge=1, le=200)
    max_replies_per_tick: int = Field(default=5, ge=1, le=50)
    min_quality_score: int = Field(default=55, ge=0, le=100)
    reply_to_only: bool = True
    stop_conditions: CampaignStopConditions = Field(default_factory=CampaignStopConditions)
    policy: dict = Field(default_factory=dict)
    invite_tracking: CampaignInviteTrackingConfig | None = None


class NeuroDialogCampaignCreate(BaseModel):
    name: str
    folder: str | None = Field(default=None, max_length=80)
    account_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)
    source_folder_ids: list[str] = Field(default_factory=list)
    target_refs: list[str] = Field(default_factory=list)
    source_assignment_mode: CampaignSourceAssignmentMode = "standard"
    prompt_text: str = Field(min_length=10)
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER
    tone: str = "доброжелательный"
    delivery_mode: CampaignDeliveryMode = "draft_only"
    messages_per_dialog: int = Field(default=20, ge=1, le=100)
    max_replies_per_tick: int = Field(default=5, ge=1, le=50)
    min_quality_score: int = Field(default=45, ge=0, le=100)
    only_reply_to_incoming: bool = True
    delay_seconds_min: int = Field(default=45, ge=1, le=86400)
    delay_seconds_max: int = Field(default=180, ge=1, le=86400)
    stop_conditions: CampaignStopConditions = Field(default_factory=CampaignStopConditions)
    policy: dict = Field(default_factory=dict)
    invite_tracking: CampaignInviteTrackingConfig | None = None


class MassReactionsCampaignCreate(BaseModel):
    name: str
    folder: str | None = Field(default=None, max_length=80)
    source_ids: list[str] = Field(default_factory=list)
    source_folder_ids: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(min_length=1)
    source_assignment_mode: CampaignSourceAssignmentMode = "standard"
    reactions: list[str] = Field(min_length=1)
    target_scope: Literal["posts", "comments", "chat_messages"] = "posts"
    reaction_mode: Literal["fixed", "random", "round_robin", "weighted", "ai_context"] = "round_robin"
    reaction_weights: dict[str, int] = Field(default_factory=dict)
    comments_per_post: int = Field(default=3, ge=1, le=200)
    messages_per_source: int = Field(default=10, ge=1, le=500)
    react_as: Literal["account"] = "account"
    post_selection_mode: CampaignPostSelectionMode = "new_posts"
    latest_posts_per_source: int = Field(default=0, ge=0, le=500)
    reaction_probability: int = Field(default=100, ge=1, le=100)
    max_reactions: int | None = Field(default=None, ge=1)
    run_for_minutes: int | None = Field(default=None, ge=1)
    daily_limit_per_account: int | None = Field(default=None, ge=1, le=5000)
    delay_seconds_min: int = Field(default=600, ge=1, le=86400)
    delay_seconds_max: int = Field(default=1800, ge=1, le=86400)
    policy: dict = Field(default_factory=dict)
    invite_tracking: CampaignInviteTrackingConfig | None = None


class MessageBroadcastCampaignCreate(BaseModel):
    name: str
    folder: str | None = Field(default=None, max_length=80)
    source_ids: list[str] = Field(default_factory=list)
    source_folder_ids: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(min_length=1)
    source_assignment_mode: CampaignSourceAssignmentMode = "standard"
    message_text: str = Field(min_length=1, max_length=4000)
    author_gender: AuthorGender = DEFAULT_AUTHOR_GENDER
    max_messages: int | None = Field(default=None, ge=1)
    run_for_minutes: int | None = Field(default=None, ge=1)
    daily_limit_per_account: int = Field(default=3, ge=1, le=3)
    delay_seconds_min: int = Field(default=900, ge=1, le=86400)
    delay_seconds_max: int = Field(default=3600, ge=1, le=86400)
    policy: dict = Field(default_factory=dict)


class CampaignResponse(BaseModel):
    id: str
    workspace_id: str
    type: str
    name: str
    status: str
    config: dict
    policy: dict
    sent_count: int = 0
    active_run_status: str | None = None
    last_run_status: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    invite_tracking: dict | None = None

    model_config = {"from_attributes": True}


class CampaignInviteTrackingResponse(BaseModel):
    id: str
    campaign_id: str
    telegram_account_id: str | None
    source_ref: str
    invite_link: str
    title: str | None
    baseline_usage_count: int | None
    usage_count: int | None
    requested_count: int | None
    joined_delta: int | None = None
    revoked: bool | None
    expire_date: datetime | None
    usage_limit: int | None
    last_checked_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    raw: dict

    model_config = {"from_attributes": True}


class CampaignSafetyAccountResponse(BaseModel):
    account_id: str
    account_label: str
    module: str
    status: str
    reason: str | None = None
    message: str | None = None
    risk_level: str
    risk_score: int
    recommended_next_step: str | None = None


class CampaignSafetySourceResponse(BaseModel):
    source_id: str
    source_label: str
    status: str
    reason: str | None = None
    message: str | None = None
    risk_level: str
    recommended_next_step: str | None = None


class CampaignSafetyResponse(BaseModel):
    campaign_id: str
    module: str
    status: str
    can_start: bool
    blocked_count: int
    limited_count: int
    accounts: list[CampaignSafetyAccountResponse]
    sources: list[CampaignSafetySourceResponse] = Field(default_factory=list)


class CampaignDryRunResponse(BaseModel):
    campaign_id: str
    campaign_type: str
    can_start: bool
    accounts: dict[str, int]
    sources: dict[str, int]
    effective_policy: dict
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    safety: CampaignSafetyResponse


class CampaignActionResponse(BaseModel):
    id: str
    campaign_id: str
    telegram_account_id: str | None
    target_type: str
    target_id: str
    action_type: str
    status: str
    planned_at: datetime | None
    executed_at: datetime | None
    result: dict | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CampaignRunResponse(BaseModel):
    id: str
    workspace_id: str
    campaign_id: str
    status: str
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    requested_stop_at: datetime | None
    progress: dict
    stop_conditions: dict
    report: dict
    error_code: str | None
    error_message: str | None

    model_config = {"from_attributes": True}


class CampaignCommentReportRow(BaseModel):
    comment_id: str
    campaign_id: str
    source_id: str
    channel: str
    source_title: str | None
    source_username: str | None
    source_url: str | None
    post_message_id: str
    post_tg_message_id: str | None
    post_published_at: datetime | None
    post_url: str | None
    post_text: str | None
    comment_text: str
    comment_status: str
    comment_sent_at: datetime | None
    comment_created_at: datetime
    comment_error_code: str | None
    comment_error_message: str | None
    tg_comment_message_id: str | None
    account_id: str | None
    account_label: str | None


class CampaignChannelReportSummary(BaseModel):
    source_id: str
    channel: str
    source_title: str | None
    source_username: str | None
    total_count: int
    sent_count: int
    draft_count: int
    failed_count: int
    last_comment_at: datetime | None


class CampaignCommentReport(BaseModel):
    campaign_id: str
    campaign_name: str
    campaign_status: str
    generated_at: datetime
    total_count: int
    sent_count: int
    draft_count: int
    failed_count: int
    by_channel: list[CampaignChannelReportSummary]
    rows: list[CampaignCommentReportRow]


class CampaignReactionReportRow(BaseModel):
    action_id: str
    campaign_id: str
    source_id: str | None
    source_title: str | None
    source_ref: str | None
    message_id: str | None
    tg_message_id: str | None
    target_kind: str | None = None
    target_url: str | None = None
    post_published_at: datetime | None
    post_text: str | None
    reaction: str | None
    status: str
    account_id: str | None
    account_label: str | None
    created_at: datetime
    executed_at: datetime | None
    error_code: str | None
    error_message: str | None


class CampaignReactionReport(BaseModel):
    campaign_id: str
    campaign_name: str
    campaign_status: str
    generated_at: datetime
    total_count: int
    sent_count: int
    failed_count: int
    rows: list[CampaignReactionReportRow]


class CampaignEventResponse(BaseModel):
    id: str
    run_id: str
    campaign_id: str
    level: str
    event_type: str
    message: str
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class CampaignRuntimeLogResponse(CampaignEventResponse):
    account_id: str | None = None
    account_name: str | None = None
    source_id: str | None = None
    source_title: str | None = None
    source_ref: str | None = None
