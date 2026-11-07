from __future__ import annotations

from pydantic import BaseModel, Field


class AssistantCitation(BaseModel):
    title: str
    url: str | None = None
    chunk_id: str
    source_id: str


class AssistantConversationCreate(BaseModel):
    surface: str = Field(default="landing", pattern="^(landing|dashboard|help)$")
    initial_url: str | None = Field(default=None, max_length=500)
    visitor_id: str | None = Field(default=None, max_length=120)


class AssistantConversationCreateResponse(BaseModel):
    conversation_id: str
    welcome_message: str


class AssistantMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    citations: list[AssistantCitation] = Field(default_factory=list)
    created_at: str


class AssistantChatRequest(BaseModel):
    conversation_id: str
    surface: str = Field(default="landing", pattern="^(landing|dashboard|help)$")
    message: str = Field(min_length=1, max_length=2000)
    page_url: str | None = Field(default=None, max_length=500)
    visitor_id: str | None = Field(default=None, max_length=120)


class AssistantChatResponse(BaseModel):
    message_id: str
    answer: str
    citations: list[AssistantCitation] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)


class AssistantFeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=1000)


class AssistantSupportRequestCreate(BaseModel):
    conversation_id: str
    surface: str = Field(default="landing", pattern="^(landing|dashboard|help)$")
    contact: str = Field(min_length=2, max_length=240)
    message: str = Field(min_length=5, max_length=2000)
    page_url: str | None = Field(default=None, max_length=500)
    visitor_id: str | None = Field(default=None, max_length=120)


class AssistantSupportRequestResponse(BaseModel):
    request_id: str
    status: str
    message: str


class AssistantKnowledgeSourceResponse(BaseModel):
    id: str
    source_type: str
    title: str
    path: str
    public_url: str | None
    visibility: str
    checksum: str
    status: str
    last_indexed_at: str | None


class AssistantKnowledgeSearchResponse(BaseModel):
    chunk_id: str
    source_id: str
    title: str
    url: str | None
    content: str
    score: float


class AssistantReindexResponse(BaseModel):
    status: str
    sources: int
    chunks: int
