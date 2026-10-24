"""Worker lifecycle tests: execution, cancellation, failure, recovery (spec 16.1, 16.3)."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

from synapse.config import Settings
from synapse.contracts import JobStatus
from synapse.db import session_scope
from synapse.db.models import Job as JobRow
from synapse.db.models import utcnow
from synapse.worker.main import recover_stale, run_once


def _job_status(settings: Settings, job_id: str) -> str:
    with session_scope(settings.database_url) as session:
        job = session.get(JobRow, job_id)
        assert job is not None
        return job.status


def _insert_running_job(
    settings: Settings,
    attempts: int,
    max_attempts: int,
    lease_expired: bool,
) -> str:
    with session_scope(settings.database_url) as session:
        job = JobRow(
            id=str(uuid.uuid4()),
            task_type="slow",
            status=JobStatus.RUNNING.value,
            payload={"seconds": 1},
            correlation_id=str(uuid.uuid4()),
            attempts=attempts,
            max_attempts=max_attempts,
            worker_id="ghost-worker:1",
            lease_expires_at=utcnow() - timedelta(seconds=30)
            if lease_expired
            else utcnow() + timedelta(seconds=30),
        )
        session.add(job)
        session.flush()
        return job.id


# --- happy path ----------------------------------------------------------------


def test_worker_completes_echo_job(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs",
        json={"task_type": "echo", "payload": {"text": "hello brave world"}},
        headers=auth,
    ).json()

    assert run_once(settings) == created["id"]
    assert run_once(settings) is None  # queue now empty

    final = client.get(f"/v1/jobs/{created['id']}", headers=auth).json()
    assert final["status"] == "completed"
    assert final["result"]["words"] == 3
    assert [event["event_type"] for event in final["events"]] == [
        "job_queued",
        "job_started",
        "job_completed",
    ]
    assert final["error"] is None


def test_text_stats_task_computes_real_counts(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs",
        json={"task_type": "text_stats", "payload": {"text": "alpha beta\nalpha"}},
        headers=auth,
    ).json()
    run_once(settings)
    final = client.get(f"/v1/jobs/{created['id']}", headers=auth).json()
    assert final["status"] == "completed"
    assert final["result"]["words"] == 3
    assert final["result"]["unique_words"] == 2
    assert final["result"]["lines"] == 2


# --- failure -------------------------------------------------------------------


def test_unknown_task_type_fails_with_clear_error(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs",
        json={"task_type": "not-a-real-task", "payload": {}},
        headers=auth,
    ).json()
    run_once(settings)
    final = client.get(f"/v1/jobs/{created['id']}", headers=auth).json()
    assert final["status"] == "failed"
    assert "unknown task_type" in final["error"]
    assert final["attempts"] == 1


def test_invalid_task_payload_fails_cleanly(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs",
        json={"task_type": "text_stats", "payload": {"text": 123}},
        headers=auth,
    ).json()
    run_once(settings)
    final = client.get(f"/v1/jobs/{created['id']}", headers=auth).json()
    assert final["status"] == "failed"
    assert "must be a string" in final["error"]


# --- cancellation --------------------------------------------------------------


def test_cancelled_queued_job_is_never_claimed(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs", json={"task_type": "echo", "payload": {"text": "x"}}, headers=auth
    ).json()
    client.post(f"/v1/jobs/{created['id']}/cancel", headers=auth)
    assert run_once(settings) is None
    final = client.get(f"/v1/jobs/{created['id']}", headers=auth).json()
    assert final["status"] == "cancelled"
    assert final["result"] is None


def test_cancel_running_job_stops_the_worker_task(client, auth, settings) -> None:
    created = client.post(
        "/v1/jobs",
        json={"task_type": "slow", "payload": {"seconds": 20}},
        headers=auth,
    ).json()
    job_id = created["id"]

    outcome: dict[str, str] = {}

    def _run() -> None:
        outcome["claimed"] = run_once(settings) or "none"

    worker_thread = threading.Thread(target=_run, daemon=True)
    worker_thread.start()

    # Wait until the job is actually running (claimed by the worker thread).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _job_status(settings, job_id) == JobStatus.RUNNING.value:
            break
        time.sleep(0.05)
    assert _job_status(settings, job_id) == JobStatus.RUNNING.value

    # Request cancellation through the API — must not block on the running task.
    cancel_response = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth)
    assert cancel_response.status_code == 200
    assert cancel_response.json()["cancel_requested"] is True

    worker_thread.join(timeout=15.0)
    assert not worker_thread.is_alive(), "worker task did not observe the cancel flag"
    assert outcome.get("claimed") == job_id

    final = client.get(f"/v1/jobs/{job_id}", headers=auth).json()
    assert final["status"] == "cancelled"
    assert final["result"] is None
    assert time.monotonic()  # sanity: no full-duration wait occurred


# --- worker restart recovery (spec 16.3 #4) -------------------------------------


def test_stale_lease_is_requeued_with_events(client, auth, settings) -> None:
    job_id = _insert_running_job(settings, attempts=1, max_attempts=3, lease_expired=True)
    stats = recover_stale(settings)
    assert stats == {"requeued": 1, "failed": 0}

    final = client.get(f"/v1/jobs/{job_id}", headers=auth).json()
    assert final["status"] == "queued"
    assert final["attempts"] == 1
    assert final["worker_id"] is None
    event_types = [event["event_type"] for event in final["events"]]
    assert "job_lease_expired" in event_types
    assert "job_requeued" in event_types

    # and a fresh worker can pick it up again
    assert run_once(settings) == job_id
    assert client.get(f"/v1/jobs/{job_id}", headers=auth).json()["status"] == "completed"


def test_stale_lease_fails_after_max_attempts(client, auth, settings) -> None:
    job_id = _insert_running_job(settings, attempts=3, max_attempts=3, lease_expired=True)
    stats = recover_stale(settings)
    assert stats == {"requeued": 0, "failed": 1}

    final = client.get(f"/v1/jobs/{job_id}", headers=auth).json()
    assert final["status"] == "failed"
    assert "max attempts" in final["error"]


def test_fresh_lease_is_left_alone(settings) -> None:
    job_id = _insert_running_job(settings, attempts=1, max_attempts=3, lease_expired=False)
    stats = recover_stale(settings)
    assert stats == {"requeued": 0, "failed": 0}
    assert _job_status(settings, job_id) == JobStatus.RUNNING.value
