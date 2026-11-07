from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.channel_map.schemas import (
    ChannelMapCategoryResponse,
    ChannelMapChannelsResponse,
    ChannelMapSummaryResponse,
)
from app.modules.channel_map.service import atlas_categories, atlas_channels, atlas_summary, load_atlas

router = APIRouter()


@router.get("/summary", response_model=ChannelMapSummaryResponse)
async def summary() -> dict:
    atlas = await load_atlas()
    return atlas_summary(atlas)


@router.get("/categories", response_model=list[ChannelMapCategoryResponse])
async def categories(
    level: Annotated[str, Query(pattern="^(macro|micro)$")] = "macro",
    parent_id: Annotated[int | None, Query()] = None,
) -> list[dict]:
    atlas = await load_atlas()
    return atlas_categories(atlas, level=level, parent_id=parent_id)


@router.get("/channels", response_model=ChannelMapChannelsResponse)
async def channels(
    level: Annotated[str, Query(pattern="^(macro|micro)$")] = "macro",
    category_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    atlas = await load_atlas()
    total, items = atlas_channels(atlas, level=level, category_id=category_id, query=q, limit=limit, offset=offset)
    return {
        "total": total,
        "atlas_total": atlas_summary(atlas)["total_channels"],
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "items": items,
    }
