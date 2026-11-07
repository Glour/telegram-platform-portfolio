from pydantic import BaseModel, Field


class ContactRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    contact: str = Field(min_length=2, max_length=200)
    message: str = Field(min_length=5, max_length=4000)
    page_url: str | None = Field(default=None, max_length=500)


class ContactResponse(BaseModel):
    status: str
    message: str
