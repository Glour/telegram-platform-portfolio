from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings

ATLAS_URL = "https://gramgpt.io/api/atlas/"
AVATAR_SIZE_QUERY = "?width=80&height=80"
CACHE_TTL = timedelta(hours=6)
ATLAS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://gramgpt.io/tools/channel-map",
}

_cached_at: datetime | None = None
_cached_atlas: dict[str, Any] | None = None


async def load_atlas() -> dict[str, Any]:
    global _cached_at, _cached_atlas

    now = datetime.now(UTC)
    if _cached_atlas is not None and _cached_at is not None and now - _cached_at < CACHE_TTL:
        return _cached_atlas

    atlas = _load_local_atlas()
    if atlas is not None:
        _cached_atlas = atlas
        _cached_at = now
        return atlas

    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        response = await client.get(ATLAS_URL, headers=ATLAS_HEADERS)
        response.raise_for_status()
        atlas = response.json()

    _validate_atlas(atlas)

    _cached_atlas = atlas
    _cached_at = now
    return atlas


def _load_local_atlas() -> dict[str, Any] | None:
    atlas_path = Path(get_settings().channel_atlas_file)
    if not atlas_path.exists():
        return None

    opener = gzip.open if atlas_path.suffix == ".gz" else open
    with opener(atlas_path, "rt", encoding="utf-8") as file:
        atlas = json.load(file)
    _validate_atlas(atlas)
    return atlas


def _validate_atlas(atlas: Any) -> None:
    if not isinstance(atlas, dict) or not isinstance(atlas.get("channels"), list):
        raise ValueError("Invalid channel atlas response")


def atlas_categories(atlas: dict[str, Any], level: str = "macro", parent_id: int | None = None) -> list[dict[str, Any]]:
    key = "sub_clusters" if level == "micro" else "clusters"
    categories = [item for item in atlas.get(key, []) if isinstance(item, dict)]
    if parent_id is not None and level == "micro":
        categories = [item for item in categories if item.get("parent_id") == parent_id]
    normalized = [_category_to_response(item) for item in categories]
    return sorted(normalized, key=lambda item: int(item.get("channel_count") or 0), reverse=True)


def _category_to_response(item: dict[str, Any]) -> dict[str, Any]:
    channel_count = item.get("channel_count", item.get("count", 0))
    try:
        channel_count_value = int(channel_count or 0)
    except (TypeError, ValueError):
        channel_count_value = 0
    color = item.get("color")
    if not isinstance(color, str) or not color:
        color = "#737373"
    return {
        "id": int(item.get("id") or 0),
        "label": str(item.get("label") or item.get("name") or "Без названия"),
        "color": color,
        "channel_count": channel_count_value,
        "parent_id": item.get("parent_id"),
    }


def atlas_summary(atlas: dict[str, Any]) -> dict[str, int]:
    channels = [item for item in atlas.get("channels", []) if isinstance(item, list)]
    clusters = [item for item in atlas.get("clusters", []) if isinstance(item, dict)]
    sub_clusters = [item for item in atlas.get("sub_clusters", []) if isinstance(item, dict)]
    return {
        "total_channels": len(channels),
        "macro_categories": len(clusters),
        "micro_categories": len(sub_clusters),
    }


def atlas_channels(
    atlas: dict[str, Any],
    *,
    level: str = "macro",
    category_id: int | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    channels = [item for item in atlas.get("channels", []) if isinstance(item, list) and len(item) >= 8]
    clusters = {item.get("id"): item for item in atlas.get("clusters", []) if isinstance(item, dict)}
    sub_clusters = [item for item in atlas.get("sub_clusters", []) if isinstance(item, dict)]
    sub_cluster_id_to_index = {item.get("id"): index for index, item in enumerate(sub_clusters)}
    avatar_cdn = str(atlas.get("meta", {}).get("avatar_cdn") or "")
    normalized_query = (query or "").strip().lower()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    if category_id is not None:
        if level == "micro":
            sub_index = sub_cluster_id_to_index.get(category_id)
            channels = [item for item in channels if len(item) > 8 and item[8] == sub_index]
        else:
            channels = [item for item in channels if item[2] == category_id]

    if normalized_query:
        channels = [item for item in channels if _channel_matches_query(item, normalized_query, clusters, sub_clusters)]

    total = len(channels)
    channels = sorted(channels, key=lambda item: int(item[5] or 0), reverse=True)[offset : offset + limit]
    return total, [_channel_to_response(item, clusters, sub_clusters, avatar_cdn) for item in channels]


def atlas_channel_metadata_by_refs(atlas: dict[str, Any], refs: list[str]) -> dict[str, dict[str, Any]]:
    requested_refs = {_comparable_channel_ref(ref) for ref in refs if _comparable_channel_ref(ref)}
    if not requested_refs:
        return {}

    channels = [item for item in atlas.get("channels", []) if isinstance(item, list) and len(item) >= 8]
    clusters = {item.get("id"): item for item in atlas.get("clusters", []) if isinstance(item, dict)}
    sub_clusters = [item for item in atlas.get("sub_clusters", []) if isinstance(item, dict)]
    metadata_by_ref: dict[str, dict[str, Any]] = {}

    for item in channels:
        comparable_refs = _channel_comparable_refs(item)
        matched_refs = requested_refs.intersection(comparable_refs)
        if not matched_refs:
            continue

        cluster = clusters.get(item[2])
        sub_cluster = _sub_cluster_for_channel(item, sub_clusters)
        metadata = {
            "cluster_id": item[2] if isinstance(item[2], int) else None,
            "cluster_label": cluster.get("label") if cluster else None,
            "sub_cluster_id": sub_cluster.get("id") if sub_cluster else None,
            "sub_cluster_label": sub_cluster.get("label") if sub_cluster else None,
        }
        for ref in matched_refs:
            metadata_by_ref[ref] = metadata

    return metadata_by_ref


def _channel_comparable_refs(item: list[Any]) -> set[str]:
    refs: set[str] = set()
    username = str(item[3] or "").strip().lstrip("@")
    if username:
        refs.update(
            {
                _comparable_channel_ref(username),
                _comparable_channel_ref(f"@{username}"),
                _comparable_channel_ref(f"https://t.me/{username}"),
                _comparable_channel_ref(f"t.me/{username}"),
            }
        )
    channel_id = str(item[7] or "").strip()
    if channel_id:
        refs.update(
            {
                _comparable_channel_ref(channel_id),
                _comparable_channel_ref(f"https://t.me/{channel_id}"),
                _comparable_channel_ref(f"t.me/{channel_id}"),
            }
        )
    return {ref for ref in refs if ref}


def _comparable_channel_ref(value: str) -> str:
    ref = value.strip().lower()
    if ref.startswith("https://t.me/"):
        ref = ref.removeprefix("https://t.me/")
    elif ref.startswith("http://t.me/"):
        ref = ref.removeprefix("http://t.me/")
    elif ref.startswith("t.me/"):
        ref = ref.removeprefix("t.me/")
    return ref.strip("/").lstrip("@")


def _channel_matches_query(
    item: list[Any],
    query: str,
    clusters: dict[int, dict[str, Any]],
    sub_clusters: list[dict[str, Any]],
) -> bool:
    cluster = clusters.get(item[2])
    sub_cluster = _sub_cluster_for_channel(item, sub_clusters)
    haystack = " ".join(
        str(value or "")
        for value in [
            item[3],
            item[4],
            cluster.get("label") if cluster else "",
            sub_cluster.get("label") if sub_cluster else "",
        ]
    ).lower()
    return query in haystack


def _channel_to_response(
    item: list[Any],
    clusters: dict[int, dict[str, Any]],
    sub_clusters: list[dict[str, Any]],
    avatar_cdn: str,
) -> dict[str, Any]:
    cluster = clusters.get(item[2])
    sub_cluster = _sub_cluster_for_channel(item, sub_clusters)
    username = str(item[3] or "").strip().lstrip("@") or None
    avatar_name = str(item[6] or "").strip()
    avatar_url = None
    if avatar_name:
        avatar_url = avatar_name if avatar_name.startswith("http") else f"{avatar_cdn}{avatar_name}{AVATAR_SIZE_QUERY}"
    return {
        "channel_id": str(item[7]),
        "username": username,
        "title": str(item[4] or username or item[7]),
        "participants_count": int(item[5] or 0),
        "cluster_id": item[2] if isinstance(item[2], int) else None,
        "cluster_label": cluster.get("label") if cluster else None,
        "sub_cluster_id": sub_cluster.get("id") if sub_cluster else None,
        "sub_cluster_label": sub_cluster.get("label") if sub_cluster else None,
        "avatar_url": avatar_url,
        "ref": f"@{username}" if username else f"https://t.me/{item[7]}",
    }


def _sub_cluster_for_channel(item: list[Any], sub_clusters: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(item) <= 8 or not isinstance(item[8], int) or item[8] >= len(sub_clusters):
        return None
    return sub_clusters[item[8]]
