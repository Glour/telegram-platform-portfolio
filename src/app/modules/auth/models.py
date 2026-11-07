from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthToken(IdMixin, TimestampMixin, Base):
    __tablename__ = "auth_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthAccount(IdMixin, TimestampMixin, Base):
    __tablename__ = "oauth_accounts"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id", name="uq_oauth_accounts_provider_user"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    raw_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class OAuthState(IdMixin, TimestampMixin, Base):
    __tablename__ = "oauth_states"

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)


Index("ix_api_keys_workspace_status", ApiKey.workspace_id, ApiKey.status)
Index("ix_auth_tokens_user_purpose", AuthToken.user_id, AuthToken.purpose, AuthToken.consumed_at)
Index("ix_oauth_accounts_user_provider", OAuthAccount.user_id, OAuthAccount.provider)
Index("ix_oauth_states_provider_consumed", OAuthState.provider, OAuthState.consumed_at)
