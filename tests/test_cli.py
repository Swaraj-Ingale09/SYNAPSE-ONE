"""CLI behaviour tests: help, exit codes, JSON mode, honest failure paths (spec 10.1, 16.3)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from synapse.cli.main import EXIT_NOT_RUNNING, app

runner = CliRunner()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch) -> None:
    """Point the CLI at an empty state home and a closed port (no core running)."""
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SYNAPSE_API_PORT", "59871")
    monkeypatch.delenv("SYNAPSE_TOKEN", raising=False)


def test_help_lists_real_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("start", "stop", "status", "doctor", "submit", "jobs", "events",
                    "token", "ask", "ingest", "dashboard", "search"):
        assert command in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_status_without_core_exits_not_running(isolated_env) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == EXIT_NOT_RUNNING
    assert "not running" in result.output


def test_status_json_without_core(isolated_env) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == EXIT_NOT_RUNNING


def test_job_inspect_without_core_exits_not_running(isolated_env) -> None:
    result = runner.invoke(app, ["job", "inspect", "some-id"])
    assert result.exit_code == EXIT_NOT_RUNNING
    assert "synapse start" in result.output


def test_submit_rejects_invalid_json_before_contacting_core(isolated_env) -> None:
    result = runner.invoke(app, ["submit", "echo", "--data", "{not json"])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_submit_rejects_non_object_payload(isolated_env) -> None:
    result = runner.invoke(app, ["submit", "echo", "--data", "[1, 2]"])
    assert result.exit_code == 1
    assert "JSON object" in result.output


def test_job_group_help() -> None:
    result = runner.invoke(app, ["job", "--help"])
    assert result.exit_code == 0
    assert "inspect" in result.output
    assert "cancel" in result.output


# --- token / dashboard / ask (ADR 0006) -------------------------------------------


def test_token_prints_the_real_token(tmp_path, monkeypatch) -> None:
    """``token`` reads the same file ``get_settings`` hands the API client."""
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SYNAPSE_API_PORT", "59871")
    monkeypatch.delenv("SYNAPSE_TOKEN", raising=False)
    token_file = tmp_path / "state" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("tok-local-123", encoding="utf-8")

    result = runner.invoke(app, ["token"])

    assert result.exit_code == 0
    assert "tok-local-123" in result.output


def test_token_without_token_file_is_an_honest_failure(isolated_env) -> None:
    """Before the first `synapse start` no token exists — say so, don't invent one."""
    result = runner.invoke(app, ["token"])

    assert result.exit_code == 1
    assert "synapse start" in result.output


def test_dashboard_missing_build_exits_with_the_build_hint(
    isolated_env, tmp_path, monkeypatch
) -> None:
    """A missing dashboard build must produce a clear instruction, not a blank UI."""
    from synapse.cli import main as cli_main

    monkeypatch.setattr(cli_main, "dashboard_dist_dir", lambda: tmp_path / "no-dist")
    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 1
    assert "npm run build" in result.output


def test_ask_rejects_empty_prompt_before_contacting_core(isolated_env) -> None:
    """Same rule the worker enforces — reject locally, with no core needed."""
    result = runner.invoke(app, ["ask", "   "])

    assert result.exit_code == 1
    assert "non-empty" in result.output


# --- search (ADR 0008: free public-API connectors) --------------------------------------


def test_search_without_connector_lists_them_all(isolated_env) -> None:
    """No argument = the catalog. Every registered connector is discoverable."""
    result = runner.invoke(app, ["search"])

    assert result.exit_code == 0
    for name in ("github", "stackoverflow", "openalex", "crossref",
                 "europepmc", "usgs", "weather", "frankfurter"):
        assert name in result.output


def test_search_json_catalog_is_machine_readable(isolated_env) -> None:
    import json

    result = runner.invoke(app, ["--json", "search"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert {row["name"] for row in payload} >= {"github", "europepmc", "frankfurter"}


def test_search_unknown_connector_exits_with_the_real_names() -> None:
    """A typo must say so and list the truth — never render an empty 'success'."""
    result = runner.invoke(app, ["search", "wikipedia", "something"])

    assert result.exit_code == 1
    assert "unknown connector" in result.output
    assert "github" in result.output


def test_search_empty_query_is_rejected_locally(isolated_env) -> None:
    """`search github` with no query fails before any network or core involvement."""
    result = runner.invoke(app, ["search", "github"])

    assert result.exit_code == 1
    assert "invalid_query" in result.output
    assert "non-empty" in result.output


def _fake_connector_factory(response=None, error=None):
    class _FakeConnector:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def search(self, query, *, limit=10):
            if error is not None:
                raise error
            return response

    return lambda name, settings: _FakeConnector()


def test_search_renders_results_and_json(isolated_env, monkeypatch) -> None:
    import json

    from synapse.connectors import RateLimit, ResultItem, SearchResponse

    response = SearchResponse(
        connector="github",
        domain="coding",
        query="rag",
        requested=10,
        page_size=10,
        items=(
            ResultItem(
                title="pypa/sampleproject",
                url="https://github.com/pypa/sampleproject",
                date="2026-01-02T03:04:05Z",
                snippet="Example project",
                facts=(("stars", "42"), ("language", "n/a")),
            ),
        ),
        total=1,
        fetched_at="2026-10-25T12:00:00+00:00",
        rate=RateLimit(limit=10, remaining=9, source="headers"),
    )
    monkeypatch.setattr(
        "synapse.cli.main.build_connector", _fake_connector_factory(response=response)
    )

    result = runner.invoke(app, ["search", "github", "rag"])
    assert result.exit_code == 0
    assert "pypa/sampleproject" in result.output
    assert "10/10" not in result.output  # rate renders as reported: 9 remaining of 10
    assert "9/10" in result.output
    assert "language=n/a" in result.output  # absent fact stays n/a, not 0

    json_result = runner.invoke(app, ["--json", "search", "github", "rag"])
    assert json_result.exit_code == 0
    payload = json.loads(json_result.output)
    assert payload["connector"] == "github"
    assert payload["items"][0]["title"] == "pypa/sampleproject"
    assert payload["items"][0]["facts"][1] == ["language", "n/a"]  # tuples → JSON arrays
    assert payload["total"] == 1
    assert payload["rate"] == {"limit": 10, "remaining": 9, "source": "headers"}


def test_search_failure_prints_the_real_reason(isolated_env, monkeypatch) -> None:
    from synapse.connectors import ConnectorError

    error = ConnectorError(
        "rate_limited", "github is rate limited (HTTP 429); server says wait 60s"
    )
    monkeypatch.setattr("synapse.cli.main.build_connector", _fake_connector_factory(error=error))

    result = runner.invoke(app, ["search", "github", "rag"])

    assert result.exit_code == 1
    assert "rate_limited" in result.output
    assert "wait 60s" in result.output


# --- search --queue (durable job path, ADR 0008) --------------------------------------


def test_search_queue_without_connector_or_query_is_an_honest_error(isolated_env) -> None:
    """--queue with no arguments must say exactly what is missing — no silent catalog."""
    result = runner.invoke(app, ["search", "--queue"])

    assert result.exit_code == 1
    assert "--queue" in result.output
    assert "CONNECTOR" in result.output
    assert "QUERY" in result.output


def test_search_queue_with_connector_but_no_query_is_rejected(isolated_env) -> None:
    result = runner.invoke(app, ["search", "github", "--queue"])

    assert result.exit_code == 1
    assert "QUERY" in result.output


def test_search_queue_submits_a_durable_search_job_and_renders(
    isolated_env, monkeypatch
) -> None:
    """--queue submits task "search" with the real payload and renders its result."""
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from synapse.connectors import RateLimit, ResultItem, SearchResponse
    from synapse.contracts import Job, JobStatus

    response = SearchResponse(
        connector="github",
        domain="coding",
        query="rag",
        requested=10,
        page_size=10,
        items=(
            ResultItem(
                title="pypa/sampleproject",
                url="https://github.com/pypa/sampleproject",
                date="2026-01-02T03:04:05Z",
                snippet="Example project",
                facts=(("stars", "42"), ("language", "n/a")),
            ),
        ),
        total=None,  # provider reported no count — must render as n/a, never 0
        fetched_at="2026-10-25T12:00:00+00:00",
        rate=RateLimit(limit=10, remaining=9, source="headers"),
    )
    # The worker result reaches the CLI as JSON — lists, not tuples.
    result_dict = json.loads(json.dumps(asdict(response)))
    final = Job(
        id="job-search-1",
        task_type="search",
        status=JobStatus.COMPLETED,
        payload={},
        result=result_dict,
        user_scope="local",
        correlation_id="corr-1",
        attempts=1,
        max_attempts=3,
        cancel_requested=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    captured: dict = {}

    def fake_submit(state, settings, task_type, payload, *, timeout, project):
        captured.update(
            task_type=task_type, payload=payload, timeout=timeout, project=project
        )
        return final

    monkeypatch.setattr("synapse.cli.main._submit_and_wait", fake_submit)

    result = runner.invoke(app, ["search", "github", "rag", "--queue"])

    assert result.exit_code == 0
    assert captured["task_type"] == "search"
    assert captured["payload"] == {"connector": "github", "query": "rag", "limit": 10}
    assert captured["project"] is None
    assert "pypa/sampleproject" in result.output
    assert "total n/a (this API reports no total)" in result.output  # null stays null
    assert "9/10" in result.output
    assert "language=n/a" in result.output
