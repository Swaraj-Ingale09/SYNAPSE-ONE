"""FastAPI application factory for the SYNAPSE ONE core gateway."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from synapse import __version__
from synapse.api.auth import require_token
from synapse.api.routers import events, jobs, system
from synapse.config import Settings, get_settings
from synapse.contracts import HealthOut, ReadyOut
from synapse.db import check_database
from synapse.security import ensure_token

log = logging.getLogger("synapse.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # Generate (once) the client token before serving any request.
    app_token = ensure_token(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("api_started version=%s url=%s", __version__, settings.api_url)
        yield
        log.info("api_stopping url=%s", settings.api_url)

    app = FastAPI(
        title="SYNAPSE ONE API",
        version=__version__,
        description="Persistent local intelligence gateway (spec sections 10.2, 15.1).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token = app_token
    app.state.started = time.monotonic()

    @app.get("/health", response_model=HealthOut, tags=["system"])
    def health() -> HealthOut:
        return HealthOut(
            status="ok",
            service="synapse-api",
            version=__version__,
            time=datetime.now(UTC),
        )

    @app.get("/ready", tags=["system"])
    def ready():
        """Readiness: 200 only when the database is actually reachable.

        No return annotation on purpose: the success model is ReadyOut, the
        failure path is a 503 JSONResponse — FastAPI must not build a union
        response model out of them.
        """
        ok, detail = check_database(settings.database_url)
        if ok:
            return ReadyOut(status="ready", version=__version__, database="connected")
        payload = ReadyOut(
            status="not_ready",
            version=__version__,
            database="unavailable",
            detail=detail,
        )
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))

    guarded = [Depends(require_token)]
    app.include_router(jobs.router, prefix="/v1/jobs", tags=["jobs"], dependencies=guarded)
    app.include_router(events.router, prefix="/v1/events", tags=["events"], dependencies=guarded)
    app.include_router(system.router, prefix="/v1", tags=["system"], dependencies=guarded)

    return app
