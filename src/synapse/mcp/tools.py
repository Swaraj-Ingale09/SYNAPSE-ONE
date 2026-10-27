"""MCP tool definitions: explicit, bounded capabilities over the core API.

Every tool is a thin-client call to the running SYNAPSE core through the same
:class:`SynapseClient` the CLI uses (ADR 0009 surfaces; the MCP server adds a
fourth). There is no database access, no filesystem exposure, no shell — spec
rule 7: capabilities are explicit, bounded, and permissioned (the /v1 bearer
token gates every call, exactly like the CLI and the dashboard).

Honesty rules (spec 2) survive the wire format:
- tool failures are ``isError`` results carrying the real reason — a failed
  job prints its real ``job.error``, never an apologetic fiction;
- a job still running when the caller's timeout expires says so, with its id,
  instead of inventing a partial answer;
- search totals the provider did not report render as "not reported by
  provider" — never a fake ``0``;
- usage lines say "n/a (price unknown)" when the gateway has no price.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from synapse.cli.client import ApiError, CoreNotRunning, SynapseClient
from synapse.contracts import TERMINAL_STATES, Job, JobStatus
from synapse.research import (
    DEFAULT_RESEARCH_CONNECTORS,
    RESEARCH_DEFAULT_SOURCES,
    RESEARCH_MAX_QUESTION_CHARS,
    RESEARCH_MAX_SOURCES,
)
from synapse.security import redact
from synapse.worker.tasks import ASK_MAX_PROMPT_CHARS, ASK_MAX_TOKENS

ASK_TIMEOUT_SECONDS = 120  # model answers are usually quick
RESEARCH_TIMEOUT_SECONDS = 300  # search + fetch + synthesis takes longer
MAX_TIMEOUT_SECONDS = 900  # hard cap a caller can never exceed
MAX_JOBS_LIST = 200
MAX_SEARCH_LIMIT = 100


class ToolError(Exception):
    """An honest tool failure: ``isError`` result with the real reason."""


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False

    def as_mcp(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": self.text}],
            "isError": self.is_error,
        }


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], SynapseClient], ToolResult] = field(repr=False)

    def as_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# --- argument validation (mirrors the schemas; never trust the client) --------


def _require_str(
    args: dict[str, Any], key: str, *, max_chars: int | None = None
) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} must be a non-empty string")
    cleaned = value.strip()
    if max_chars is not None and len(cleaned) > max_chars:
        raise ToolError(f"{key} exceeds {max_chars} characters (got {len(cleaned)})")
    return cleaned


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} must be a non-empty string when provided")
    return value.strip()


def _int_arg(
    args: dict[str, Any], key: str, default: int, low: int, high: int
) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer between {low} and {high}")
    if not low <= value <= high:
        raise ToolError(f"{key} must be an integer between {low} and {high}")
    return value


def _timeout_arg(args: dict[str, Any], default: int) -> int:
    return _int_arg(args, "timeout_seconds", default, 1, MAX_TIMEOUT_SECONDS)


def _client_error(exc: Exception) -> ToolError:
    """Map transport/API failures to honest tool errors (real reasons only)."""
    if isinstance(exc, CoreNotRunning):
        return ToolError(f"{exc} — start the core with: synapse start")
    if isinstance(exc, ApiError):
        if exc.status_code == 401:
            return ToolError(
                "unauthorized (HTTP 401): the local token does not match the "
                "core's — run `synapse token` to see it, or restart the core"
            )
        return ToolError(f"core rejected the call: {exc}")
    return ToolError(f"internal error: {type(exc).__name__}: {redact(str(exc))}")


def _call(client: SynapseClient, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (CoreNotRunning, ApiError) as exc:
        raise _client_error(exc) from None
    except ToolError:
        raise
    except Exception as exc:  # unexpected — honest message, no stack leak
        raise _client_error(exc) from None


def _wait_for_job(
    client: SynapseClient,
    job_id: str,
    timeout_seconds: int,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Job:
    """Poll a durable job to a terminal state, or return it still running.

    The caller decides how to render the timeout — this function never kills
    the job: it is durable and keeps running on the core regardless.
    """
    sleep = sleep if sleep is not None else time.sleep
    clock = clock if clock is not None else time.monotonic
    deadline = clock() + timeout_seconds
    job = _call(client, lambda: client.get_job(job_id))
    while job.status not in TERMINAL_STATES:
        if clock() >= deadline:
            return job
        sleep(0.5)
        job = _call(client, lambda: client.get_job(job_id))
    return job


def _require_completed(job: Job, timeout_seconds: int) -> dict[str, Any]:
    """Return the result of a completed job, or raise an honest tool error."""
    if job.status is JobStatus.COMPLETED:
        result = job.result
        if not result:
            raise ToolError(
                f"job {job.id} completed but returned no result — nothing to show"
            )
        return result
    if job.status is JobStatus.CANCELLED:
        raise ToolError(f"job {job.id} was cancelled — no result exists")
    if job.status is JobStatus.FAILED:
        raise ToolError(f"job {job.id} failed: {job.error or 'no error recorded'}")
    # Still running (or retrying/waiting): the job survives, the answer is not
    # invented. The caller can poll with synapse_job.
    raise ToolError(
        f"job {job.id} is still {job.status.value} after {timeout_seconds}s — "
        "it keeps running on the core; poll it with the synapse_job tool"
    )


# --- renderers (plain text; honest n/a everywhere) ----------------------------


def _usage_line(result: dict[str, Any]) -> str:
    usage = result.get("usage") or {}
    cost = usage.get("cost_usd")
    cost_text = "n/a (price unknown)" if cost is None else f"${cost:.6f}"
    return (
        f"{result.get('provider')}/{result.get('model')} · "
        f"in {usage.get('input_tokens')} out {usage.get('output_tokens')} tok · "
        f"{usage.get('latency_ms')} ms · {cost_text}"
    )


def _render_ask(result: dict[str, Any]) -> str:
    parts: list[str] = [str(result.get("text") or "(no text in result)")]
    sources = result.get("sources") or []
    if sources:
        lines = ["", "Grounded in stored knowledge:"]
        for source in sources:
            title = source.get("title") or source.get("uri") or "untitled"
            lines.append(f"  - {title}")
        parts.append("\n".join(lines))
    parts.append(f"\n[{_usage_line(result)}]")
    return "\n".join(parts)


def _render_research(result: dict[str, Any]) -> str:
    parts: list[str] = [str(result.get("answer") or "(no answer in result)")]

    sources = result.get("sources") or []
    if sources:
        lines = ["", "Sources (all material the answer was synthesised from):"]
        for source in sources:
            index = source.get("index", "?")
            title = source.get("title") or source.get("url") or "untitled"
            bits = [f"via {source.get('connector')}", str(source.get("via"))]
            if source.get("status_code") is not None:
                bits.append(f"HTTP {source['status_code']}")
            lines.append(f"  [{index}] {title}")
            if source.get("url"):
                lines.append(f"      {source['url']} ({' · '.join(bits)})")
            else:
                lines.append(f"      ({' · '.join(bits)})")
        parts.append("\n".join(lines))

    searches = result.get("searches") or []
    if searches:
        lines = ["", "Searches:"]
        for entry in searches:
            name = entry.get("connector")
            error = entry.get("error")
            if error:
                lines.append(
                    f"  x {name} — {error.get('reason')}: {error.get('detail')}"
                )
            else:
                total = entry.get("total")
                total_text = (
                    "total not reported by provider"
                    if total is None
                    else f"total {total:,}"
                )
                lines.append(
                    f"  + {name} — {entry.get('fetched')} result(s), {total_text}"
                )
        parts.append("\n".join(lines))

    skipped = result.get("fetch_skipped") or []
    if skipped:
        lines = ["", "Not fetched (snippet used instead):"]
        for entry in skipped:
            lines.append(f"  - {entry.get('url')} — {entry.get('reason')}")
        parts.append("\n".join(lines))

    parts.append(f"\n[{_usage_line(result)}]")
    return "\n".join(parts)


def _render_search(payload: dict[str, Any]) -> str:
    lines = [
        f"{payload.get('connector')} ({payload.get('domain')}) — "
        f"query: {payload.get('query')}"
    ]
    total = payload.get("total")
    if total is None:
        lines.append("Total: not reported by provider")
    else:
        lines.append(f"Total: {total:,}")
    items = payload.get("items") or []
    if not items:
        lines.append("No results.")
    for position, item in enumerate(items, start=1):
        lines.append(f"  {position}. {item.get('title') or '(untitled)'}")
        if item.get("url"):
            lines.append(f"     {item['url']}")
        date = item.get("date") or "n/a"
        lines.append(f"     date: {date}")
        if item.get("snippet"):
            lines.append(f"     {item['snippet']}")
    fetched_at = payload.get("fetched_at")
    if fetched_at:
        lines.append(f"\n[fetched at {fetched_at}]")
    return "\n".join(lines)


# --- handlers -----------------------------------------------------------------


def _handle_ask(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    prompt = _require_str(args, "prompt", max_chars=ASK_MAX_PROMPT_CHARS)
    payload: dict[str, Any] = {"prompt": prompt}
    context = _optional_str(args, "context")
    if context is not None:
        payload["context"] = context
    model = _optional_str(args, "model")
    if model is not None:
        payload["model"] = model
    max_tokens = _int_arg(args, "max_tokens", ASK_MAX_TOKENS, 1, ASK_MAX_TOKENS)
    payload["max_tokens"] = max_tokens
    timeout = _timeout_arg(args, ASK_TIMEOUT_SECONDS)

    job = _call(client, lambda: client.create_job("ask", payload=payload))
    final = _wait_for_job(client, job.id, timeout)
    return ToolResult(_render_ask(_require_completed(final, timeout)))


def _handle_research(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    question = _require_str(args, "question", max_chars=RESEARCH_MAX_QUESTION_CHARS)
    payload: dict[str, Any] = {"question": question}
    connectors = args.get("connectors")
    if connectors is not None:
        if not isinstance(connectors, list | tuple) or not connectors:
            raise ToolError("connectors must be a non-empty array of names")
        names: list[str] = []
        for entry in connectors:
            if not isinstance(entry, str) or not entry.strip():
                raise ToolError("connectors entries must be non-empty strings")
            names.append(entry.strip().lower())
        payload["connectors"] = names
    max_sources = _int_arg(
        args, "max_sources", RESEARCH_DEFAULT_SOURCES, 1, RESEARCH_MAX_SOURCES
    )
    payload["max_sources"] = max_sources
    model = _optional_str(args, "model")
    if model is not None:
        payload["model"] = model
    timeout = _timeout_arg(args, RESEARCH_TIMEOUT_SECONDS)

    job = _call(client, lambda: client.create_job("research", payload=payload))
    final = _wait_for_job(client, job.id, timeout)
    return ToolResult(_render_research(_require_completed(final, timeout)))


def _handle_search(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    connector = _require_str(args, "connector")
    query = _require_str(args, "query")
    limit = _int_arg(args, "limit", 10, 1, MAX_SEARCH_LIMIT)
    payload = _call(
        client,
        lambda: client.search(connector=connector, query=query, limit=limit),
    )
    return ToolResult(_render_search(payload))


def _handle_connectors(_args: dict[str, Any], client: SynapseClient) -> ToolResult:
    catalog = _call(client, client.connector_catalog)
    if not catalog:
        return ToolResult("No connectors are registered on the core.")
    lines = ["Available search connectors:"]
    for entry in catalog:
        lines.append(
            f"  {entry.get('name')} ({entry.get('domain')}) — "
            f"{entry.get('description') or 'no description'}"
        )
    return ToolResult("\n".join(lines))


def _handle_jobs(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    status_value = args.get("status")
    status: JobStatus | None = None
    if status_value is not None:
        try:
            status = JobStatus(str(status_value))
        except ValueError:
            valid = ", ".join(state.value for state in JobStatus)
            raise ToolError(f"unknown status {status_value!r} — valid: {valid}") from None
    limit = _int_arg(args, "limit", 50, 1, MAX_JOBS_LIST)
    jobs = _call(client, lambda: client.list_jobs(status=status, limit=limit))
    if not jobs:
        return ToolResult("No jobs match.")
    lines = [f"{len(jobs)} job(s):"]
    for job in jobs:
        lines.append(
            f"  {job.id}  {job.status.value:<10}  {job.task_type}  "
            f"created {job.created_at:%Y-%m-%d %H:%M:%S}"
        )
    return ToolResult("\n".join(lines))


def _job_id_arg(args: dict[str, Any]) -> str:
    return _require_str(args, "job_id", max_chars=64)


def _handle_job(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    job_id = _job_id_arg(args)
    try:
        detail = client.get_job(job_id)
    except (CoreNotRunning, ApiError) as exc:
        raise _client_error(exc) from None
    lines = [
        f"job {detail.id}",
        f"  task:       {detail.task_type}",
        f"  status:     {detail.status.value}",
        f"  attempts:   {detail.attempts}/{detail.max_attempts}",
        f"  created:    {detail.created_at:%Y-%m-%d %H:%M:%S}",
    ]
    if detail.started_at:
        lines.append(f"  started:    {detail.started_at:%Y-%m-%d %H:%M:%S}")
    if detail.finished_at:
        lines.append(f"  finished:   {detail.finished_at:%Y-%m-%d %H:%M:%S}")
    if detail.error:
        lines.append(f"  error:      {detail.error}")
    if detail.result:
        keys = ", ".join(sorted(detail.result))
        lines.append(f"  result keys: {keys}")
    if detail.events:
        lines.append(f"  events ({len(detail.events)}):")
        for event in detail.events[-10:]:
            lines.append(f"    {event.event_type} @ {event.timestamp:%H:%M:%S}")
    return ToolResult("\n".join(lines))


def _handle_cancel_job(args: dict[str, Any], client: SynapseClient) -> ToolResult:
    job_id = _job_id_arg(args)
    job = _call(client, lambda: client.cancel_job(job_id))
    return ToolResult(
        f"cancellation recorded for job {job.id} — status now: {job.status.value}"
    )


def _handle_status(_args: dict[str, Any], client: SynapseClient) -> ToolResult:
    status = _call(client, client.system_status)
    counts = status.get("jobs") or {}
    lines = [
        f"SYNAPSE core v{status.get('version')} — "
        f"api up {status.get('api_uptime_seconds')}s",
        f"queue depth: {status.get('queue_depth')}",
    ]
    if counts:
        lines.append(
            "jobs: " + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
    return ToolResult("\n".join(lines))


# --- registry -----------------------------------------------------------------

ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": ASK_MAX_PROMPT_CHARS,
            "description": "The question to answer",
        },
        "context": {
            "type": "string",
            "description": (
                "Optional untrusted reference text; it is fenced out of the "
                "system prompt and can never change policy"
            ),
        },
        "model": {"type": "string", "description": "Optional model override"},
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": ASK_MAX_TOKENS,
            "description": "Output token cap",
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TIMEOUT_SECONDS,
            "description": f"How long to wait (default {ASK_TIMEOUT_SECONDS})",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "minLength": 1,
            "maxLength": RESEARCH_MAX_QUESTION_CHARS,
            "description": "The research question",
        },
        "connectors": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "Search connectors to use "
                f"(default: {', '.join(DEFAULT_RESEARCH_CONNECTORS)})"
            ),
        },
        "max_sources": {
            "type": "integer",
            "minimum": 1,
            "maximum": RESEARCH_MAX_SOURCES,
            "description": "How many sources to fetch as evidence",
        },
        "model": {"type": "string", "description": "Optional model override"},
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TIMEOUT_SECONDS,
            "description": f"How long to wait (default {RESEARCH_TIMEOUT_SECONDS})",
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "connector": {
            "type": "string",
            "minLength": 1,
            "description": "Connector name (see synapse_connectors)",
        },
        "query": {"type": "string", "minLength": 1, "description": "Search query"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SEARCH_LIMIT,
            "description": "Max results (default 10)",
        },
    },
    "required": ["connector", "query"],
    "additionalProperties": False,
}

EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

JOBS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "Optional status filter (e.g. running, queued, failed)",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_JOBS_LIST,
            "description": "Max jobs to list (default 50)",
        },
    },
    "additionalProperties": False,
}

JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "minLength": 1, "description": "The job id"},
    },
    "required": ["job_id"],
    "additionalProperties": False,
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="synapse_ask",
        description=(
            "Ask the SYNAPSE core a question through its model gateway and get "
            "a real answer with usage. The job is durable: if it outlives the "
            "timeout it keeps running and can be polled with synapse_job."
        ),
        input_schema=ASK_SCHEMA,
        handler=_handle_ask,
    ),
    Tool(
        name="synapse_research",
        description=(
            "Research a question evidence-first: the core searches free public "
            "APIs, fetches sources through its SSRF gate, and synthesises an "
            "answer cited to the material actually provided. No sources, no "
            "answer — the tool fails with the real reason instead of guessing."
        ),
        input_schema=RESEARCH_SCHEMA,
        handler=_handle_research,
    ),
    Tool(
        name="synapse_search",
        description=(
            "Search a free public API connector (OpenAlex, GitHub, "
            "StackOverflow, arXiv, Crossref, …) and get real results with "
            "provider-reported totals (explicitly marked when not reported)."
        ),
        input_schema=SEARCH_SCHEMA,
        handler=_handle_search,
    ),
    Tool(
        name="synapse_connectors",
        description="List the search connectors registered on the core.",
        input_schema=EMPTY_SCHEMA,
        handler=_handle_connectors,
    ),
    Tool(
        name="synapse_jobs",
        description="List durable jobs on the core, optionally filtered by status.",
        input_schema=JOBS_SCHEMA,
        handler=_handle_jobs,
    ),
    Tool(
        name="synapse_job",
        description=(
            "Inspect one durable job: status, attempts, real error if it "
            "failed, result keys if it completed, and its recent events."
        ),
        input_schema=JOB_ID_SCHEMA,
        handler=_handle_job,
    ),
    Tool(
        name="synapse_cancel_job",
        description=(
            "Request cancellation of a durable job. Cancellation is "
            "cooperative: the job reaches a terminal state on its next check."
        ),
        input_schema=JOB_ID_SCHEMA,
        handler=_handle_cancel_job,
    ),
    Tool(
        name="synapse_status",
        description="Real core health: version, api uptime, queue depth, job counts.",
        input_schema=EMPTY_SCHEMA,
        handler=_handle_status,
    ),
)

TOOL_REGISTRY: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def call_tool(
    name: str, arguments: dict[str, Any], client: SynapseClient
) -> ToolResult:
    """Execute one tool; failures become honest ``isError`` results."""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        available = ", ".join(sorted(TOOL_REGISTRY))
        return ToolResult(
            redact(f"unknown tool {name!r}: available: {available}"),
            is_error=True,
        )
    try:
        return tool.handler(arguments, client)
    except ToolError as exc:
        return ToolResult(redact(str(exc)), is_error=True)
