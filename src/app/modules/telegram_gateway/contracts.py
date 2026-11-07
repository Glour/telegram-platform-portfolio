from dataclasses import dataclass
from datetime import datetime
from typing import Any


class TelegramGatewayError(Exception):
    def __init__(self, code: str, message: str, raw: dict[str, Any] | None = None) -> None:
        self.code = code
        self.raw = raw or {}
        super().__init__(message)


@dataclass(frozen=True)
class TelegramAccountInfo:
    tg_user_id: int
    username: str | None
    display_name: str
    phone: str | None


@dataclass(frozen=True)
class TelegramProfileInfo:
    tg_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    display_name: str
    about: str | None
    phone: str | None
    photo_data_url: str | None


@dataclass(frozen=True)
class TelegramProfilePhotoInfo:
    photo_id: str
    date: datetime | None
    photo_data_url: str | None
    is_current: bool


@dataclass(frozen=True)
class TelegramLoginCodeRequestResult:
    phone: str | None
    login_code: str | None
    message_text: str | None = None
    message_date: datetime | None = None


@dataclass(frozen=True)
class TelegramCloneProfileResult:
    profile: TelegramProfileInfo
    copied_fields: list[str]
    skipped_fields: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class TelegramStoryClonePayload:
    media: bytes
    media_kind: str
    file_name: str
    mime_type: str | None
    attributes: list[Any]
    caption: str | None
    entities: list[Any] | None
    noforwards: bool | None


@dataclass(frozen=True)
class TelegramRestrictionInfo:
    status: str
    label: str
    raw_text: str | None
    checked_at: str
    until_text: str | None
    until_at: str | None


@dataclass(frozen=True)
class TelegramSourceInfo:
    tg_id: int | None
    username: str | None
    title: str | None
    source_type: str
    comments_enabled: bool
    linked_discussion_tg_id: int | None
    linked_discussion_username: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TelegramDialogSourceInfo:
    tg_id: int | None
    username: str | None
    title: str | None
    source_type: str
    comments_enabled: bool
    linked_discussion_tg_id: int | None
    linked_discussion_username: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TelegramPostInfo:
    tg_message_id: str
    text: str | None
    published_at: Any
    views_count: int
    forwards_count: int
    replies_count: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class TelegramSendResult:
    tg_message_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class TelegramCommentInfo:
    tg_comment_message_id: str
    text: str
    author_tg_id: str | None
    published_at: Any
    raw: dict[str, Any]


@dataclass(frozen=True)
class TelegramChatMessageInfo:
    tg_message_id: str
    text: str | None
    published_at: Any
    sender_tg_id: str | None
    outgoing: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class TelegramSourceAccessResult:
    tg_id: int | None
    username: str | None
    title: str | None
    joined: bool
    join_attempted: bool
    comments_enabled: bool
    linked_discussion_tg_id: int | None
    linked_discussion_username: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TelegramInviteLinkStats:
    invite_link: str
    title: str | None
    usage_count: int | None
    requested_count: int | None
    revoked: bool | None
    expire_date: Any
    usage_limit: int | None
    raw: dict[str, Any]
