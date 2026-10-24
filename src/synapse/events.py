"""Append a job event inside an open session (event schema: spec 14.3)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from synapse.db.models import JobEvent


def emit(
    session: Session,
    *,
    job_id: str,
    event_type: str,
    component_id: str = "core",
    status: str | None = None,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> JobEvent:
    event = JobEvent(
        job_id=job_id,
        event_type=event_type,
        component_id=component_id,
        status=status,
        correlation_id=correlation_id,
        metadata_=metadata or {},
    )
    session.add(event)
    session.flush()
    return event
