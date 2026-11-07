from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.modules.auth.schemas import RegisterRequest, UserResponse


@pytest.mark.parametrize("email", ["demo@example.invalid", "user@example.com"])
def test_user_response_serializes_existing_login(email: str) -> None:
    user = SimpleNamespace(
        id="internal-user",
        email=email,
        full_name=None,
        status="active",
        email_verified_at=None,
    )
    assert UserResponse.model_validate(user).model_dump(mode="json")["email"] == email


def test_registration_still_rejects_reserved_email_domain() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(email="new@example.invalid", password="test-password")
