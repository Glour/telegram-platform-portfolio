from typing import Protocol


class SourceLike(Protocol):
    id: str
    type: str
    comments_enabled: bool
    access_status: str
    source_metadata: dict | None


def source_metadata(source: SourceLike) -> dict:
    return source.source_metadata if isinstance(source.source_metadata, dict) else {}


def source_safety(source: SourceLike) -> dict[str, object]:
    metadata = source_metadata(source)
    manual_available = metadata.get("manual_status_override") is True and source.access_status == "available"
    reasons: list[str] = []
    if source.access_status == "blacklisted" or metadata.get("blacklisted") is True:
        reasons.append(str(metadata.get("blacklist_reason") or "source_blacklisted"))
    if source.access_status == "archived":
        reasons.append("archived")
    if not manual_available and (source.access_status.startswith("invalid_") or metadata.get("invalid") is True):
        reasons.append(str(metadata.get("invalid_reason") or source.access_status))
    if source.type != "channel":
        reasons.append("source_not_channel")
    if (
        not manual_available
        and not source.comments_enabled
        and source.access_status not in {"unknown", "pending_check"}
    ):
        reasons.append("comments_closed")
    if source.access_status in {"unknown", "pending_check", "failed", "blocked"}:
        reasons.append(f"source_status_{source.access_status}")
    if not manual_available and metadata.get("linked_discussion_resolved") is False:
        reasons.append("discussion_unresolved")

    critical_reasons = {
        "archived",
        "source_blacklisted",
        "blacklisted",
        "comments_closed",
        "source_comments_not_available",
        "source_not_accessible",
        "discussion_join_approval_required",
        "account_banned_in_source",
        "account_cannot_write_comments",
        "source_not_found",
        "source_not_channel",
        "invalid_comments_closed",
        "invalid_private",
        "invalid_join_request",
        "invalid_account_banned",
        "invalid_write_forbidden",
        "invalid_not_found",
        "invalid_not_channel",
    }
    risk_level = "low"
    if any(reason in critical_reasons for reason in reasons):
        risk_level = "critical"
    elif source.access_status in {"unknown", "pending_check"}:
        risk_level = "unknown"
    elif source.access_status in {"failed", "blocked"}:
        risk_level = "high"
    elif reasons:
        risk_level = "medium"

    return {
        "source_id": source.id,
        "source_risk_level": risk_level,
        "safe_for_new_accounts": risk_level == "low",
        "safe_for_trusted_accounts": risk_level in {"low", "medium"},
        "reasons": list(dict.fromkeys(reasons)),
        "recommended_action": source_recommended_action(risk_level, reasons),
    }


def source_recommended_action(risk_level: str, reasons: list[str]) -> str | None:
    if "source_blacklisted" in reasons or "blacklisted" in reasons:
        return "keep_blacklisted"
    if risk_level == "critical":
        return "archive_source"
    if any(reason.startswith("source_status_") for reason in reasons):
        return "check_source"
    if risk_level == "medium":
        return "review_source"
    return None
