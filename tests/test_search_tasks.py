"""Worker × search task: durable jobs over the free public-API connectors (ADR 0008).

Honesty invariants asserted here:

- the result is the connector's own response, JSON-round-trippable, with
  ``total=None`` ("provider reported no count") staying ``None`` — distinct
  from a measured ``0``;
- an unknown connector fails with the stable ``unknown_connector`` reason and
  lists the real names — before any network traffic;
- connector failures become ``TaskFailure`` carrying the stable reason prefix
  and the real server detail, never a swallowed error or an empty success.

No test touches the real network: fakes replace ``build_connector`` at the
task module, and validation paths fail before any fetch.
"""

from __future__ import annotations

import json
import uuid

import pytest

from synapse.config import Settings
from synapse.connectors import ConnectorError, RateLimit, ResultItem, SearchResponse
from synapse.contracts import JobStatus
from synapse.db import session_scope
from synapse.db.models import Job
from synapse.worker.tasks import TaskContext, TaskFailure, search_task


def _ctx(settings: Settings, task_type: str = "search") -> TaskContext:
    """A TaskContext backed by a live job row (ensure_not_cancelled reads it)."""
    job_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    with session_scope(settings.database_url) as session:
        session.add(
            Job(
                id=job_id,
                task_type=task_type,
                status=JobStatus.RUNNING.value,
                payload={},
                correlation_id=correlation_id,
                attempts=1,
                max_attempts=3,
            )
        )
    return TaskContext(settings=settings, job_id=job_id, correlation_id=correlation_id)


def _response(**overrides) -> SearchResponse:
    defaults: dict = {
        "connector": "github",
        "domain": "coding",
        "query": "rag",
        "requested": 10,
        "page_size": 10,
        "items": (
            ResultItem(
                title="pypa/sampleproject",
                url="https://github.com/pypa/sampleproject",
                date="2026-01-02T03:04:05Z",
                snippet="Example project",
                facts=(("stars", "42"), ("language", "n/a")),
            ),
        ),
        "total": 1,
        "fetched_at": "2026-10-25T12:00:00+00:00",
        "rate": RateLimit(limit=10, remaining=9, source="headers"),
    }
    defaults.update(overrides)
    return SearchResponse(**defaults)


class _FakeConnector:
    """Context-manager connector replaying a canned response or error."""

    def __init__(self, response=None, error=None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def __enter__(self) -> _FakeConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        self.calls.append((query, limit))
        if self._error is not None:
            raise self._error
        return self._response


def _install_connector(monkeypatch: pytest.MonkeyPatch, **kwargs) -> _FakeConnector:
    fake = _FakeConnector(**kwargs)
    monkeypatch.setattr(
        "synapse.worker.tasks.build_connector", lambda _name, _settings: fake
    )
    return fake


def test_search_task_returns_the_connector_response(settings: Settings, monkeypatch) -> None:
    fake = _install_connector(monkeypatch, response=_response())

    result = search_task({"connector": "github", "query": "rag"}, _ctx(settings))

    assert fake.calls == [("rag", 10)]
    assert result["connector"] == "github"
    assert result["total"] == 1
    assert result["items"][0]["title"] == "pypa/sampleproject"
    assert result["items"][0]["facts"][0] == ("stars", "42")
    assert result["rate"] == {"limit": 10, "remaining": 9, "source": "headers"}


def test_search_task_result_survives_json_round_trip(settings: Settings, monkeypatch) -> None:
    """The worker result is persisted as JSON — tuples must serialize cleanly."""
    _install_connector(monkeypatch, response=_response())

    result = search_task({"connector": "github", "query": "rag"}, _ctx(settings))

    restored = json.loads(json.dumps(result))
    assert restored["items"][0]["facts"][0] == ["stars", "42"]
    assert restored["rate"]["remaining"] == 9


def test_search_task_total_none_stays_none(settings: Settings, monkeypatch) -> None:
    """total=None means 'provider reported no count' — it must not become 0."""
    _install_connector(monkeypatch, response=_response(total=None))

    result = search_task({"connector": "usgs", "query": "earthquake"}, _ctx(settings))

    assert result["total"] is None
    assert result["items"], "honest results are present alongside the null total"


def test_search_task_total_zero_stays_zero(settings: Settings, monkeypatch) -> None:
    """A measured 0 is a real answer — distinct from an unreported total."""
    _install_connector(monkeypatch, response=_response(items=(), total=0))

    result = search_task({"connector": "crossref", "query": "zzz-no-match"}, _ctx(settings))

    assert result["total"] == 0
    assert not result["items"]


def test_search_task_unknown_connector_fails_before_any_network(
    settings: Settings,
) -> None:
    """The REAL build_connector raises on a typo — listing the real names."""
    with pytest.raises(TaskFailure) as excinfo:
        search_task({"connector": "wikipedia", "query": "platypus"}, _ctx(settings))

    message = str(excinfo.value)
    assert "unknown_connector" in message
    assert "github" in message
    assert "frankfurter" in message


def test_search_task_rejects_missing_connector(settings: Settings) -> None:
    with pytest.raises(TaskFailure, match="connector"):
        search_task({"query": "rag"}, _ctx(settings))


def test_search_task_rejects_empty_query(settings: Settings, monkeypatch) -> None:
    fake = _install_connector(monkeypatch, response=_response())
    with pytest.raises(TaskFailure, match="query"):
        search_task({"connector": "github", "query": "   "}, _ctx(settings))
    assert fake.calls == []  # rejected before any connector ran


@pytest.mark.parametrize("bad_limit", [0, -1, 101, "10", 1.5, True])
def test_search_task_rejects_bad_limit(settings: Settings, bad_limit, monkeypatch) -> None:
    fake = _install_connector(monkeypatch, response=_response())
    with pytest.raises(TaskFailure, match="limit must be an integer between 1 and 100"):
        search_task(
            {"connector": "github", "query": "rag", "limit": bad_limit}, _ctx(settings)
        )
    assert fake.calls == []


def test_search_task_connector_error_keeps_reason_and_detail(
    settings: Settings, monkeypatch
) -> None:
    error = ConnectorError("rate_limited", "github is rate limited (HTTP 429)")
    _install_connector(monkeypatch, error=error)

    with pytest.raises(TaskFailure) as excinfo:
        search_task({"connector": "github", "query": "rag"}, _ctx(settings))

    message = str(excinfo.value)
    assert message.startswith("rate_limited:")
    assert "is rate limited (HTTP 429)" in message


def test_search_task_registered_in_task_registry() -> None:
    from synapse.worker.tasks import TASKS

    assert TASKS["search"] is search_task
