from pydantic import BaseModel, Field


class AiProfileCreate(BaseModel):
    name: str
    tone: str | None = None
    language: str = "ru"
    system_prompt: str
    safety_prompt: str | None = None
    model: str = "gpt-6-luna"
    temperature: str = "0.7"


class AiProfileResponse(BaseModel):
    id: str
    name: str
    tone: str | None
    language: str
    model: str

    model_config = {"from_attributes": True}


class CommentPromptCreate(BaseModel):
    name: str
    ai_profile_id: str | None = None
    prompt_text: str
    product_context: str | None = None
    language: str = "ru"
    max_length: int = Field(default=300, ge=40, le=1000)
    blacklist_words: list[str] = Field(default_factory=list)


class CommentPromptResponse(BaseModel):
    id: str
    name: str
    ai_profile_id: str | None
    prompt_text: str
    language: str
    max_length: int
    status: str
    version: int

    model_config = {"from_attributes": True}


class CommentPreviewRequest(BaseModel):
    profile_id: str | None = None
    comment_prompt_template_id: str
    post_text: str
    channel_title: str | None = None


class AiGenerationResponse(BaseModel):
    id: str
    output_text: str
    moderation_status: str

    model_config = {"from_attributes": True}
