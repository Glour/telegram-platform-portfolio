from datetime import datetime

from pydantic import BaseModel


class TelegramCommentResponse(BaseModel):
    id: str
    source_id: str
    post_message_id: str
    campaign_id: str | None
    telegram_account_id: str | None
    text: str
    status: str
    sent_at: datetime | None

    model_config = {"from_attributes": True}
