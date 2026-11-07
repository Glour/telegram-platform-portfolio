from datetime import UTC, datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TelegramAccount(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_accounts"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    phone_hash: Mapped[str | None] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(160))
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    import_type: Mapped[str] = mapped_column(String(24), nullable=False)
    proxy_id: Mapped[str | None] = mapped_column(ForeignKey("proxies.id"))
    proxy_required: Mapped[bool] = mapped_column(default=True, nullable=False)
    health_score: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    warmup_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warmup_status: Mapped[str] = mapped_column(String(32), default="not_started", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=1_000_000, nullable=False)
    daily_limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    def _restriction_metadata(self) -> dict:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        metadata = data.get("restriction_status")
        return metadata if isinstance(metadata, dict) else {}

    def _profile_metadata(self) -> dict:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        metadata = data.get("profile")
        return metadata if isinstance(metadata, dict) else {}

    def _session_import_metadata(self) -> dict:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        metadata = data.get("session_import")
        return metadata if isinstance(metadata, dict) else {}

    def _account_metadata(self) -> dict:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        metadata = data.get("account_metadata")
        return metadata if isinstance(metadata, dict) else {}

    @property
    def owner_attested(self) -> bool:
        from app.modules.telegram_safety.admission import owner_attested

        return owner_attested(self, "neurocommenting")

    @property
    def first_name(self) -> str | None:
        value = self._profile_metadata().get("first_name")
        return value if isinstance(value, str) and value else None

    @property
    def last_name(self) -> str | None:
        value = self._profile_metadata().get("last_name")
        return value if isinstance(value, str) and value else None

    @property
    def profile_photo_data_url(self) -> str | None:
        value = self._profile_metadata().get("photo_data_url")
        return value if isinstance(value, str) and value.startswith("data:image/") else None

    @property
    def is_premium(self) -> bool:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        candidates = [
            data.get("is_premium"),
            data.get("premium"),
            data.get("telegram_premium"),
            self._profile_metadata().get("is_premium"),
            self._profile_metadata().get("premium"),
            self._profile_metadata().get("telegram_premium"),
        ]
        return any(
            value is True or (isinstance(value, str) and value.lower() in {"true", "active", "premium", "yes"})
            for value in candidates
        )

    @property
    def phone(self) -> str | None:
        value = self._profile_metadata().get("phone")
        return value if isinstance(value, str) and value else None

    @property
    def import_batch_id(self) -> str | None:
        value = self._session_import_metadata().get("batch_id")
        return value if isinstance(value, str) and value else None

    @property
    def import_batch_size(self) -> int | None:
        value = self._session_import_metadata().get("batch_size")
        return value if isinstance(value, int) and value > 0 else None

    @property
    def import_batch_at(self) -> str | None:
        value = self._session_import_metadata().get("batch_at")
        return value if isinstance(value, str) and value else None

    @property
    def restriction_status(self) -> str | None:
        return self._restriction_metadata().get("status")

    @property
    def restriction_label(self) -> str | None:
        return self._restriction_metadata().get("label")

    @property
    def restriction_checked_at(self) -> str | None:
        return self._restriction_metadata().get("checked_at")

    @property
    def restriction_until_text(self) -> str | None:
        return self._restriction_metadata().get("until_text")

    @property
    def restriction_until_at(self) -> str | None:
        return self._restriction_metadata().get("until_at")

    @property
    def restriction_raw_text(self) -> str | None:
        return self._restriction_metadata().get("raw_text")

    @property
    def quality_score(self) -> dict | None:
        data = self.daily_limits if isinstance(self.daily_limits, dict) else {}
        quality = data.get("quality_score")
        return quality if isinstance(quality, dict) else None

    @property
    def origin(self) -> str | None:
        value = self._account_metadata().get("origin")
        if isinstance(value, str) and value:
            return value
        if self.import_type == "tdata":
            return "tdata"
        if self.import_type in {"session", "session_file"}:
            return "market_session"
        return None

    @property
    def claimed_age_days(self) -> int | None:
        return _positive_int_or_none(self._account_metadata().get("claimed_age_days"))

    @property
    def real_age_days(self) -> int | None:
        metadata = self._account_metadata()
        explicit_age = _positive_int_or_none(metadata.get("real_age_days"))
        real_created_at = _parse_dt(metadata.get("real_created_at"))
        if real_created_at:
            now = datetime.now(UTC)
            return max(0, (now - real_created_at).days)
        return explicit_age

    @property
    def system_age_days(self) -> int:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - created_at).days)

    @property
    def incubator_mode(self) -> bool:
        metadata = self._account_metadata()
        return metadata.get("incubator_mode") is True or self.warmup_status == "incubator"

    @property
    def incubator_until_at(self) -> str | None:
        value = self._account_metadata().get("incubator_until_at")
        return value if isinstance(value, str) and value else None

    @property
    def trust_age_bonus(self) -> int:
        age = self.real_age_days
        if age is None:
            age = self.claimed_age_days
        if age is None:
            age = self.system_age_days
        if age >= 90:
            return 18
        if age >= 60:
            return 12
        if age >= 30:
            return 7
        return 0

    @property
    def immortal_candidate(self) -> bool:
        if self.incubator_mode:
            return False
        if self.status not in {"active", "warming"}:
            return False
        if self.restriction_status in {"limited", "blocked", "frozen"}:
            return False
        age = self.real_age_days if self.real_age_days is not None else self.system_age_days
        manual_origin = self.origin in {"manual_phone_registered", "sms_service_manual"}
        return age >= 30 and manual_origin and bool(self.proxy_id)


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class TelegramSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_sessions"

    account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    encrypted_session_blob: Mapped[str] = mapped_column(Text, nullable=False)
    storage_type: Mapped[str] = mapped_column(String(32), default="database", nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)


class AccountHealthEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_health_events"

    account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_error: Mapped[dict | None] = mapped_column(JSON)


class AccountQualityCheck(IdMixin, TimestampMixin, Base):
    __tablename__ = "account_quality_checks"

    account_id: Mapped[str] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    rating: Mapped[float | None] = mapped_column(Float)
    potential_30d: Mapped[float | None] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(32))
    potential_label: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class RiskDecision(IdMixin, TimestampMixin, Base):
    __tablename__ = "risk_decisions"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("telegram_accounts.id"))
    module: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    limits: Mapped[dict | None] = mapped_column(JSON)


Index("ix_telegram_accounts_workspace_status", TelegramAccount.workspace_id, TelegramAccount.status)
Index("ix_telegram_accounts_workspace_username", TelegramAccount.workspace_id, TelegramAccount.username)
Index(
    "ix_telegram_accounts_workspace_proxy_status",
    TelegramAccount.workspace_id,
    TelegramAccount.proxy_id,
    TelegramAccount.status,
)
Index(
    "ix_telegram_accounts_workspace_warmup_status",
    TelegramAccount.workspace_id,
    TelegramAccount.warmup_status,
    TelegramAccount.status,
)
Index("ix_health_events_account_created", AccountHealthEvent.account_id, AccountHealthEvent.created_at)
Index("ix_quality_checks_account_created", AccountQualityCheck.account_id, AccountQualityCheck.created_at)
Index("ix_risk_decisions_workspace_created", RiskDecision.workspace_id, RiskDecision.created_at)
Index("ix_risk_decisions_account_created", RiskDecision.account_id, RiskDecision.created_at)
Index("ix_risk_decisions_module_decision", RiskDecision.module, RiskDecision.decision)
