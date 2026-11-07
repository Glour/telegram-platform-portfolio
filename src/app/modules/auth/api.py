from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep
from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    GoogleCallbackRequest,
    GoogleStartResponse,
    LoginResponse,
    RegisterRequest,
    ResendVerificationRequest,
    UserResponse,
    VerifyEmailRequest,
)
from app.modules.auth.service import (
    GoogleOAuthError,
    build_login_response,
    consume_email_verification_token,
    consume_google_oauth_state,
    create_email_verification_token,
    create_google_oauth_state,
    create_workspace_for_user,
    ensure_internal_full_access_for_user,
    exchange_google_code,
    get_or_create_google_user,
    get_valid_google_oauth_state,
    google_authorization_url,
    send_email_verification,
)

router = APIRouter()
OAuth2FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: SessionDep) -> LoginResponse:
    existing = await session.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
    )
    session.add(user)
    await session.flush()

    await create_workspace_for_user(session, user, payload.workspace_name)
    verification_token = await create_email_verification_token(session, user)
    await session.commit()
    await send_email_verification(user, verification_token)

    settings = get_settings()
    return LoginResponse(
        requires_email_verification=True,
        message="Проверьте почту и подтвердите email, чтобы войти в кабинет.",
        dev_verification_token=verification_token if settings.auth_debug_tokens or not settings.is_production else None,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    session: SessionDep,
    form: OAuth2FormDep,
) -> LoginResponse:
    result = await session.execute(select(User).where(User.email == form.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.email_verified_at:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email is not verified. Check your inbox or resend verification email.",
        )
    user.last_login_at = datetime.now(UTC)
    await ensure_internal_full_access_for_user(session, user)
    await session.commit()
    return LoginResponse(**build_login_response(user))


@router.post("/verify-email", response_model=LoginResponse)
async def verify_email(payload: VerifyEmailRequest, session: SessionDep) -> LoginResponse:
    user = await consume_email_verification_token(session, payload.token)
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification link is invalid or expired")
    user.last_login_at = datetime.now(UTC)
    await ensure_internal_full_access_for_user(session, user)
    await session.commit()
    return LoginResponse(**build_login_response(user), message="Email verified")


@router.post("/resend-verification", response_model=LoginResponse)
async def resend_verification(payload: ResendVerificationRequest, session: SessionDep) -> LoginResponse:
    result = await session.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if not user:
        return LoginResponse(requires_email_verification=True, message="If the email exists, verification was sent.")
    if user.email_verified_at:
        return LoginResponse(message="Email already verified")
    token = await create_email_verification_token(session, user)
    await session.commit()
    await send_email_verification(user, token)
    settings = get_settings()
    return LoginResponse(
        requires_email_verification=True,
        message="Verification email sent.",
        dev_verification_token=token if settings.auth_debug_tokens or not settings.is_production else None,
    )


@router.get("/google/start", response_model=GoogleStartResponse)
async def google_start(session: SessionDep) -> GoogleStartResponse:
    try:
        state = await create_google_oauth_state(session)
        await session.commit()
        return GoogleStartResponse(authorization_url=google_authorization_url(state), state=state)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/google/callback", response_model=LoginResponse)
async def google_callback(payload: GoogleCallbackRequest, session: SessionDep) -> LoginResponse:
    if not await get_valid_google_oauth_state(session, payload.state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google OAuth state")
    try:
        profile = await exchange_google_code(payload.code)
        user, workspace = await get_or_create_google_user(session, profile)
        await consume_google_oauth_state(session, payload.state)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google auth failed: {exc}") from exc
    if not user.email_verified_at:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Google email is not verified")
    user.last_login_at = datetime.now(UTC)
    await ensure_internal_full_access_for_user(session, user)
    await session.commit()
    return LoginResponse(**build_login_response(user, workspace.id if workspace else None))


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUserDep) -> User:
    return current_user
