from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.account_trust.models import AccountRiskFactor, AccountTrustSnapshot
from app.modules.account_trust.schemas import AccountTrustSummaryResponse
from app.modules.proxies.models import Proxy, ProxyCheck
from app.modules.proxies.service import active_proxy_account_count
from app.modules.telegram_accounts.models import AccountHealthEvent, AccountQualityCheck, TelegramAccount

TRUST_SOURCE_VERSION = "trust_v1"
OUTBOUND_MODULES = [
    "neurocommenting",
    "neurochatting",
    "mass_reactions",
    "message_broadcast",
    "auto_join",
    "profile_edit",
]
TECHNICAL_MODULES = ["warmup_validation", "source_check", "read_only_parsing"]


@dataclass(frozen=True)
class TrustFactor:
    key: str
    group: str
    label: str
    delta: int
    severity: str = "info"
    hard_block: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "delta": self.delta,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass(frozen=True)
class AccountSignalSet:
    account: TelegramAccount
    proxy: Proxy | None = None
    proxy_account_count: int | None = None
    latest_proxy_check: ProxyCheck | None = None
    recent_health_events: list[AccountHealthEvent] = field(default_factory=list)
    latest_quality_check: AccountQualityCheck | None = None
    batch_accounts_count: int = 0
    batch_limited_count: int = 0
    batch_invalid_count: int = 0


async def collect_account_signals(
    session: AsyncSession,
    account: TelegramAccount,
) -> AccountSignalSet:
    proxy = await _account_proxy(session, account)
    proxy_account_count = await active_proxy_account_count(session, account.workspace_id, proxy.id) if proxy else None
    latest_proxy_check = await _latest_proxy_check(session, account)
    recent_events = await _recent_health_events(session, account)
    latest_quality_check = await _latest_quality_check(session, account)
    batch_count, batch_limited, batch_invalid = await _batch_outcomes(session, account)
    return AccountSignalSet(
        account=account,
        proxy=proxy,
        proxy_account_count=proxy_account_count,
        latest_proxy_check=latest_proxy_check,
        recent_health_events=recent_events,
        latest_quality_check=latest_quality_check,
        batch_accounts_count=batch_count,
        batch_limited_count=batch_limited,
        batch_invalid_count=batch_invalid,
    )


def score_account(signals: AccountSignalSet, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    factors = _account_factors(signals, now)
    score = 60 + sum(factor.delta for factor in factors)
    caps = _score_caps(signals, factors, now)
    if caps:
        score = min(score, min(caps.values()))
    score = max(0, min(100, int(round(score))))
    safety_status = _safety_status(signals, factors)
    if safety_status == "unknown" and not any(factor.hard_block for factor in factors):
        score = max(score, 40)
    label = _trust_label(score)
    confidence = _confidence(signals, now)
    survival_7d, survival_30d = _survival_forecast(score, signals, factors, now)
    recommendations = _recommendations(safety_status, score, confidence)
    positive = [factor for factor in factors if factor.delta > 0]
    risk = [factor for factor in factors if factor.delta < 0]
    hard_blocks = [factor for factor in factors if factor.hard_block]
    return {
        "score_100": score,
        "score_10": round(score / 10, 1),
        "label": label,
        "confidence": confidence,
        "safety_status": safety_status,
        "survival_7d": survival_7d,
        "survival_30d": survival_30d,
        "median_lifespan_days": _median_lifespan(score, safety_status),
        "similar_sample_size": max(0, signals.batch_accounts_count),
        "factor_scores": _factor_scores(factors),
        "risk_factors": [factor.to_dict() for factor in risk],
        "positive_factors": [factor.to_dict() for factor in positive],
        "hard_blocks": [factor.to_dict() for factor in hard_blocks],
        "recommendations": recommendations,
        "source_version": TRUST_SOURCE_VERSION,
    }


async def recompute_account_trust(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    reason: str,
) -> AccountTrustSnapshot:
    signals = await collect_account_signals(session, account)
    scored = score_account(signals)
    snapshot = AccountTrustSnapshot(
        workspace_id=account.workspace_id,
        telegram_account_id=account.id,
        score_100=scored["score_100"],
        score_10=scored["score_10"],
        label=scored["label"],
        confidence=scored["confidence"],
        safety_status=scored["safety_status"],
        survival_7d=scored["survival_7d"],
        survival_30d=scored["survival_30d"],
        median_lifespan_days=scored["median_lifespan_days"],
        similar_sample_size=scored["similar_sample_size"],
        factor_scores=scored["factor_scores"],
        risk_factors=scored["risk_factors"],
        positive_factors=scored["positive_factors"],
        hard_blocks=scored["hard_blocks"],
        recommendations={**scored["recommendations"], "reason": reason},
        source_version=TRUST_SOURCE_VERSION,
    )
    session.add(snapshot)
    await session.flush()
    for factor in [*scored["risk_factors"], *scored["positive_factors"]]:
        session.add(
            AccountRiskFactor(
                workspace_id=account.workspace_id,
                telegram_account_id=account.id,
                factor_key=str(factor["key"]),
                factor_group=str(factor["group"]),
                severity=str(factor["severity"]),
                score_delta=int(factor["delta"]),
                source_type=reason,
                source_id=snapshot.id,
                details=factor.get("details") or {},
            )
        )
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def latest_account_trust_snapshot(
    session: AsyncSession,
    workspace_id: str,
    account_id: str,
) -> AccountTrustSnapshot | None:
    result = await session.execute(
        select(AccountTrustSnapshot)
        .where(
            AccountTrustSnapshot.workspace_id == workspace_id,
            AccountTrustSnapshot.telegram_account_id == account_id,
        )
        .order_by(AccountTrustSnapshot.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def ensure_account_trust_snapshot(
    session: AsyncSession,
    account: TelegramAccount,
    *,
    reason: str,
    max_age: timedelta = timedelta(hours=24),
) -> AccountTrustSnapshot:
    snapshot = await latest_account_trust_snapshot(session, account.workspace_id, account.id)
    if snapshot is None:
        return await recompute_account_trust(session, account, reason=reason)
    created_at = snapshot.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - created_at > max_age:
        return await recompute_account_trust(session, account, reason=reason)
    return snapshot


async def account_trust_history(
    session: AsyncSession,
    workspace_id: str,
    account_id: str,
    *,
    limit: int = 50,
) -> list[AccountTrustSnapshot]:
    result = await session.execute(
        select(AccountTrustSnapshot)
        .where(
            AccountTrustSnapshot.workspace_id == workspace_id,
            AccountTrustSnapshot.telegram_account_id == account_id,
        )
        .order_by(AccountTrustSnapshot.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def trust_summary(session: AsyncSession, workspace_id: str) -> AccountTrustSummaryResponse:
    accounts_result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status.not_in(["archived", "deleted"]),
        )
    )
    accounts = list(accounts_result.scalars().all())
    safety_statuses: dict[str, int] = {}
    labels: dict[str, int] = {}
    confidence: dict[str, int] = {}
    scores: list[int] = []
    outbound_blocked = 0
    needs_runtime_review = 0
    for account in accounts:
        snapshot = await ensure_account_trust_snapshot(session, account, reason="trust_summary")
        safety_status = snapshot.safety_status
        label = snapshot.label
        confidence_label = snapshot.confidence
        score = snapshot.score_100
        recommendations = snapshot.recommendations
        safety_statuses[safety_status] = safety_statuses.get(safety_status, 0) + 1
        labels[label] = labels.get(label, 0) + 1
        confidence[confidence_label] = confidence.get(confidence_label, 0) + 1
        scores.append(score)
        if recommendations.get("blocked_modules"):
            outbound_blocked += 1
        if recommendations.get("next_best_action") == "runtime_review_before_launch":
            needs_runtime_review += 1
    return AccountTrustSummaryResponse(
        total=len(accounts),
        safety_statuses=safety_statuses,
        labels=labels,
        confidence=confidence,
        avg_score=round(sum(scores) / len(scores), 1) if scores else None,
        outbound_blocked=outbound_blocked,
        needs_runtime_review=needs_runtime_review,
    )


def _account_factors(signals: AccountSignalSet, now: datetime) -> list[TrustFactor]:
    account = signals.account
    data = account.daily_limits if isinstance(account.daily_limits, dict) else {}
    factors: list[TrustFactor] = []
    restriction = account.restriction_status or "unknown"
    checked_at = _parse_dt(account.restriction_checked_at)
    if restriction == "clear" and checked_at and now - checked_at <= timedelta(hours=72):
        factors.append(
            _factor(
                "restriction_clear_recent",
                "restriction_history",
                "Нет runtime-ограничений <= 72ч",
                10,
            )
        )
    elif restriction in {"limited", "blocked", "frozen"}:
        delta = -50 if restriction == "limited" else -60
        factors.append(
            _factor(
                f"restriction_{restriction}",
                "restriction_history",
                f"Telegram restriction: {restriction}",
                delta,
                severity="critical",
                hard_block=True,
            )
        )
    else:
        factors.append(
            _factor(
                "restriction_unknown",
                "restriction_history",
                "Нет свежего runtime-статуса перед боевой нагрузкой",
                -10,
                severity="warning",
            )
        )

    if account.status in {"invalid", "blocked", "deleted", "banned", "frozen", "spam_blocked"}:
        factors.append(
            _factor(
                f"account_status_{account.status}",
                "session_integrity",
                f"Статус аккаунта: {account.status}",
                -35,
                severity="critical",
                hard_block=True,
            )
        )
    elif account.status == "quarantined":
        factors.append(
            _factor(
                "account_quarantined",
                "restriction_history",
                "Аккаунт в карантине",
                -25,
                severity="critical",
                hard_block=True,
            )
        )

    import_at = _parse_dt(account.import_batch_at) or _aware_dt(account.created_at)
    account_age = now - import_at
    real_age_days = account.real_age_days
    claimed_age_days = account.claimed_age_days
    system_age_days = account.system_age_days
    trust_age_days = real_age_days if real_age_days is not None else claimed_age_days
    if trust_age_days is None:
        trust_age_days = system_age_days
    if trust_age_days >= 90:
        factors.append(
            _factor(
                "account_age_90d",
                "account_origin",
                "Возраст аккаунта 90+ дней",
                18,
                details={"age_days": trust_age_days},
            )
        )
    elif trust_age_days >= 60:
        factors.append(
            _factor(
                "account_age_60d",
                "account_origin",
                "Возраст аккаунта 60+ дней",
                12,
                details={"age_days": trust_age_days},
            )
        )
    elif trust_age_days >= 30:
        factors.append(
            _factor(
                "account_age_30d",
                "account_origin",
                "Возраст аккаунта 30+ дней",
                7,
                details={"age_days": trust_age_days},
            )
        )
    if account.origin in {"manual_phone_registered", "sms_service_manual"}:
        factors.append(
            _factor(
                f"origin_{account.origin}",
                "account_origin",
                "Ручная регистрация/первичная история",
                8,
            )
        )
    elif account.origin in {"market_session", "tdata"}:
        factors.append(
            _factor(
                f"origin_{account.origin}",
                "account_origin",
                "Аккаунт из внешней сессии",
                -3,
                severity="warning",
            )
        )
    if account.incubator_mode:
        factors.append(
            _factor(
                "incubator_mode",
                "warmup_quality",
                "Аккаунт находится в инкубаторе и не должен идти в кампании",
                -5,
                severity="warning",
            )
        )
    if account_age >= timedelta(days=7):
        factors.append(
            _factor(
                "account_age_7d",
                "account_origin",
                "Аккаунт в системе 7+ дней",
                10,
            )
        )
    elif account_age < timedelta(hours=24):
        factors.append(
            _factor(
                "new_import_under_24h",
                "account_origin",
                "Импорт меньше 24 часов назад",
                -15,
                severity="warning",
            )
        )

    if account.warmup_status in {"completed", "succeeded", "ready"} and account.warmup_score >= 80:
        factors.append(
            _factor(
                "warmup_completed_clean",
                "warmup_quality",
                "Прогрев завершен без явных ошибок",
                10,
            )
        )
    elif account.warmup_status in {"not_started", "paused_by_risk", "quarantined"} or account.warmup_score < 50:
        factors.append(_factor("warmup_not_ready", "warmup_quality", "Прогрев не завершен", -8, severity="warning"))

    if account.proxy_required and not account.proxy_id:
        factors.append(
            _factor(
                "proxy_missing",
                "proxy_reputation",
                "Не назначен обязательный прокси",
                -20,
                severity="critical",
                hard_block=True,
            )
        )
    if signals.proxy:
        proxy = signals.proxy
        if proxy.telegram_connectivity_status in {"ok", "active"}:
            factors.append(
                _factor(
                    "proxy_telegram_ok",
                    "proxy_reputation",
                    "Прокси подключается к Telegram",
                    8,
                )
            )
        elif proxy.telegram_connectivity_status in {"failed", "error", "blocked"} or proxy.status in {
            "failed",
            "blocked",
        }:
            factors.append(
                _factor(
                    "proxy_telegram_failed",
                    "proxy_reputation",
                    "Прокси не проходит Telegram connect",
                    -20,
                    severity="critical",
                    hard_block=True,
                )
            )
        if proxy.proxy_class in {"static_residential", "isp", "mobile"}:
            factors.append(
                _factor(
                    "proxy_class_safe",
                    "proxy_reputation",
                    f"Класс прокси: {proxy.proxy_class}",
                    5,
                )
            )
        if proxy.exclusivity == "dedicated" and (signals.proxy_account_count or 0) <= 1:
            factors.append(
                _factor(
                    "proxy_dedicated_1to1",
                    "proxy_reputation",
                    "Dedicated прокси 1:1",
                    5,
                )
            )
        if (
            signals.proxy_account_count is not None
            and proxy.max_accounts > 0
            and signals.proxy_account_count > proxy.max_accounts
        ):
            factors.append(
                _factor(
                    "proxy_overloaded",
                    "proxy_reputation",
                    "На прокси больше аккаунтов, чем разрешено",
                    -15,
                    severity="critical",
                    hard_block=True,
                )
            )
    if signals.latest_proxy_check:
        latency = signals.latest_proxy_check.telegram_latency_ms
        if latency is not None and latency <= 1200 and signals.latest_proxy_check.status == "ok":
            factors.append(
                _factor(
                    "proxy_latency_ok",
                    "proxy_reputation",
                    f"Telegram latency {latency} ms",
                    8,
                    details={"latency_ms": latency},
                )
            )
        elif latency is not None and latency > 2000:
            factors.append(
                _factor(
                    "proxy_latency_high",
                    "proxy_reputation",
                    f"Telegram latency {latency} ms",
                    -8,
                    severity="warning",
                    details={"latency_ms": latency},
                )
            )

    quality_raw = signals.latest_quality_check.raw if signals.latest_quality_check else {}
    if isinstance(quality_raw, dict) and quality_raw.get("successful_comments_without_errors"):
        factors.append(
            _factor(
                "successful_comments_without_errors",
                "behavior_history",
                "Комментарии проходили без ошибок",
                5,
            )
        )
    if data.get("last_flood_wait_at"):
        seconds = int(data.get("last_flood_wait_seconds") or 0)
        if seconds >= 3600:
            factors.append(
                _factor(
                    "flood_wait_3600",
                    "restriction_history",
                    "FloodWait >= 3600 секунд",
                    -25,
                    severity="critical",
                    hard_block=True,
                )
            )
        else:
            factors.append(
                _factor(
                    "flood_wait_60",
                    "restriction_history",
                    "Свежий FloodWait",
                    -15,
                    severity="critical",
                    hard_block=seconds >= 60,
                )
            )
    if data.get("last_action_limit_error_at"):
        factors.append(
            _factor(
                "fresh_peer_flood",
                "restriction_history",
                "Свежий PeerFlood/UserRestricted",
                -45,
                severity="critical",
                hard_block=True,
            )
        )
    if data.get("last_reactions_too_many_at"):
        factors.append(
            _factor(
                "reactions_too_many",
                "behavior_history",
                "ReactionsTooManyError",
                -10,
                severity="warning",
            )
        )
    connection_failed_count = int(data.get("telegram_connection_failed_count") or 0)
    if connection_failed_count:
        delta = -min(20, connection_failed_count * 5)
        factors.append(
            _factor(
                "telegram_connection_failed",
                "session_integrity",
                f"Ошибки подключения Telegram: {connection_failed_count}",
                delta,
                severity="warning",
                details={"count": connection_failed_count},
            )
        )
    if signals.batch_accounts_count >= 3:
        spamblock_rate = signals.batch_limited_count / signals.batch_accounts_count
        invalid_rate = signals.batch_invalid_count / signals.batch_accounts_count
        if spamblock_rate >= 0.5:
            factors.append(
                _factor(
                    "batch_survival_bad",
                    "batch_vendor_benchmark",
                    "Партия массово ограничивается",
                    -15,
                    severity="warning",
                )
            )
        elif spamblock_rate == 0 and invalid_rate == 0:
            factors.append(
                _factor(
                    "batch_survival_clean",
                    "batch_vendor_benchmark",
                    "Партия пока без ограничений",
                    5,
                )
            )
    return factors


def _score_caps(signals: AccountSignalSet, factors: list[TrustFactor], now: datetime) -> dict[str, int]:
    account = signals.account
    caps: dict[str, int] = {}
    if account.restriction_status == "limited":
        caps["limited"] = 35
    if account.restriction_status in {"blocked", "frozen"} or account.status in {
        "blocked",
        "invalid",
        "deleted",
        "banned",
        "frozen",
    }:
        caps["blocked_or_invalid"] = 10
    if any(factor.key in {"proxy_missing", "proxy_telegram_failed"} for factor in factors):
        caps["proxy_technical_only"] = 45
    if account.restriction_status in {None, "unknown"}:
        caps["unknown_before_outbound"] = 75
    import_at = _parse_dt(account.import_batch_at) or _aware_dt(account.created_at)
    declared_age = account.real_age_days if account.real_age_days is not None else account.claimed_age_days
    if now - import_at < timedelta(hours=24) and (declared_age is None or declared_age < 30):
        caps["new_import"] = 70
    if any(factor.key == "fresh_peer_flood" for factor in factors):
        caps["peer_flood"] = 35
    if any(factor.key == "flood_wait_3600" for factor in factors):
        caps["long_flood_wait"] = 40
    return caps


def _safety_status(signals: AccountSignalSet, factors: list[TrustFactor]) -> str:
    account = signals.account
    if account.status in {"invalid", "deleted", "banned", "frozen"}:
        return "invalid"
    if account.restriction_status == "frozen":
        return "frozen"
    if account.restriction_status == "blocked" or account.status == "blocked":
        return "blocked"
    if account.restriction_status == "limited":
        return "limited"
    if account.status == "quarantined" or any(
        factor.key in {"fresh_peer_flood", "flood_wait_3600"} for factor in factors
    ):
        return "quarantined"
    if any(factor.key in {"proxy_missing", "proxy_telegram_failed", "proxy_overloaded"} for factor in factors):
        return "technical_only"
    if account.restriction_status == "clear":
        return "clear"
    return "unknown"


def _recommendations(safety_status: str, score: int, confidence: str) -> dict[str, Any]:
    if safety_status in {"limited", "blocked", "frozen", "invalid", "quarantined"}:
        return {
            "next_best_action": "pause_or_replace_account",
            "allowed_modules": [],
            "blocked_modules": OUTBOUND_MODULES,
            "limits": {},
        }
    if safety_status == "technical_only":
        return {
            "next_best_action": "fix_proxy_or_session_before_launch",
            "allowed_modules": TECHNICAL_MODULES,
            "blocked_modules": OUTBOUND_MODULES,
            "limits": {},
        }
    if safety_status == "unknown":
        return {
            "next_best_action": "runtime_review_before_launch",
            "allowed_modules": TECHNICAL_MODULES if score < 60 else [*TECHNICAL_MODULES, "warmup_safe"],
            "blocked_modules": OUTBOUND_MODULES,
            "limits": {"warmup_mode": "validation" if score < 60 else "safe"},
        }
    if score < 60:
        return {
            "next_best_action": "safe_warmup_before_outbound",
            "allowed_modules": TECHNICAL_MODULES,
            "blocked_modules": OUTBOUND_MODULES,
            "limits": {},
        }
    if score < 75:
        return {
            "next_best_action": "soft_launch_low_daily_limits",
            "allowed_modules": [*TECHNICAL_MODULES, "warmup_safe", "neurocommenting"],
            "blocked_modules": ["mass_reactions", "auto_join"],
            "limits": {"neurocommenting_per_day": 3},
        }
    return {
        "next_best_action": "allowed_with_account_limits",
        "allowed_modules": [*TECHNICAL_MODULES, *OUTBOUND_MODULES],
        "blocked_modules": [],
        "limits": {"neurocommenting_per_day": 5 if score < 90 else 10, "confidence": confidence},
    }


def _survival_forecast(
    score: int,
    signals: AccountSignalSet,
    factors: list[TrustFactor],
    now: datetime,
) -> tuple[float, float]:
    short_penalty = 0.0
    long_penalty = 0.0
    if signals.account.restriction_status in {None, "unknown"}:
        short_penalty += 0.08
    import_at = _parse_dt(signals.account.import_batch_at) or _aware_dt(signals.account.created_at)
    if now - import_at < timedelta(hours=24):
        short_penalty += 0.08
    if any(factor.hard_block for factor in factors):
        short_penalty += 0.20
    if signals.proxy and signals.proxy.proxy_class in {"datacenter", "dc"}:
        long_penalty += 0.10
    if signals.batch_accounts_count >= 3 and signals.batch_limited_count / signals.batch_accounts_count >= 0.2:
        long_penalty += 0.12
    survival_7d = _clamp(score / 100 - short_penalty, 0.05, 0.98)
    survival_30d = _clamp(survival_7d - 0.08 - long_penalty, 0.03, 0.95)
    return round(survival_7d, 3), round(survival_30d, 3)


def _trust_label(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "medium"
    if score >= 40:
        return "risky"
    return "critical"


def _confidence(signals: AccountSignalSet, now: datetime) -> str:
    account = signals.account
    import_at = _parse_dt(account.import_batch_at) or _aware_dt(account.created_at)
    age = now - import_at
    if age >= timedelta(days=7) and account.restriction_status == "clear" and account.warmup_score >= 80:
        return "high"
    if age >= timedelta(days=1) and signals.proxy and signals.latest_proxy_check:
        return "medium"
    return "low"


def _median_lifespan(score: int, safety_status: str) -> int | None:
    if safety_status in {"blocked", "invalid"}:
        return 0
    if score >= 90:
        return 90
    if score >= 75:
        return 45
    if score >= 60:
        return 21
    if score >= 40:
        return 7
    return 1


def _factor_scores(factors: list[TrustFactor]) -> dict[str, int]:
    scores = {
        "session_integrity": 0,
        "proxy_reputation": 0,
        "account_origin": 0,
        "profile_naturalness": 0,
        "warmup_quality": 0,
        "behavior_history": 0,
        "restriction_history": 0,
        "source_safety": 0,
        "batch_vendor_benchmark": 0,
    }
    for factor in factors:
        scores[factor.group] = scores.get(factor.group, 0) + factor.delta
    return scores


async def _account_proxy(session: AsyncSession, account: TelegramAccount) -> Proxy | None:
    if not account.proxy_id:
        return None
    proxy = await session.get(Proxy, account.proxy_id)
    if proxy and proxy.workspace_id == account.workspace_id:
        return proxy
    return None


async def _latest_proxy_check(session: AsyncSession, account: TelegramAccount) -> ProxyCheck | None:
    if not account.proxy_id:
        return None
    result = await session.execute(
        select(ProxyCheck)
        .where(
            ProxyCheck.workspace_id == account.workspace_id,
            ProxyCheck.proxy_id == account.proxy_id,
        )
        .order_by(ProxyCheck.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _recent_health_events(session: AsyncSession, account: TelegramAccount) -> list[AccountHealthEvent]:
    result = await session.execute(
        select(AccountHealthEvent)
        .where(AccountHealthEvent.account_id == account.id)
        .order_by(AccountHealthEvent.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


async def _latest_quality_check(session: AsyncSession, account: TelegramAccount) -> AccountQualityCheck | None:
    result = await session.execute(
        select(AccountQualityCheck)
        .where(AccountQualityCheck.account_id == account.id)
        .order_by(AccountQualityCheck.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _batch_outcomes(session: AsyncSession, account: TelegramAccount) -> tuple[int, int, int]:
    batch_id = account.import_batch_id
    if not batch_id:
        return 0, 0, 0
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.workspace_id == account.workspace_id,
            TelegramAccount.status != "deleted",
        )
    )
    accounts = [item for item in result.scalars().all() if item.import_batch_id == batch_id]
    limited = sum(1 for item in accounts if item.restriction_status == "limited")
    invalid = sum(1 for item in accounts if item.status in {"invalid", "blocked", "deleted", "banned", "frozen"})
    return len(accounts), limited, invalid


def _factor(
    key: str,
    group: str,
    label: str,
    delta: int,
    *,
    severity: str = "info",
    hard_block: bool = False,
    details: dict[str, Any] | None = None,
) -> TrustFactor:
    return TrustFactor(
        key=key,
        group=group,
        label=label,
        delta=delta,
        severity=severity,
        hard_block=hard_block,
        details=details or {},
    )


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _aware_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
