"""MCP server: JSON-RPC protocol, tool honesty, stdio loop (ADR 0011).

Honesty invariants asserted here, on the wire:
- initialize negotiates protocol versions honestly (echo supported, else
  answer with the version we speak);
- notifications never receive responses; unknown methods, bad ids, bad
  params, and parse errors each carry their own stable code and reason;
- a job that failed surfaces its real ``job.error`` as an ``isError``
  result; a job still running when the caller's timeout expires says so
  with its id — never an invented answer;
- search totals the provider did not report render as "not reported by
  provider", never a fake 0;
- stdout carries protocol messages only (serve_stdio proven with in-memory
  streams).

No test touches the real network: a fake client stands in for SynapseClient.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from synapse.contracts import Job, JobStatus
from synapse.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from synapse.mcp.server import McpServer, serve_stdio
from synapse.mcp.tools import (
    RESEARCH_TIMEOUT_SECONDS,
    TOOL_REGISTRY,
    ToolResult,
    call_tool,
)

# --- fake client --------------------------------------------------------------


def _job(
    job_id: str,
    status: JobStatus,
    *,
    result: dict | None = None,
    error: str | None = None,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=job_id,
        task_type="ask",
        status=status,
        payload={},
        result=result,
        error=error,
        user_scope="tester",
        correlation_id="corr-1",
        attempts=1,
        max_attempts=3,
        cancel_requested=False,
        created_at=now,
        updated_at=now,
    )


class _FakeClient:
    """Stands in for SynapseClient; records every call."""

    def __init__(self, *, job: Job | None = None, search_payload: dict | None = None):
        self.job = job
        self.search_payload = search_payload or {}
        self.created: list[tuple[str, dict]] = []
        self.cancels: list[str] = []
        self.get_job_calls = 0

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def create_job(self, task_type: str, payload: dict | None = None, **_kw):
        self.created.append((task_type, payload or {}))
        assert self.job is not None
        return self.job

    def get_job(self, _job_id: str) -> Job:
        self.get_job_calls += 1
        assert self.job is not None
        return self.job

    def list_jobs(self, status=None, limit=50):
        return [_job("job-1", JobStatus.QUEUED)]

    def cancel_job(self, job_id: str) -> Job:
        self.cancels.append(job_id)
        return _job(job_id, JobStatus.CANCELLED)

    def system_status(self) -> dict:
        return {
            "version": "0.0.0",
            "api_uptime_seconds": 12.5,
            "jobs": {"queued": 1},
            "queue_depth": 1,
        }

    def search(self, connector: str, query: str, limit: int = 10) -> dict:
        return dict(self.search_payload)

    def connector_catalog(self) -> list[dict]:
        return [
            {"name": "openalex", "domain": "research", "description": "papers"},
        ]


def _server(client: _FakeClient, monkeypatch) -> McpServer:
    """A server whose tool calls receive the fake client (settings unused)."""
    monkeypatch.setattr("synapse.mcp.server.SynapseClient", lambda _settings: client)
    return McpServer(settings=None)  # type: ignore[arg-type]


def _request(method: str, params: dict | None = None, message_id=1) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# --- protocol tests -----------------------------------------------------------


def test_initialize_echoes_a_supported_protocol_version() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    for version in SUPPORTED_PROTOCOL_VERSIONS:
        response = server.handle(_request("initialize", {"protocolVersion": version}))
        assert response["result"]["protocolVersion"] == version


def test_initialize_answers_with_our_version_for_unknown_ones() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(
        _request("initialize", {"protocolVersion": "1999-01-01"})
    )
    assert response["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    info = response["result"]["serverInfo"]
    assert info["name"] == "synapse-one"
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False


def test_notification_gets_no_response() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert server.handle(message) is None


def test_unknown_method_is_method_not_found() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(_request("resources/list"))
    assert response["error"]["code"] == METHOD_NOT_FOUND
    assert "resources/list" in response["error"]["message"]


def test_parse_error_has_its_own_code_and_null_id() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    line = server.handle_line("{not json")
    payload = json.loads(line)
    assert payload["error"]["code"] == PARSE_ERROR
    assert payload["id"] is None


def test_wrong_jsonrpc_version_is_invalid_request() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle({"jsonrpc": "1.0", "id": 5, "method": "ping"})
    assert response["error"]["code"] == INVALID_REQUEST
    assert response["id"] is None


@pytest.mark.parametrize("bad_id", [True, {"a": 1}, [1]])
def test_invalid_id_type_is_invalid_request(bad_id) -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle({"jsonrpc": "2.0", "id": bad_id, "method": "ping"})
    assert response["error"]["code"] == INVALID_REQUEST


def test_params_must_be_an_object() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_tools_call_requires_a_name() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(_request("tools/call", {"arguments": {}}))
    assert response["error"]["code"] == INVALID_PARAMS
    assert "name" in response["error"]["message"]


def test_tools_call_arguments_must_be_an_object() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(
        _request("tools/call", {"name": "synapse_status", "arguments": [1]})
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_tools_list_lists_the_registry_with_schemas() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    response = server.handle(_request("tools/list"))
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == set(TOOL_REGISTRY)
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_ping_returns_empty_object() -> None:
    server = McpServer(settings=None)  # type: ignore[arg-type]
    assert server.handle(_request("ping"))["result"] == {}


# --- tool honesty tests -------------------------------------------------------


def test_ask_renders_answer_and_unpriced_usage(monkeypatch) -> None:
    client = _FakeClient(
        job=_job(
            "j1",
            JobStatus.COMPLETED,
            result={
                "text": "42",
                "provider": "test",
                "model": "m",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "cost_usd": None,
                    "latency_ms": 9,
                },
            },
        )
    )
    result = call_tool("synapse_ask", {"prompt": "q"}, client)
    assert result.is_error is False
    assert "42" in result.text
    assert "n/a (price unknown)" in result.text


def test_ask_failed_job_surfaces_the_real_error(monkeypatch) -> None:
    client = _FakeClient(
        job=_job("j2", JobStatus.FAILED, error="no_provider_configured: no keys")
    )
    result = call_tool("synapse_ask", {"prompt": "q"}, client)
    assert result.is_error is True
    assert "no_provider_configured: no keys" in result.text


def test_ask_cancelled_job_is_an_error_not_an_answer(monkeypatch) -> None:
    client = _FakeClient(job=_job("j3", JobStatus.CANCELLED))
    result = call_tool("synapse_ask", {"prompt": "q"}, client)
    assert result.is_error is True
    assert "cancelled" in result.text


def test_ask_timeout_reports_the_running_job_instead_of_inventing(
    monkeypatch,
) -> None:
    """A still-running job is reported honestly, with its id, never faked."""
    client = _FakeClient(job=_job("j4", JobStatus.RUNNING))
    monkeypatch.setattr("synapse.mcp.tools.time.sleep", lambda _s: None)

    # A clock that jumps far past any deadline on its first reading.
    ticks = {"now": 0.0}

    def jump_clock() -> float:
        ticks["now"] += 10_000.0
        return ticks["now"]

    monkeypatch.setattr("synapse.mcp.tools.time.monotonic", jump_clock)
    result = call_tool("synapse_ask", {"prompt": "q", "timeout_seconds": 1}, client)
    assert result.is_error is True
    assert "j4" in result.text
    assert "still running" in result.text


def test_ask_validates_before_any_client_call(monkeypatch) -> None:
    client = _FakeClient(job=_job("j5", JobStatus.COMPLETED, result={"text": "x"}))
    result = call_tool("synapse_ask", {"prompt": "   "}, client)
    assert result.is_error is True
    assert "non-empty" in result.text
    assert client.created == []  # nothing was submitted


def test_research_renders_sources_searches_and_skips(monkeypatch) -> None:
    client = _FakeClient(
        job=_job(
            "r1",
            JobStatus.COMPLETED,
            result={
                "answer": "Cited [1].",
                "provider": "test",
                "model": "m",
                "sources": [
                    {
                        "index": 1,
                        "title": "Paper",
                        "url": "https://doi.org/x",
                        "connector": "openalex",
                        "via": "fetched",
                        "status_code": 200,
                    }
                ],
                "searches": [
                    {"connector": "openalex", "fetched": 5, "total": None, "error": None},
                    {
                        "connector": "github",
                        "fetched": None,
                        "total": None,
                        "error": {"reason": "rate_limited", "detail": "wait"},
                    },
                ],
                "fetch_skipped": [
                    {"url": "https://blocked.example", "reason": "blocked"}
                ],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cost_usd": None,
                    "latency_ms": 3,
                },
            },
        )
    )
    result = call_tool("synapse_research", {"question": "q"}, client)
    assert result.is_error is False
    assert "Cited [1]." in result.text
    assert "total not reported by provider" in result.text
    assert "rate_limited" in result.text
    assert "https://blocked.example — blocked" in result.text


def test_research_payload_carries_validated_fields(monkeypatch) -> None:
    client = _FakeClient(
        job=_job("r2", JobStatus.COMPLETED, result={"answer": "a", "usage": {}})
    )
    call_tool(
        "synapse_research",
        {"question": " q ", "connectors": ["OpenAlex"], "max_sources": 2},
        client,
    )
    task_type, payload = client.created[0]
    assert task_type == "research"
    assert payload["question"] == "q"
    assert payload["connectors"] == ["openalex"]
    assert payload["max_sources"] == 2


def test_search_renders_unreported_total_honestly(monkeypatch) -> None:
    client = _FakeClient(
        search_payload={
            "connector": "github",
            "domain": "coding",
            "query": "q",
            "total": None,
            "items": [{"title": "repo", "url": "https://x", "date": None}],
            "fetched_at": "2026-10-27T00:00:00+00:00",
        }
    )
    result = call_tool("synapse_search", {"connector": "github", "query": "q"}, client)
    assert result.is_error is False
    assert "Total: not reported by provider" in result.text
    assert "date: n/a" in result.text


def test_unknown_tool_lists_the_real_ones(monkeypatch) -> None:
    client = _FakeClient()
    result = call_tool("synapse_nope", {}, client)
    assert result.is_error is True
    assert "synapse_ask" in result.text  # the real names are listed


def test_jobs_and_status_and_cancel(monkeypatch) -> None:
    client = _FakeClient()
    jobs = call_tool("synapse_jobs", {}, client)
    assert jobs.is_error is False and "job-1" in jobs.text

    status = call_tool("synapse_status", {}, client)
    assert status.is_error is False and "queue depth: 1" in status.text

    cancel = call_tool("synapse_cancel_job", {"job_id": "job-9"}, client)
    assert cancel.is_error is False and "cancelled" in cancel.text
    assert client.cancels == ["job-9"]


def test_jobs_rejects_unknown_status_filter(monkeypatch) -> None:
    client = _FakeClient()
    result = call_tool("synapse_jobs", {"status": "sideways"}, client)
    assert result.is_error is True
    assert "valid" in result.text


def test_connector_catalog_is_rendered(monkeypatch) -> None:
    client = _FakeClient()
    result = call_tool("synapse_connectors", {}, client)
    assert "openalex" in result.text


def test_tool_result_as_mcp_shape() -> None:
    shaped = ToolResult("hello", is_error=True).as_mcp()
    assert shaped == {
        "content": [{"type": "text", "text": "hello"}],
        "isError": True,
    }


def test_research_default_timeout_is_the_documented_one() -> None:
    # Guard against accidental drift of the documented default.
    assert RESEARCH_TIMEOUT_SECONDS == 300


# --- stdio loop tests ---------------------------------------------------------


def _drive(server: McpServer, lines: list[str]) -> list[dict]:
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
    assert code == 0
    assert "waiting for MCP client" in stderr.getvalue()  # diagnostics -> stderr
    raw_lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in raw_lines]


def test_stdio_session_initialize_list_call(monkeypatch) -> None:
    client = _FakeClient(
        job=_job("j1", JobStatus.COMPLETED, result={"text": "ok", "usage": {}})
    )
    server = _server(client, monkeypatch)
    responses = _drive(
        server,
        [
            json.dumps(_request("initialize", {"protocolVersion": "2025-03-26"})),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(_request("tools/list")),
            json.dumps(
                _request(
                    "tools/call",
                    {"name": "synapse_ask", "arguments": {"prompt": "hi"}},
                )
            ),
        ],
    )
    # The notification produced no response; the three requests did.
    assert len(responses) == 3
    assert [r["id"] for r in responses] == [1, 1, 1]
    call = responses[2]["result"]
    assert call["isError"] is False
    assert "ok" in call["content"][0]["text"]


def test_stdio_bad_line_does_not_kill_the_session(monkeypatch) -> None:
    client = _FakeClient()
    server = _server(client, monkeypatch)
    responses = _drive(
        server,
        ["{broken", json.dumps(_request("ping"))],
    )
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[1]["result"] == {}


def test_stdio_tool_error_is_an_iserror_result_not_a_protocol_error(
    monkeypatch,
) -> None:
    client = _FakeClient(job=_job("j2", JobStatus.FAILED, error="real reason here"))
    server = _server(client, monkeypatch)
    responses = _drive(
        server,
        [
            json.dumps(
                _request(
                    "tools/call",
                    {"name": "synapse_ask", "arguments": {"prompt": "q"}},
                )
            )
        ],
    )
    result = responses[0]["result"]  # protocol-level success...
    assert result["isError"] is True  # ...tool-level honest failure
    assert "real reason here" in result["content"][0]["text"]
