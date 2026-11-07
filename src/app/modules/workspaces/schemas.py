from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    plan_id: str | None

    model_config = {"from_attributes": True}
