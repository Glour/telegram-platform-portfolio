from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_WARMUP_SYSTEM_SOURCE_REFS: tuple[str, ...] = ()


def default_warmup_system_source_refs() -> str:
    return ",".join(DEFAULT_WARMUP_SYSTEM_SOURCE_REFS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "demo-platform telegram agents"
    app_env: str = "local"
    api_v1_prefix: str = "/api/v1"
    api_root_path: str = ""
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    secret_key: str = Field(default="change-me-in-production")
    telegram_operations_enabled: bool = False
    spambot_appeals_enabled: bool = False
    telegram_manual_workspaces: str = ""
    telegram_manual_proxy_donors: str = ""
    access_token_expire_minutes: int = 60 * 24 * 7
    frontend_base_url: str = "https://demo.invalid"
    tracking_public_base_url: str = "https://demo.invalid"
    internal_full_access_emails: str = ""
    market_admin_emails: str = ""

    database_url: str = "postgresql+asyncpg://tg_ai:tg_ai@localhost:5432/tg_ai"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    session_encryption_key: str | None = None
    openai_base_url: str = "http://openai-proxy:8787/v1"
    openai_model: str = "gpt-6-luna"
    openai_quality_model: str = "gpt-6.1-sol"
    openai_image_model: str = "gpt-image-2-medium"
    openai_proxy_bearer_token: str | None = None
    openai_timeout_seconds: int = 45
    openai_prompt_cache_retention: str | None = None

    telegram_alerts_enabled: bool = False
    telegram_alert_bot_token: str | None = None
    telegram_alert_chat_id: str | None = None
    operation_alert_min_interval_minutes: int = 30
    campaign_run_stale_minutes: int = 60
    live_listener_stale_minutes: int = 12

    telegram_mode: str = "scaffold"
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_request_timeout_seconds: int = 25
    telegram_channel_discovery_dialog_limit: int = 500
    telegram_channel_discovery_max_sources: int = 200
    telegram_channel_discovery_min_participants: int = 1000
    telegram_channel_discovery_active_days: int = 180
    telegram_auto_discovery_on_import: bool = False
    contact_broadcast_media_dir: str = "/app/data/contact-broadcasts"
    contact_broadcast_pilot_account_id: str = ""
    contact_broadcast_pilot_sender_tg_id: str = ""
    contact_broadcast_pilot_recipient_tg_id: str = ""
    contact_broadcast_bulk_enabled: bool = False
    contact_broadcast_bulk_workspace_ids: list[str] = []
    contact_broadcast_use_assigned_proxy: bool = False
    warmup_system_source_refs: str = Field(default_factory=default_warmup_system_source_refs)

    auth_email_from: str = "noreply@example.invalid"
    auth_email_verification_ttl_minutes: int = 60 * 24
    auth_debug_tokens: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_ssl: bool = False
    smtp_use_tls: bool = True
    contact_email_to: str | None = None

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None

    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None
    yookassa_api_url: str = "https://api.yookassa.ru/v3"
    yookassa_vat_code: int = 1
    nowpayments_api_key: str | None = None
    nowpayments_ipn_secret: str | None = None
    nowpayments_api_url: str = "https://api.nowpayments.io"
    payment_webhook_base_url: str | None = None

    channel_atlas_file: str = "/app/data/channel_atlas.json.gz"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
