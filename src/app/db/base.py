from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)

    @declared_attr.directive
    def __tablename__(cls) -> str:
        name = cls.__name__
        chars: list[str] = []
        for index, char in enumerate(name):
            if char.isupper() and index:
                chars.append("_")
            chars.append(char.lower())
        return "".join(chars)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class IdMixin:
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))


JsonDict = dict[str, Any]
