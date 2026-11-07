"""Owner attestation is an admission basis, never a Telegram restriction check."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

ATTESTABLE_REASONS = {
    None,
    "admission_required",
    "historical_account_requires_operator_admission",
    "owner_attested_admission",
    "operator_paused",
    "operator_admission",
}


def owner_attested(account: Any, module: str | None = None) -> bool:
    data = getattr(account, "daily_limits", None) or {}
    admission = data.get("owner_admission", {})
    return bool(
        isinstance(admission, dict)
        and admission.get("basis") == "owner_attestation"
        and admission.get("account_id") == account.id
        and admission.get("workspace_id") == account.workspace_id
        and admission.get("actor_id")
        and admission.get("approved_at")
        and admission.get("active") is True
        and (module is None or module in admission.get("modules", []))
        and (account.restriction_status or "unknown") in {"unknown", "clear"}
        and account.status
        not in {"blocked", "banned", "frozen", "spam_blocked", "quarantined", "deleted", "archived", "invalid"}
    )


def owner_join_limits(account: Any, at: datetime) -> tuple[int, int]:
    """Temporary, identity-bound owner pacing; default limits resume at expiry."""
    scoped = replacement_join_limits(account, at)
    if scoped is not None:
        return scoped
    default = (2, 1801)
    if not owner_attested(account, "auto_join"):
        return default
    admission = account.daily_limits["owner_admission"]
    override = admission.get("join_rate_override", {})
    try:
        start = datetime.fromisoformat(override["approved_at"]).astimezone(UTC)
        end = datetime.fromisoformat(override["expires_at"]).astimezone(UTC)
        if (
            override.get("basis") != "owner_request"
            or override.get("account_id") != account.id
            or override.get("workspace_id") != account.workspace_id
            or override.get("actor_id") != admission["actor_id"]
            or not start <= at.astimezone(UTC) < end
            or not 0 < (end - start).total_seconds() <= 3600
        ):
            return default
        limit, interval = int(override["hour_limit"]), int(override["min_interval_seconds"])
        return (limit, interval) if 2 <= limit <= 30 and 90 <= interval <= 1801 else default
    except (KeyError, TypeError, ValueError):
        return default


# Queue-specific pacing never changes the account's normal admission or limits.

_replacement_pacing = ContextVar("replacement_join_pacing", default=None)


@contextmanager
def replacement_pacing_scope(pacing):
    token = _replacement_pacing.set(pacing)
    try:
        yield
    finally:
        _replacement_pacing.reset(token)


def replacement_join_limits(account: Any, at: datetime) -> tuple[int, int] | None:
    context = _replacement_pacing.get()
    saved = (getattr(account, "daily_limits", None) or {}).get("replacement_join_pacing")
    if not isinstance(context, dict) or not isinstance(saved, dict) or context != saved:
        return None
    try:
        start = datetime.fromisoformat(saved["approved_at"]).astimezone(UTC)
        end = datetime.fromisoformat(saved["expires_at"]).astimezone(UTC)
        interval = int(saved["min_interval_seconds"])
        if (
            saved["account_id"] != account.id
            or saved["workspace_id"] != account.workspace_id
            or not saved["actor_id"]
            or not saved["campaign_id"]
            or not saved["replacement_id"]
            or not start <= at.astimezone(UTC) < end
            or not 0 < (end - start).total_seconds() <= 3600
            or not 60 <= interval <= 86400
        ):
            return None
        return max(1, (3600 + interval - 1) // interval), interval
    except (KeyError, ValueError, TypeError):
        return None
