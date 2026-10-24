"""Shared Pydantic contracts for jobs, events, and system status.

These models are the stable interface between the API, the worker, the CLI,
and (later) the dashboard and MCP server. Spec reference: sections 4.3, 9.2,
14.3.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    """Job lifecycle states (spec 9.2)."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    }
)

# Legal state transitions (spec 9.2). Anything not listed here is invalid.
LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset(
        {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.QUEUED: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.EXPIRED}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.RETRYING,
            JobStatus.WAITING_FOR_APPROVAL,
            JobStatus.WAITING_FOR_INPUT,
            JobStatus.EXPIRED,
        }
    ),
    JobStatus.WAITING_FOR_APPROVAL: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.EXPIRED}
    ),
    JobStatus.WAITING_FOR_INPUT: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.EXPIRED}
    ),
    JobStatus.RETRYING: frozenset(
        {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.EXPIRED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a job state transition is not permitted."""


def assert_transition(current: JobStatus | str, new: JobStatus | str) -> None:
    """Validate a state transition, raising InvalidTransition if illegal."""
    current = JobStatus(current)
    new = JobStatus(new)
    if new not in LEGAL_TRANSITIONS[current]:
        raise InvalidTransition(f"illegal job transition: {current.value} -> {new.value}")


# --- Request / response models -----------------------------------------------


class JobCreate(BaseModel):
    task_type: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    project_scope: str | None = None


class Job(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_type: str
    status: JobStatus
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    project_scope: str | None = None
    user_scope: str
    correlation_id: str
    worker_id: str | None = None
    attempts: int
    max_attempts: int
    cancel_requested: bool
    current_step: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    finished_at: datetime | None = None
    lease_expires_at: datetime | None = None


class JobEvent(BaseModel):
    """Event stream record (schema follows spec 14.3)."""

    model_config = ConfigDict(from_attributes=True)

    # ORM column is `id`; expose it as `event_id` per the event schema (spec 14.3).
    event_id: int = Field(validation_alias=AliasChoices("event_id", "id"))
    job_id: str
    event_type: str
    timestamp: datetime
    component_id: str
    status: str | None = None
    correlation_id: str | None = None
    # ORM attribute is `metadata_`; `metadata` on the class resolves to
    # SQLAlchemy's MetaData object, so the ORM name must win the alias choice.
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata_", "metadata"),
    )


class JobDetail(Job):
    events: list[JobEvent] = Field(default_factory=list)


class SystemStatus(BaseModel):
    version: str
    api_uptime_seconds: float
    jobs: dict[str, int]
    queue_depth: int


class HealthOut(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    time: datetime


class ReadyOut(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    database: str
    detail: str | None = None
