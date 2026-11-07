from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.sentry import init_sentry
from app.modules.link_tracking.api import public_router as tracking_public_router

settings = get_settings()
init_sentry()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Backend MVP for Telegram AI neurocommenting SaaS.",
    root_path=settings.api_root_path,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if settings.is_production or host not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "env": settings.app_env}


app.include_router(api_router, prefix=settings.api_v1_prefix)

app.include_router(tracking_public_router)
