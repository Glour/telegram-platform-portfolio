from pydantic import BaseModel


class ChannelMapCategoryResponse(BaseModel):
    id: int
    label: str
    color: str
    channel_count: int
    parent_id: int | None = None


class ChannelMapChannelResponse(BaseModel):
    channel_id: str
    username: str | None
    title: str
    participants_count: int
    cluster_id: int | None
    cluster_label: str | None
    sub_cluster_id: int | None
    sub_cluster_label: str | None
    avatar_url: str | None
    ref: str


class ChannelMapChannelsResponse(BaseModel):
    total: int
    atlas_total: int
    limit: int
    offset: int
    has_more: bool
    items: list[ChannelMapChannelResponse]


class ChannelMapSummaryResponse(BaseModel):
    total_channels: int
    macro_categories: int
    micro_categories: int
