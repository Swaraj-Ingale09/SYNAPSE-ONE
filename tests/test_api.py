"""API integration tests against the real gateway and test database (spec 16.1)."""

from __future__ import annotations

# --- health & readiness --------------------------------------------------------


def test_health_is_open_and_reports_version(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "synapse-api"


def test_ready_reports_database_connected(client) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"


# --- authentication (spec 15.1) ------------------------------------------------


def test_v1_requires_token(client) -> None:
    assert client.get("/v1/jobs").status_code == 401


def test_v1_rejects_wrong_token(client) -> None:
    response = client.get("/v1/jobs", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_v1_accepts_valid_token(client, auth) -> None:
    assert client.get("/v1/jobs", headers=auth).status_code == 200


# --- job lifecycle through the API ---------------------------------------------


def test_create_job_returns_queued_job_with_event(client, auth) -> None:
    response = client.post(
        "/v1/jobs",
        json={"task_type": "echo", "payload": {"text": "hi"}},
        headers=auth,
    )
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "queued"
    assert job["task_type"] == "echo"

    detail = client.get(f"/v1/jobs/{job['id']}", headers=auth).json()
    assert detail["id"] == job["id"]
    event_types = [event["event_type"] for event in detail["events"]]
    assert event_types == ["job_queued"]


def test_create_job_rejects_malformed_task_type(client, auth) -> None:
    response = client.post(
        "/v1/jobs",
        json={"task_type": "Bad Type!", "payload": {}},
        headers=auth,
    )
    assert response.status_code == 422


def test_create_job_rejects_oversized_payload(client, auth) -> None:
    response = client.post(
        "/v1/jobs",
        json={"task_type": "echo", "payload": {"text": "x" * (300 * 1024)}},
        headers=auth,
    )
    assert response.status_code == 413


def test_get_unknown_job_is_404(client, auth) -> None:
    assert client.get("/v1/jobs/does-not-exist", headers=auth).status_code == 404


def test_cancel_queued_job_transitions_to_cancelled(client, auth) -> None:
    job = client.post(
        "/v1/jobs", json={"task_type": "echo", "payload": {"text": "x"}}, headers=auth
    ).json()
    response = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    # cancelling a terminal job is a conflict, not a silent success
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth).status_code == 409


def test_list_filters_by_status(client, auth) -> None:
    client.post("/v1/jobs", json={"task_type": "echo", "payload": {"text": "a"}}, headers=auth)
    client.post("/v1/jobs", json={"task_type": "echo", "payload": {"text": "b"}}, headers=auth)
    queued = client.get("/v1/jobs?status=queued", headers=auth).json()
    cancelled = client.get("/v1/jobs?status=cancelled", headers=auth).json()
    assert len(queued) == 2
    assert cancelled == []


def test_system_status_counts_jobs(client, auth) -> None:
    client.post("/v1/jobs", json={"task_type": "echo", "payload": {"text": "a"}}, headers=auth)
    status = client.get("/v1/system/status", headers=auth).json()
    assert status["queue_depth"] == 1
    assert status["jobs"]["queued"] == 1
    assert status["version"]


# --- event feed (spec 14.3) -----------------------------------------------------


def test_events_polling_returns_ordered_records(client, auth) -> None:
    job = client.post(
        "/v1/jobs", json={"task_type": "echo", "payload": {"text": "e"}}, headers=auth
    ).json()
    events = client.get("/v1/events?since_id=0", headers=auth).json()
    assert events, "expected at least the job_queued event"
    assert events[-1]["job_id"] == job["id"]
    assert events[-1]["event_type"] == "job_queued"
    ids = [event["event_id"] for event in events]
    assert ids == sorted(ids)


def test_events_stream_emits_existing_event(client, auth) -> None:
    client.post(
        "/v1/jobs", json={"task_type": "echo", "payload": {"text": "stream"}}, headers=auth
    ).json()
    events = client.get("/v1/events?since_id=0&limit=200", headers=auth).json()
    last_id = events[-1]["event_id"]

    with client.stream(
        "GET", f"/v1/events/stream?since_id={last_id - 1}", headers=auth
    ) as response:
        assert response.status_code == 200
        found = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                found = line.split(":", 1)[1].strip()
                break
    assert found == "job_queued"


def test_job_events_endpoint_is_scoped(client, auth) -> None:
    job = client.post(
        "/v1/jobs", json={"task_type": "echo", "payload": {"text": "scoped"}}, headers=auth
    ).json()
    events = client.get(f"/v1/jobs/{job['id']}/events", headers=auth).json()
    assert [event["event_type"] for event in events] == ["job_queued"]
    assert client.get("/v1/jobs/nope/events", headers=auth).status_code == 404
