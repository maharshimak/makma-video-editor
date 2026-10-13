"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .db import init_db, session_scope
from .media.ffmpeg import ffmpeg_available

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("makma")


def _install_usage_sink() -> None:
    """Route model-router usage into the ModelUsage table for cost reporting."""
    from .ai.router import get_router
    from .models import ModelUsage

    def sink(capability: str, usage, ok: bool, project_id: str) -> None:
        with session_scope() as db:
            db.add(
                ModelUsage(
                    project_id=project_id, capability=capability,
                    provider=usage.provider, model=usage.model,
                    tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                    duration_ms=usage.duration_ms, cost_usd=usage.cost_usd,
                    cost_known=usage.cost_known,
                    cache_hit=usage.cache_hit, ok=ok,
                )
            )

    get_router().set_usage_sink(sink)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed before anything binds a port. A production instance running
    # with the built-in secret is worse than one that refused to start.
    settings.enforce_production_safety()
    if not settings.is_production and settings.unsafe_settings():
        log.warning(
            "Running with development defaults (%s). These are refused in "
            "production; see docs/SECURITY.md.",
            settings.environment,
        )

    init_db()
    _install_usage_sink()

    ok, version = ffmpeg_available()
    if ok:
        log.info("FFmpeg ready: %s", version)
    else:
        log.error(
            "FFmpeg was not found at %r. Rendering and ingestion will fail until "
            "it is installed or MAKMA_FFMPEG_PATH is set.", settings.ffmpeg_path
        )

    if settings.inline_worker:
        from .jobs.worker import start_inline_workers, stop_inline_workers

        start_inline_workers()
        log.info("Started %d inline worker thread(s)", settings.worker_concurrency)
        try:
            yield
        finally:
            stop_inline_workers()
    else:
        log.info("Inline workers disabled; run `python -m makma.jobs.worker`")
        yield


app = FastAPI(
    title="Mak'ma Editz API",
    version="0.1.0",
    description="AI-native creative operating system for video and photo editing.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}" if settings.debug
                      else "Something went wrong on the server."
        },
    )


from .api import (  # noqa: E402  (routers import app-level settings)
    routes_ai,
    routes_assets,
    routes_jobs,
    routes_projects,
    routes_system,
)

app.include_router(routes_projects.auth_router)
app.include_router(routes_projects.router)
app.include_router(routes_assets.router)
app.include_router(routes_ai.router)
app.include_router(routes_jobs.router)
app.include_router(routes_system.router)


@app.get("/")
def root() -> dict:
    return {
        "name": settings.app_name,
        "version": app.version,
        "docs": "/docs",
        "health": "/api/system/health",
    }
