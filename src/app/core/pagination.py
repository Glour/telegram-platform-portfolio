from pydantic import BaseModel, Field


class PageParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class PageMeta(BaseModel):
    limit: int
    next_cursor: str | None = None
