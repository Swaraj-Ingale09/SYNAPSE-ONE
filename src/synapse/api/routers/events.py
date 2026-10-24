"""Global event routes: polling and Server-Sent Events streaming."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from synapse.contracts import JobEvent
from synapse.db import session_scope
from synapse.db.models import JobEvent as JobEventRow

router = APIRouter()


def _fetch_after(database_url: str, since_id: int, limit: int) -> list[JobEventRow]:
    """Blocking fetch executed in a worker thread."""
    with session_scope(database_url) as session:
        rows = session.scalars(
            select(JobEventRow)
            .where(JobEventRow.id > since_id)
            .order_by(JobEventRow.id)
            .limit(limit)
        ).all()
        return list(rows)


@router.get("", response_model=list[JobEvent])
def events_since(
    request: Request,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[JobEvent]:
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        rows = session.scalars(
            select(JobEventRow)
            .where(JobEventRow.id > since_id)
            .order_by(JobEventRow.id)
            .limit(limit)
        ).all()
        return [JobEvent.model_validate(row) for row in rows]


@router.get("/stream")
def stream(
    request: Request,
    since_id: int | None = Query(default=None, ge=0),
) -> EventSourceResponse:
    """SSE stream of real job events (dashboard / CLI subscribe here)."""
    settings = request.app.state.settings

    initial = since_id
    if initial is None:
        header = request.headers.get("last-event-id", "")
        initial = int(header) if header.isdigit() else 0

    async def generator():
        last = initial or 0
        while True:
            rows = await anyio.to_thread.run_sync(
                _fetch_after, settings.database_url, last, 500
            )
            for row in rows:
                last = row.id
                yield ServerSentEvent(
                    id=str(row.id),
                    event=row.event_type,
                    data=JobEvent.model_validate(row).model_dump_json(),
                )
            if not rows:
                await anyio.sleep(settings.event_poll_interval)

    return EventSourceResponse(generator(), ping=15)
