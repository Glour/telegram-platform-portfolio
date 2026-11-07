from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    workspace_name: str = Field(default="My workspace", min_length=2, max_length=160)


class LoginResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    requires_email_verification: bool = False
    message: str | None = None
    dev_verification_token: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=20)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class GoogleStartResponse(BaseModel):
    authorization_url: str
    state: str


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str


class UserResponse(BaseModel):
    id: str
    # Existing internal logins may use reserved domains such as .local.
    # Validate public addresses at registration, not when returning stored users.
    email: str
    full_name: str | None
    status: str
    email_verified_at: datetime | None = None

    model_config = {"from_attributes": True}
