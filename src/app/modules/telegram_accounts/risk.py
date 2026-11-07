from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.proxies.models import Proxy
from app.modules.telegram_accounts.models import TelegramAccount
from app.modules.telegram_safety.admission import owner_attested

OUTBOUND_MODULES = {
    "auto_join",
    "bulk_join_chats",
    "bulk_clone_profile",
    "bulk_copy_stories",
    "bulk_ai_profiles",
    "mass_reactions",
    "message_broadcast",
    "neurochatting",
    "neurocommenting",
    "profile_edit",
    "stories_copy",
    "warmup_balanced",
    "warmup_growth",
    "warmup_safe",
}
CAMPAIGN_MODULES = {"neurocommenting", "neurochatting", "mass_reactions", "message_broadcast", "auto_join"}
INCUBATOR_BLOCKED_MODULES = CAMPAIGN_MODULES | {"bulk_join_chats"}
LIGHT_MODULES = {"warmup_validation", "proxy_check", "read_only_parsing", "source_check"}
RESTRICTED_STATUSES = {"limited", "blocked", "frozen"}
BLOCKED_ACCOUNT_STATUSES = {
    "archived",
    "banned",
    "blocked",
    "deleted",
    "frozen",
    "held",
    "invalid",
    "quarantined",
    "spam_blocked",
}


@dataclass(frozen=True)
class AccountRiskProfile:
    risk_level: str
    score: int
    reasons: list[str] = field(default_factory=list)
    hard_blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommended_next_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_level": self.risk_level,
            "score": self.score,
            "reasons": self.reasons,
            "hard_blocks": self.hard_blocks,
            "warnings": self.warnings,
            "recommended_next_step": self.recommended_next_step,
        }


@dataclass(frozen=True)
class ModuleEligibility:
    module: str
    status: str
    reason: str | None = None
    message: str | None = None
    allowed_limits: dict[str, Any] | None = None
    risk_profile: AccountRiskProfile | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "allowed_limits": self.allowed_limits,
            "risk_profile": self.risk_profile.to_dict() if self.risk_profile else None,
        }


def build_account_risk_profile(
    account: TelegramAccount,
    *,
    proxy: Proxy | None = None,
    proxy_account_count: int | None = None,
    now: datetime | None = None,
) -> AccountRiskProfile:
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    hard_blocks: list[str] = []
    warnings: list[str] = []
    score = max(0, min(100, int(account.health_score or 0)))

    if account.restriction_status in RESTRICTED_STATUSES:
        reasons.append(f"telegram_restriction_{account.restriction_status}")
        hard_blocks.append(f"telegram_restriction_{account.restriction_status}")
        score = min(score, 10)

    if account.status in BLOCKED_ACCOUNT_STATUSES:
        reasons.append(f"account_status_{account.status}")
        hard_blocks.append(f"account_status_{account.status}")
        score = min(score, 15)

    if account.proxy_required and not account.proxy_id:
        reasons.append("proxy_missing")
        hard_blocks.append("proxy_missing")
        score = min(score, 20)

    if proxy and proxy.status not in {"active", "new"}:
        reasons.append(f"proxy_status_{proxy.status}")
        hard_blocks.append(f"proxy_status_{proxy.status}")
        score = min(score, 25)
    if (
        proxy
        and proxy_account_count is not None
        and proxy.max_accounts > 0
        and proxy_account_count > proxy.max_accounts
    ):
        reasons.append("proxy_overloaded")
        hard_blocks.append("proxy_overloaded")
        score = min(score, 25)

    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    quality = data.get("quality_score") if isinstance(data.get("quality_score"), dict) else {}
    if quality:
        warnings.append("quality_score_is_not_permission")
        potential = quality.get("potential_30d")
        if isinstance(potential, int | float) and potential < 5:
            reasons.append("low_30d_potential")
            warnings.append("low_30d_potential")
            score = min(score, 65)
    if data.get("last_flood_wait_at"):
        reasons.append("recent_flood_wait")
        warnings.append("recent_flood_wait")
        score = min(score, 60)
    if _recent_iso(data.get("last_action_limit_error_at"), now, timedelta(days=7)):
        reasons.append("recent_action_limit_error")
        hard_blocks.append("recent_action_limit_error")
        score = min(score, 40)
    if _recent_iso(data.get("last_reactions_too_many_at"), now, timedelta(days=7)):
        reasons.append("recent_reactions_too_many")
        hard_blocks.append("recent_reactions_too_many")
        score = min(score, 35)
    if int(data.get("telegram_connection_failed_count") or 0) >= 2:
        reasons.append("repeated_connection_failed")
        warnings.append("repeated_connection_failed")
        score = min(score, 55)

    if account.status == "new" or account.warmup_score < 80:
        reasons.append("not_fully_warmed")
        warnings.append("not_fully_warmed")
        score = min(score, 70)
    if account.incubator_mode:
        reasons.append("incubator_mode")
        warnings.append("incubator_mode")
        score = min(score, 65)

    risk_level = "low"
    if hard_blocks:
        risk_level = "critical"
    elif score < 45:
        risk_level = "high"
    elif score < 75 or warnings:
        risk_level = "medium"

    return AccountRiskProfile(
        risk_level=risk_level,
        score=score,
        reasons=list(dict.fromkeys(reasons)),
        hard_blocks=list(dict.fromkeys(hard_blocks)),
        warnings=list(dict.fromkeys(warnings)),
        recommended_next_step=_recommended_next_step(hard_blocks, warnings),
    )


def get_module_eligibility(
    account: TelegramAccount,
    module: str,
    *,
    proxy: Proxy | None = None,
    proxy_account_count: int | None = None,
    now: datetime | None = None,
) -> ModuleEligibility:
    profile = build_account_risk_profile(account, proxy=proxy, proxy_account_count=proxy_account_count, now=now)
    if account.incubator_mode and module in INCUBATOR_BLOCKED_MODULES:
        return ModuleEligibility(
            module=module,
            status="blocked",
            reason="incubator_mode",
            message="Аккаунт в инкубаторе: исходящие действия запрещены до ручного вывода из инкубатора.",
            risk_profile=profile,
        )
    hard_block = profile.hard_blocks[0] if profile.hard_blocks else None
    if hard_block and module in OUTBOUND_MODULES and _is_physical_module_block(hard_block):
        return ModuleEligibility(
            module=module,
            status="blocked",
            reason=hard_block,
            message=_eligibility_message(hard_block, module),
            risk_profile=profile,
        )
    if module == "warmup_validation" and hard_block in {
        "telegram_restriction_limited",
        "telegram_restriction_blocked",
        "telegram_restriction_frozen",
    }:
        return ModuleEligibility(
            module=module,
            status="blocked",
            reason=hard_block,
            message=_eligibility_message(hard_block, module),
            risk_profile=profile,
        )

    if module in OUTBOUND_MODULES and account.restriction_status != "clear" and not owner_attested(account, module):
        reason = f"telegram_restriction_{account.restriction_status or 'unknown'}"
        return ModuleEligibility(
            module=module,
            status="blocked",
            reason=reason,
            message=_eligibility_message(reason, module),
            risk_profile=profile,
        )

    if module in {"neurocommenting", "warmup_safe"} and ("not_fully_warmed" in profile.reasons):
        return ModuleEligibility(
            module=module,
            status="limited",
            reason="not_fully_warmed",
            message="Аккаунт еще не прогрет. Проверьте лимиты кампании перед запуском.",
            risk_profile=profile,
        )

    if module in {"bulk_clone_profile", "bulk_copy_stories", "bulk_ai_profiles"} and profile.risk_level != "low":
        reason = (
            profile.hard_blocks[0]
            if profile.hard_blocks
            else (profile.reasons[0] if profile.reasons else "risk_not_low")
        )
        return ModuleEligibility(
            module=module,
            status="limited" if not profile.hard_blocks else "blocked",
            reason=reason,
            message=(
                _eligibility_message(reason, module)
                if profile.hard_blocks
                else "Массовые профильные действия допустимы только на аккаунтах с низким риском."
            ),
            risk_profile=profile,
        )

    status = "limited" if profile.risk_level == "medium" and module not in LIGHT_MODULES else "allowed"
    return ModuleEligibility(
        module=module,
        status=status,
        reason=profile.warnings[0] if status == "limited" and profile.warnings else None,
        message="Разрешено с осторожностью." if status == "limited" else None,
        risk_profile=profile,
    )


def assert_module_allowed(account: TelegramAccount, module: str, *, proxy: Proxy | None = None) -> ModuleEligibility:
    eligibility = get_module_eligibility(account, module, proxy=proxy)
    admitted_pilot = (
        eligibility.status == "limited"
        and eligibility.risk_profile is not None
        and not eligibility.risk_profile.hard_blocks
        and (owner_attested(account, module) or _has_fresh_operator_admission(account))
    )
    if eligibility.status == "blocked" or (
        module in OUTBOUND_MODULES and eligibility.status != "allowed" and not admitted_pilot
    ):
        from app.core.errors import bad_request

        raise bad_request(eligibility.message or "Аккаунт сейчас не подходит для этого модуля.")
    return eligibility


def _has_fresh_operator_admission(account: TelegramAccount) -> bool:
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    metadata = data.get("restriction_status")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("status") != "clear" or metadata.get("evidence_source") != "operator_admission":
        return False
    checked_at = metadata.get("checked_at")
    if not isinstance(checked_at, str):
        return False
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if checked.tzinfo is None:
        return False
    return timedelta(0) <= datetime.now(UTC) - checked <= timedelta(hours=24)


def _is_physical_module_block(reason: str) -> bool:
    return (
        reason.startswith("telegram_restriction_")
        or reason.startswith("account_status_")
        or reason.startswith("proxy_status_")
        or reason in {"proxy_missing", "proxy_overloaded", "recent_action_limit_error", "recent_reactions_too_many"}
    )


def _recent_iso(value: object, now: datetime, window: timedelta) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now - parsed <= window


def _recommended_next_step(hard_blocks: list[str], warnings: list[str]) -> str | None:
    if any(item.startswith("telegram_restriction_") for item in hard_blocks):
        return "keep_in_quarantine_until_runtime_clear"
    if "proxy_missing" in hard_blocks:
        return "assign_proxy"
    if any(item.startswith("proxy_status_") for item in hard_blocks):
        return "fix_or_replace_proxy"
    if "proxy_overloaded" in hard_blocks:
        return "reduce_proxy_accounts"
    if "recent_action_limit_error" in hard_blocks:
        return "pause_account"
    if "recent_reactions_too_many" in hard_blocks:
        return "pause_account"
    if "not_fully_warmed" in warnings:
        return "run_safe_warmup"
    if "incubator_mode" in warnings:
        return "keep_in_incubator"
    return None


def _eligibility_message(reason: str, module: str) -> str:
    if reason == "telegram_restriction_unknown":
        return "Статус ограничений Telegram неизвестен. Исходящие модули запрещены до ручного допуска."
    if reason.startswith("telegram_restriction_"):
        return (
            "У аккаунта есть Telegram-ограничение по фактическим событиям. "
            "Исходящие модули запрещены до снятия ограничения."
        )
    if reason == "proxy_missing":
        return "Аккаунт без прокси: назначьте уникальный прокси перед запуском."
    if reason.startswith("proxy_status_"):
        return "Прокси аккаунта сейчас не активен. Проверьте или замените прокси."
    if reason == "proxy_overloaded":
        return "Прокси перегружен сверх max_accounts. Разнесите аккаунты перед запуском."
    if reason == "recent_action_limit_error":
        return "Недавно был лимит Telegram/Flood/PeerFlood/ReactionsTooMany. Аккаунт нужно поставить на паузу."
    if reason == "account_status_frozen":
        return "Аккаунт заморожен и не подходит для активных действий."
    if reason == "incubator_mode":
        return "Аккаунт в инкубаторе: кампании запрещены до ручного вывода из инкубатора."
    if reason.startswith("account_status_"):
        return f"Аккаунт не подходит для модуля {module}: текущий статус запрещает активные действия."
    return "Аккаунт сейчас не подходит для этого модуля."
