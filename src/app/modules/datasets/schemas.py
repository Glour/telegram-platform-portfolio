from datetime import datetime

from pydantic import BaseModel, Field


class CommentDatasetCollectRequest(BaseModel):
    telegram_account_id: str | None = None
    source_ids: list[str] | None = None
    posts_per_source: int = Field(default=10, ge=1, le=50)
    comments_per_post: int = Field(default=30, ge=1, le=100)
    min_comment_length: int = Field(default=18, ge=1, le=500)
    max_items: int = Field(default=300, ge=1, le=5000)


class CommentDatasetDiscoverCollectRequest(CommentDatasetCollectRequest):
    dialog_limit: int = Field(default=300, ge=1, le=2000)
    max_sources: int = Field(default=50, ge=1, le=300)


class CommentDatasetItemResponse(BaseModel):
    id: str
    source_id: str
    post_message_id: str | None
    tg_comment_message_id: str
    post_text: str | None
    comment_text: str
    quality_score: int
    status: str
    published_at: datetime | None

    model_config = {"from_attributes": True}


class CommentDatasetCollectResponse(BaseModel):
    sources_checked: int
    posts_checked: int
    comments_seen: int
    created: int
    skipped: int
    examples: list[CommentDatasetItemResponse]


class DiscoveredCommentSourceResponse(BaseModel):
    id: str
    username: str | None
    title: str | None
    comments_enabled: bool

    model_config = {"from_attributes": True}


class CommentDatasetDiscoverCollectResponse(CommentDatasetCollectResponse):
    discovered_sources: int
    sources: list[DiscoveredCommentSourceResponse]
