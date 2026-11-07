from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"))
    subscription_id: Mapped[str | None] = mapped_column(String(80))
    default_locale: Mapped[str] = mapped_column(String(12), default="ru", nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Moscow", nullable=False)


class WorkspaceMember(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


Index("ix_workspaces_status_plan", Workspace.status, Workspace.plan_id)
Index(
    "ix_workspace_members_workspace_role_status",
    WorkspaceMember.workspace_id,
    WorkspaceMember.role,
    WorkspaceMember.status,
)
Index("ix_workspace_members_user_status", WorkspaceMember.user_id, WorkspaceMember.status)
