from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.modules.telegram_accounts.models import TelegramAccount


@dataclass(frozen=True)
class BehaviorProtectionProfile:
    mode: str
    delay_multiplier: float
    typing_cpm_min: int | None
    typing_cpm_max: int | None
    profile_view_probability: int
    scroll_probability: int
    typo_probability: int
    delete_probability: int
    sleep_from_hour: int | None
    sleep_to_hour: int | None

    def snapshot(self) -> dict:
        return {
            "enabled": True,
            "mode": self.mode,
            "delay_multiplier": self.delay_multiplier,
            "typing_cpm_min": self.typing_cpm_min,
            "typing_cpm_max": self.typing_cpm_max,
            "profile_view_probability": self.profile_view_probability,
            "scroll_probability": self.scroll_probability,
            "typo_probability": self.typo_probability,
            "delete_probability": self.delete_probability,
            "sleep_from_hour": self.sleep_from_hour,
            "sleep_to_hour": self.sleep_to_hour,
        }


BEHAVIOR_PROTECTION_PROFILES: dict[str, BehaviorProtectionProfile] = {
    "conservative": BehaviorProtectionProfile(
        mode="conservative",
        delay_multiplier=1.5,
        typing_cpm_min=40,
        typing_cpm_max=60,
        profile_view_probability=90,
        scroll_probability=50,
        typo_probability=8,
        delete_probability=3,
        sleep_from_hour=1,
        sleep_to_hour=7,
    ),
    "balanced": BehaviorProtectionProfile(
        mode="balanced",
        delay_multiplier=1.0,
        typing_cpm_min=100,
        typing_cpm_max=150,
        profile_view_probability=70,
        scroll_probability=30,
        typo_probability=5,
        delete_probability=2,
        sleep_from_hour=None,
        sleep_to_hour=None,
    ),
    "fast": BehaviorProtectionProfile(
        mode="fast",
        delay_multiplier=0.7,
        typing_cpm_min=None,
        typing_cpm_max=None,
        profile_view_probability=30,
        scroll_probability=0,
        typo_probability=2,
        delete_probability=1,
        sleep_from_hour=None,
        sleep_to_hour=None,
    ),
}

NEW_OR_WARMING_STATUSES = {"new", "warming", "incubator"}


def behavior_protection_enabled(policy: dict | None) -> bool:
    if not policy:
        return True
    return policy.get("behavior_protection_enabled") is not False


def resolve_behavior_protection_profile(
    policy: dict | None,
    accounts: list[TelegramAccount] | tuple[TelegramAccount, ...] = (),
) -> BehaviorProtectionProfile | None:
    if not behavior_protection_enabled(policy):
        return None
    policy = policy or {}
    requested_mode = str(policy.get("behavior_protection_mode") or "balanced").strip().lower()
    if any(_account_requires_conservative_mode(account) for account in accounts):
        requested_mode = "conservative"
    return BEHAVIOR_PROTECTION_PROFILES.get(requested_mode) or BEHAVIOR_PROTECTION_PROFILES["balanced"]


def apply_behavior_delay_range(
    policy: dict | None,
    accounts: list[TelegramAccount] | tuple[TelegramAccount, ...],
    delay_min: int,
    delay_max: int,
    *,
    floor_min: int = 0,
    now: datetime | None = None,
) -> tuple[int, int, dict | None]:
    profile = resolve_behavior_protection_profile(policy, accounts)
    if not profile:
        return delay_min, max(delay_min, delay_max), {"enabled": False}
    effective_min = max(floor_min, int(round(max(0, delay_min) * profile.delay_multiplier)))
    effective_max = max(effective_min, int(round(max(delay_min, delay_max) * profile.delay_multiplier)))
    now = now or datetime.now(UTC)
    sleep_extra = _sleep_window_extra_seconds(policy or {}, profile, now)
    if sleep_extra > 0:
        effective_min += sleep_extra
        effective_max += sleep_extra
    snapshot = {
        **profile.snapshot(),
        "base_delay_seconds_min": delay_min,
        "base_delay_seconds_max": delay_max,
        "effective_delay_seconds_min": effective_min,
        "effective_delay_seconds_max": effective_max,
        "sleep_extra_seconds": sleep_extra,
        "timezone": _policy_timezone_name(policy or {}),
    }
    return effective_min, effective_max, snapshot


def _account_requires_conservative_mode(account: TelegramAccount) -> bool:
    if account.incubator_mode:
        return True
    if account.status in NEW_OR_WARMING_STATUSES:
        return True
    if account.restriction_status in {"limited", "blocked", "frozen"}:
        return True
    return False


def _sleep_window_extra_seconds(policy: dict, profile: BehaviorProtectionProfile, now: datetime) -> int:
    if profile.sleep_from_hour is None or profile.sleep_to_hour is None:
        return 0
    local_now = _local_datetime(policy, now)
    start_hour = profile.sleep_from_hour
    end_hour = profile.sleep_to_hour
    in_window = (
        start_hour <= local_now.hour < end_hour
        if start_hour < end_hour
        else local_now.hour >= start_hour or local_now.hour < end_hour
    )
    if not in_window:
        return 0
    wake_at = local_now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if wake_at <= local_now:
        wake_at += timedelta(days=1)
    return max(0, int((wake_at - local_now).total_seconds()))


def _local_datetime(policy: dict, now: datetime) -> datetime:
    value = now if now.tzinfo else now.replace(tzinfo=UTC)
    try:
        return value.astimezone(ZoneInfo(_policy_timezone_name(policy)))
    except ZoneInfoNotFoundError:
        return value.astimezone(UTC)


def _policy_timezone_name(policy: dict) -> str:
    value = policy.get("behavior_timezone") or policy.get("timezone") or "Europe/Moscow"
    return str(value).strip() or "Europe/Moscow"
