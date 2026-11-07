from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.telegram_gateway.contracts import (
    TelegramAccountInfo,
    TelegramChatMessageInfo,
    TelegramCloneProfileResult,
    TelegramCommentInfo,
    TelegramDialogSourceInfo,
    TelegramPostInfo,
    TelegramProfileInfo,
    TelegramSendResult,
    TelegramSourceAccessResult,
    TelegramSourceInfo,
)

if TYPE_CHECKING:
    from app.modules.sources.models import TelegramMessage, TelegramSource
    from app.modules.telegram_accounts.models import TelegramAccount


class TelegramGatewayPort(Protocol):
    async def check_real_account(self, session: AsyncSession, account: "TelegramAccount") -> TelegramAccountInfo: ...

    async def get_account_profile(self, session: AsyncSession, account: "TelegramAccount") -> TelegramProfileInfo: ...

    async def clone_account_profile(
        self,
        session: AsyncSession,
        target_account: "TelegramAccount",
        *,
        source_ref: str | None = None,
        source_account: "TelegramAccount | None" = None,
        copy_name: bool = True,
        copy_bio: bool = True,
        copy_photo: bool = True,
        copy_stories: bool = False,
        pin_stories: bool = False,
        delete_old_stories: bool = False,
        story_limit: int = 3,
    ) -> TelegramCloneProfileResult: ...

    async def inspect_source_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source_ref: str,
    ) -> TelegramSourceInfo: ...

    async def ensure_source_access_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
    ) -> TelegramSourceAccessResult: ...

    async def fetch_posts_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
        *,
        limit: int = 20,
    ) -> list[TelegramPostInfo]: ...

    async def fetch_post_comments_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
        message: "TelegramMessage",
        *,
        limit: int = 100,
    ) -> list[TelegramCommentInfo]: ...

    async def fetch_chat_messages_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
        *,
        limit: int = 50,
    ) -> list[TelegramChatMessageInfo]: ...

    async def send_comment_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
        message: "TelegramMessage",
        text: str,
    ) -> TelegramSendResult: ...

    async def send_chat_reply_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        source: "TelegramSource",
        message_id: str,
        text: str,
    ) -> TelegramSendResult: ...

    async def discover_comment_sources_with_account(
        self,
        session: AsyncSession,
        account: "TelegramAccount",
        *,
        limit: int = 100,
    ) -> list[TelegramDialogSourceInfo]: ...
