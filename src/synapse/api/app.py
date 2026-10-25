"""FastAPI application factory for the SYNAPSE ONE core gateway."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from synapse import __version__
from synapse.api.auth import require_token
from synapse.api.routers import dashboard, events, jobs, system
from synapse.config import Settings, dashboard_dist_dir, get_settings
from synapse.contracts import HealthOut, ReadyOut
from synapse.db import check_database
from synapse.security import ensure_token

log = logging.getLogger("synapse.api")

# Built dashboard assets (npm run build in dashboard/). The derivation is
# shared with the CLI (`synapse dashboard`) so both resolve the same folder.
DASHBOARD_DIST = dashboard_dist_dir()


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
    app.include_router(
        dashboard.router, prefix="/v1", tags=["dashboard"], dependencies=guarded
    )

    # --- dashboard static assets ---------------------------------------------
    # The built SPA is served from the same origin as the API, so the browser
    # never needs CORS and the token is not cross-site. If the assets have not
    # been built yet, the API still works; only the UI is missing, and we say so.
    if DASHBOARD_DIST.is_dir():
        assets = DASHBOARD_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        index_file = DASHBOARD_DIST / "index.html"

        @app.get("/", include_in_schema=False)
        def dashboard_index():
            return FileResponse(index_file)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            """Serve the SPA for any non-API path.

            Registered last and explicitly skipping API routes so it can never
            shadow /v1, /health, /ready, or /docs.
            """
            if full_path.startswith(("v1/", "health", "ready", "docs", "openapi.json", "redoc")):
                return JSONResponse(status_code=404, content={"detail": "not found"})
            candidate = (DASHBOARD_DIST / full_path).resolve()
            # Path traversal guard: never serve anything outside dist/.
            if candidate.is_relative_to(DASHBOARD_DIST) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_file)
    else:
        log.warning(
            "dashboard_assets_missing dir=%s — run `npm run build` in dashboard/ to serve the UI",
            DASHBOARD_DIST,
        )

    return app
