"""Worker process: claim, execute, checkpoint, recover.

Design notes (spec 9.2, 16.3):

- Claiming uses ``FOR UPDATE SKIP LOCKED`` so multiple future workers never
  double-execute a job.
- The claim transaction commits *before* execution starts; all execution-time
  database touches (heartbeat, cancel checks, events, final state) use short
  independent transactions. A long task therefore never blocks the API's
  cancel endpoint.
- Stale jobs (expired leases) are recovered at startup and periodically:
  requeued until ``max_attempts``, then failed safely.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from synapse.config import Settings, get_settings
from synapse.contracts import JobStatus, assert_transition
from synapse.db import session_scope
from synapse.db.models import Job, utcnow
from synapse.events import emit
from synapse.security import redact
from synapse.worker.tasks import TASKS, TaskCancelled, TaskContext, TaskFailure

log = logging.getLogger("synapse.worker")


# --- claim / execute ----------------------------------------------------------


def claim_job(settings: Settings, worker_id: str) -> str | None:
    """Atomically claim the oldest queued job; returns its id or None."""
    now = utcnow()
    deadline = now + timedelta(seconds=settings.job_lease_seconds)
    with session_scope(settings.database_url) as session:
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.QUEUED.value)
            .order_by(Job.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        job = session.execute(stmt).scalar_one_or_none()
        if job is None:
            return None
        assert_transition(job.status, JobStatus.RUNNING)
        job.status = JobStatus.RUNNING.value
        job.worker_id = worker_id
        job.attempts += 1
        job.started_at = job.started_at or now
        job.lease_expires_at = deadline
        job.current_step = "executing"
        emit(
            session,
            job_id=job.id,
            event_type="job_started",
            component_id="worker",
            status=JobStatus.RUNNING.value,
            correlation_id=job.correlation_id,
            metadata={"worker_id": worker_id, "attempt": job.attempts},
        )
        return job.id


@dataclass
class _Outcome:
    kind: str  # "success" | "failed" | "cancelled"
    value: Any = None


def _finalize(settings: Settings, job_id: str, outcome: _Outcome, started: float) -> str:
    """Write the terminal state under a row lock; honour late cancel requests."""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with session_scope(settings.database_url) as session:
        job = session.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            return "missing"
        if job.status != JobStatus.RUNNING.value:
            # Someone legitimately moved the job while we were finishing.
            return job.status
        now = utcnow()
        if outcome.kind == "failed":
            assert_transition(job.status, JobStatus.FAILED)
            job.status = JobStatus.FAILED.value
            job.error = str(outcome.value)
            job.finished_at = now
            emit(
                session,
                job_id=job.id,
                event_type="job_failed",
                component_id="worker",
                status=JobStatus.FAILED.value,
                correlation_id=job.correlation_id,
                metadata={"elapsed_ms": elapsed_ms},
            )
        elif outcome.kind == "cancelled" or job.cancel_requested:
            assert_transition(job.status, JobStatus.CANCELLED)
            job.status = JobStatus.CANCELLED.value
            job.finished_at = now
            emit(
                session,
                job_id=job.id,
                event_type="job_cancelled",
                component_id="worker",
                status=JobStatus.CANCELLED.value,
                correlation_id=job.correlation_id,
                metadata={"origin": outcome.kind, "elapsed_ms": elapsed_ms},
            )
        else:
            assert_transition(job.status, JobStatus.COMPLETED)
            job.status = JobStatus.COMPLETED.value
            job.result = outcome.value
            job.current_step = "done"
            job.finished_at = now
            emit(
                session,
                job_id=job.id,
                event_type="job_completed",
                component_id="worker",
                status=JobStatus.COMPLETED.value,
                correlation_id=job.correlation_id,
                metadata={"elapsed_ms": elapsed_ms},
            )
        return job.status


def execute_job(settings: Settings, job_id: str) -> str:
    """Run the claimed job's task and persist its terminal state."""
    started = time.monotonic()
    with session_scope(settings.database_url) as session:
        job = session.get(Job, job_id)
        if job is None:
            return "missing"
        if job.status != JobStatus.RUNNING.value:
            return job.status
        task_type = job.task_type
        payload: dict[str, Any] = dict(job.payload or {})
        correlation_id = job.correlation_id

    ctx = TaskContext(settings=settings, job_id=job_id, correlation_id=correlation_id)
    task = TASKS.get(task_type)
    if task is None:
        return _finalize(
            settings, job_id, _Outcome("failed", f"unknown task_type '{task_type}'"), started
        )
    try:
        result = task(payload, ctx)
    except TaskCancelled:
        return _finalize(settings, job_id, _Outcome("cancelled", "cancel flag observed"), started)
    except TaskFailure as exc:
        return _finalize(settings, job_id, _Outcome("failed", str(exc)), started)
    except Exception as exc:  # noqa: BLE001 - worker must survive any task crash
        log.exception("task_crashed job_id=%s task_type=%s", job_id, task_type)
        message = redact(f"{type(exc).__name__}: {exc}") or "unknown error"
        return _finalize(settings, job_id, _Outcome("failed", message), started)
    return _finalize(settings, job_id, _Outcome("success", result), started)


def run_once(settings: Settings, worker_id: str = "test-worker") -> str | None:
    """Claim and execute at most one job. Returns the job id, or None if idle."""
    job_id = claim_job(settings, worker_id)
    if job_id is None:
        return None
    execute_job(settings, job_id)
    return job_id


# --- recovery -----------------------------------------------------------------


def recover_stale_jobs(session: Session, settings: Settings) -> dict[str, int]:
    """Requeue running jobs whose lease expired; fail them after max attempts."""
    now = utcnow()
    stmt = (
        select(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now),
        )
        .with_for_update(skip_locked=True)
    )
    requeued = failed = 0
    for job in session.scalars(stmt).all():
        emit(
            session,
            job_id=job.id,
            event_type="job_lease_expired",
            component_id="worker",
            status=JobStatus.RUNNING.value,
            correlation_id=job.correlation_id,
            metadata={"attempt": job.attempts, "max_attempts": job.max_attempts},
        )
        if job.attempts >= job.max_attempts:
            assert_transition(job.status, JobStatus.FAILED)
            job.status = JobStatus.FAILED.value
            job.error = "worker lease expired and max attempts exhausted"
            job.finished_at = now
            job.lease_expires_at = None
            emit(
                session,
                job_id=job.id,
                event_type="job_failed",
                component_id="worker",
                status=JobStatus.FAILED.value,
                correlation_id=job.correlation_id,
                metadata={"reason": "lease_exhausted"},
            )
            failed += 1
        else:
            assert_transition(job.status, JobStatus.RETRYING)
            job.status = JobStatus.RETRYING.value
            emit(
                session,
                job_id=job.id,
                event_type="job_retrying",
                component_id="worker",
                status=JobStatus.RETRYING.value,
                correlation_id=job.correlation_id,
                metadata={"attempt": job.attempts},
            )
            assert_transition(job.status, JobStatus.QUEUED)
            job.status = JobStatus.QUEUED.value
            job.worker_id = None
            job.lease_expires_at = None
            job.current_step = None
            emit(
                session,
                job_id=job.id,
                event_type="job_requeued",
                component_id="worker",
                status=JobStatus.QUEUED.value,
                correlation_id=job.correlation_id,
                metadata={"attempt": job.attempts},
            )
            requeued += 1
    return {"requeued": requeued, "failed": failed}


def recover_stale(settings: Settings) -> dict[str, int]:
    with session_scope(settings.database_url) as session:
        return recover_stale_jobs(session, settings)


# --- main loop ----------------------------------------------------------------


def _sleep_interruptible(seconds: float, should_stop) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def run_forever(settings: Settings) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    # A stale stop file from a previous run must not kill this fresh worker.
    if settings.worker_stop_file.exists():
        settings.worker_stop_file.unlink(missing_ok=True)

    stopping = {"flag": False}

    def _handle(signum, _frame) -> None:  # pragma: no cover - signal path
        stopping["flag"] = True
        log.info("signal_received signum=%s", signum)

    try:
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
    except ValueError:  # pragma: no cover - non-main thread (tests)
        pass

    log.info("worker_started worker_id=%s", worker_id)
    next_recovery = 0.0

    while not stopping["flag"]:
        if settings.worker_stop_file.exists():
            log.info("worker_stop_file_detected")
            break

        if time.monotonic() >= next_recovery:
            try:
                stats = recover_stale(settings)
                if stats["requeued"] or stats["failed"]:
                    log.info(
                        "lease_recovery requeued=%s failed=%s",
                        stats["requeued"],
                        stats["failed"],
                    )
            except Exception:  # noqa: BLE001 - DB may be briefly unavailable
                log.exception("lease_recovery_failed")
            next_recovery = time.monotonic() + 60.0

        try:
            job_id = claim_job(settings, worker_id)
        except Exception:  # noqa: BLE001 - keep the worker alive on transient errors
            log.exception("claim_failed")
            _sleep_interruptible(2.0, lambda: stopping["flag"])
            continue

        if job_id is None:
            _sleep_interruptible(
                settings.worker_poll_interval,
                lambda: stopping["flag"] or settings.worker_stop_file.exists(),
            )
            continue

        log.info("job_claimed job_id=%s", job_id)
        try:
            final_status = execute_job(settings, job_id)
            log.info("job_finished job_id=%s status=%s", job_id, final_status)
        except Exception:  # noqa: BLE001 - lease recovery will handle the job
            log.exception("job_execution_crashed job_id=%s", job_id)

    log.info("worker_stopped worker_id=%s", worker_id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever(get_settings())


if __name__ == "__main__":
    main()
