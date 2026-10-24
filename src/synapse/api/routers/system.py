"""System status and admin routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from starlette.responses import JSONResponse

from synapse import __version__
from synapse.contracts import JobStatus, SystemStatus
from synapse.db import session_scope
from synapse.db.models import Job as JobRow

router = APIRouter()


@router.get("/system/status", response_model=SystemStatus)
def system_status(request: Request) -> SystemStatus:
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        rows = session.execute(
            select(JobRow.status, func.count()).group_by(JobRow.status)
        ).all()
    counts = {state.value: 0 for state in JobStatus}
    for status_value, count in rows:
        counts[status_value] = int(count)
    return SystemStatus(
        version=__version__,
        api_uptime_seconds=round(time.monotonic() - request.app.state.started, 3),
        jobs=counts,
        queue_depth=counts[JobStatus.QUEUED.value],
    )


@router.post("/admin/shutdown")
def shutdown(request: Request):
    """Ask the supervising server process to exit cleanly (used by `synapse stop`)."""
    server = getattr(request.app.state, "server", None)
    if server is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "shutdown is managed by the synapse process supervisor"},
        )
    server.should_exit = True
    return {"status": "stopping"}
