"""Job lifecycle routes: create, list, inspect, events, cancel."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import desc, select

from synapse.config import Settings
from synapse.contracts import (
    TERMINAL_STATES,
    Job,
    JobCreate,
    JobDetail,
    JobEvent,
    JobStatus,
    assert_transition,
)
from synapse.db import session_scope
from synapse.db.models import Job as JobRow
from synapse.db.models import JobEvent as JobEventRow
from synapse.db.models import utcnow
from synapse.events import emit

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("", response_model=Job, status_code=201)
def create_job(request: Request, body: JobCreate) -> Job:
    settings = _settings(request)
    size = len(body.model_dump_json().encode("utf-8"))
    if size > settings.max_payload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"payload too large: {size} bytes (limit {settings.max_payload_bytes})",
        )
    with session_scope(settings.database_url) as session:
        job = JobRow(
            id=str(uuid.uuid4()),
            task_type=body.task_type,
            status=JobStatus.QUEUED.value,
            payload=body.payload,
            project_scope=body.project_scope,
            user_scope="local",
            correlation_id=str(uuid.uuid4()),
            max_attempts=settings.job_max_attempts,
        )
        session.add(job)
        session.flush()
        emit(
            session,
            job_id=job.id,
            event_type="job_queued",
            component_id="api",
            status=JobStatus.QUEUED.value,
            correlation_id=job.correlation_id,
            metadata={"task_type": job.task_type, "project_scope": job.project_scope},
        )
        return Job.model_validate(job)


@router.get("", response_model=list[Job])
def list_jobs(
    request: Request,
    status: JobStatus | None = None,
    project_scope: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Job]:
    settings = _settings(request)
    with session_scope(settings.database_url) as session:
        stmt = select(JobRow).order_by(desc(JobRow.created_at)).limit(limit)
        if status is not None:
            stmt = stmt.where(JobRow.status == status.value)
        if project_scope:
            stmt = stmt.where(JobRow.project_scope == project_scope)
        rows = session.scalars(stmt).all()
        return [Job.model_validate(row) for row in rows]


@router.get("/{job_id}", response_model=JobDetail)
def get_job(request: Request, job_id: str) -> JobDetail:
    settings = _settings(request)
    with session_scope(settings.database_url) as session:
        job = session.get(JobRow, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        rows = session.scalars(
            select(JobEventRow)
            .where(JobEventRow.job_id == job_id)
            .order_by(desc(JobEventRow.id))
            .limit(100)
        ).all()
        base = Job.model_validate(job)
        events = [JobEvent.model_validate(row) for row in reversed(rows)]
        return JobDetail(**base.model_dump(), events=events)


@router.get("/{job_id}/events", response_model=list[JobEvent])
def get_job_events(
    request: Request,
    job_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[JobEvent]:
    settings = _settings(request)
    with session_scope(settings.database_url) as session:
        if session.get(JobRow, job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        rows = session.scalars(
            select(JobEventRow)
            .where(JobEventRow.job_id == job_id)
            .order_by(desc(JobEventRow.id))
            .limit(limit)
        ).all()
        return [JobEvent.model_validate(row) for row in reversed(rows)]


@router.post("/{job_id}/cancel", response_model=Job)
def cancel_job(request: Request, job_id: str) -> Job:
    settings = _settings(request)
    with session_scope(settings.database_url) as session:
        job = session.execute(
            select(JobRow).where(JobRow.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        current = JobStatus(job.status)
        if current in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail=f"job already {job.status}")

        if current in (JobStatus.CREATED, JobStatus.QUEUED):
            # Not started yet: cancel outright.
            assert_transition(current, JobStatus.CANCELLED)
            job.status = JobStatus.CANCELLED.value
            job.finished_at = utcnow()
            job.cancel_requested = True
            emit(
                session,
                job_id=job.id,
                event_type="job_cancelled",
                component_id="api",
                status=JobStatus.CANCELLED.value,
                correlation_id=job.correlation_id,
                metadata={"origin": "queued"},
            )
        else:
            # Running: flag it; the worker observes the flag and stops cleanly.
            job.cancel_requested = True
            emit(
                session,
                job_id=job.id,
                event_type="cancellation_requested",
                component_id="api",
                status=job.status,
                correlation_id=job.correlation_id,
                metadata={"origin": "running"},
            )
        return Job.model_validate(job)
