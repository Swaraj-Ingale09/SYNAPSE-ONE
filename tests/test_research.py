"""Research pipeline: evidence-first synthesis with real provenance (ADR 0010).

Honesty invariants asserted here:

- **evidence first**: synthesis runs only over material that actually entered
  the prompt — a blocked fetch falls back to the provider's snippet, and with
  nothing usable the job fails (``no_sources``) instead of letting the model
  improvise;
- **per-source degradation**: one blocked URL or one rate-limited connector
  is recorded with its stable reason and never zeroes out the research;
- **totals stay honest**: a provider that reports no count keeps ``total``
  ``None`` — distinct from a measured ``0``;
- **untrusted fence**: every source reaches the model inside the explicit
  untrusted-content fence, with the citation instruction outside it.

No test touches the real network: fakes replace ``build_connector``,
``build_fetcher`` and ``build_gateway`` at the research module.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from synapse.config import Settings
from synapse.connectors import (
    ConnectorError,
    RateLimit,
    ResultItem,
    SearchResponse,
)
from synapse.contracts import JobStatus
from synapse.db import session_scope
from synapse.db.models import Job, JobEvent
from synapse.gateway import NoProviderConfigured
from synapse.net.fetch import FetchBlocked
from synapse.research import ResearchError, run_research
from synapse.worker.tasks import TaskContext, TaskFailure, research_task

# --- fakes ---------------------------------------------------------------------


def _response(items=(), total=None) -> SearchResponse:
    return SearchResponse(
        connector="github",
        domain="coding",
        query="q",
        requested=5,
        page_size=5,
        items=tuple(items),
        total=total,
        fetched_at="2026-10-26T12:00:00+00:00",
        rate=RateLimit(limit=10, remaining=9, source="headers"),
    )


class _FakeSearch:
    def __init__(self, *, items=(), total=None, error: ConnectorError | None = None):
        self._items = items
        self._total = total
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def __enter__(self) -> _FakeSearch:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        self.calls.append((query, limit))
        if self._error is not None:
            raise self._error
        return _response(items=self._items, total=self._total)


def _install_connectors(monkeypatch, specs: dict[str, _FakeSearch]) -> list[str]:
    """Record which connectors the pipeline actually built, in order."""
    built: list[str] = []

    def factory(name: str, _settings: Settings) -> _FakeSearch:
        built.append(name)
        if name not in specs:
            raise ConnectorError("unknown_connector", f"unknown connector {name!r}")
        return specs[name]

    monkeypatch.setattr("synapse.research.build_connector", factory)
    return built


class _FakeResult:
    def __init__(self, url: str, *, text: str, content_type: str = "text/html",
                 status_code: int = 200):
        self.url = url
        self.text = text
        self.content_type = content_type
        self.status_code = status_code


class _FakeFetcher:
    def __init__(self, pages: dict[str, _FakeResult | Exception]):
        self._pages = pages
        self.fetched: list[str] = []

    def __enter__(self) -> _FakeFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def fetch(self, url: str) -> _FakeResult:
        self.fetched.append(url)
        outcome = self._pages.get(url)
        if outcome is None:
            raise FetchBlocked(f"no page configured for {url}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_fetcher(monkeypatch, pages: dict[str, _FakeResult | Exception]) -> _FakeFetcher:
    fetcher = _FakeFetcher(pages)
    monkeypatch.setattr("synapse.research.build_fetcher", lambda _settings: fetcher)
    return fetcher


class _FakeGateway:
    def __init__(self, *, reply_text: str = "Synthesised answer citing [1].",
                 error: Exception | None = None):
        self._reply_text = reply_text
        self._error = error
        self.messages: list[object] = []
        self.ask_calls = 0

    def __enter__(self) -> _FakeGateway:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ask(self, messages, *, model=None, max_tokens=4096, job_id=None):
        self.ask_calls += 1
        self.messages = list(messages)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            text=self._reply_text,
            model=SimpleNamespace(provider="fake", model_id="test-model", alias="test"),
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=22,
                cost_usd=None,
                price_known=False,
                latency_ms=15,
                attempts=1,
            ),
            stop_reason="stop",
        )


def _install_gateway(monkeypatch, gateway: _FakeGateway) -> _FakeGateway:
    monkeypatch.setattr("synapse.research.build_gateway", lambda _settings: gateway)
    return gateway


def _events_sink():
    events: list[tuple[str, dict]] = []
    return events, (lambda event, **fields: events.append((event, fields)))


def _ctx(settings: Settings) -> TaskContext:
    """A TaskContext backed by a live job row (ensure_not_cancelled reads it)."""
    job_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    with session_scope(settings.database_url) as session:
        session.add(
            Job(
                id=job_id,
                task_type="research",
                status=JobStatus.RUNNING.value,
                payload={},
                correlation_id=correlation_id,
                attempts=1,
                max_attempts=3,
            )
        )
    return TaskContext(settings=settings, job_id=job_id, correlation_id=correlation_id)


# --- pipeline tests (no database needed) ----------------------------------------


def test_research_happy_path_uses_fetched_and_snippet_evidence(
    settings: Settings, monkeypatch
) -> None:
    doi_url = "https://doi.org/10.1/example"
    repo_url = "https://github.com/example/repo"
    specs = {
        "openalex": _FakeSearch(
            items=(
                ResultItem(
                    title="Paper A",
                    url=doi_url,
                    date="2026-01-01",
                    snippet="Abstract A",
                    facts=(),
                ),
                # Same URL reported twice — the pipeline must cite it once.
                ResultItem(title="dup", url=doi_url, date=None, snippet=None, facts=()),
            ),
            total=1_234,
        ),
        "github": _FakeSearch(
            items=(
                ResultItem(
                    title="repo",
                    url=repo_url,
                    date=None,
                    snippet="Snippet text B",
                    facts=(),
                ),
            ),
            total=None,  # this provider reports no count — must stay None
        ),
    }
    _install_connectors(monkeypatch, specs)
    fetcher = _install_fetcher(
        monkeypatch,
        {
            doi_url: _FakeResult(doi_url, text="<html><body><p>Full text A</p></body></html>"),
            repo_url: FetchBlocked("robots.txt disallows our user-agent"),
        },
    )
    gateway = _install_gateway(monkeypatch, _FakeGateway())
    events, progress = _events_sink()

    result = run_research(
        question="what is known?",
        settings=settings,
        connectors=("openalex", "github"),
        progress=progress,
    )

    assert result["answer"] == "Synthesised answer citing [1]."
    assert result["candidates"] == 2  # the duplicate URL was dropped

    searches = {entry["connector"]: entry for entry in result["searches"]}
    assert searches["openalex"]["total"] == 1_234
    assert searches["github"]["total"] is None  # not reported, not 0
    assert all(entry["error"] is None for entry in result["searches"])

    # One source fetched (HTTP 200), one degraded to the provider's snippet.
    by_url = {source["url"]: source for source in result["sources"]}
    assert by_url[doi_url]["via"] == "fetched"
    assert by_url[doi_url]["status_code"] == 200
    assert by_url[repo_url]["via"] == "snippet"
    assert [source["index"] for source in result["sources"]] == [1, 2]

    # The blocked fetch is recorded with its real reason — not silently dropped.
    assert result["fetch_skipped"] == [
        {"url": repo_url, "reason": "blocked", "detail": "robots.txt disallows our user-agent"}
    ]
    assert fetcher.fetched == [doi_url, repo_url]

    # The model saw exactly the evidence: numbered, fenced, cited outside.
    assert len(gateway.messages) == 1
    prompt = gateway.messages[0].content
    assert "----- BEGIN UNTRUSTED SOURCES -----" in prompt
    assert "----- END UNTRUSTED SOURCES -----" in prompt
    assert "[1] Paper A" in prompt
    assert "Full text A" in prompt
    assert "Snippet text B" in prompt
    assert "Research question: what is known?" in prompt

    # Real usage, honestly unpriced.
    assert result["usage"]["cost_usd"] is None
    assert result["usage"]["price_known"] is False
    assert result["usage"]["input_tokens"] == 11

    kinds = [name for name, _fields in events]
    assert kinds[0] == "research_started"
    assert kinds.count("research_searched") == 2
    assert "research_source_fetched" in kinds
    assert "research_source_skipped" in kinds
    assert kinds[-1] == "research_synthesis_completed"


def test_research_all_searches_failing_is_reported_with_real_reasons(
    settings: Settings, monkeypatch
) -> None:
    specs = {
        "openalex": _FakeSearch(error=ConnectorError("rate_limited", "wait 60s")),
        "github": _FakeSearch(error=ConnectorError("blocked", "egress refused")),
    }
    _install_connectors(monkeypatch, specs)
    gateway = _install_gateway(monkeypatch, _FakeGateway())

    with pytest.raises(Exception) as excinfo:
        run_research(question="q", settings=settings, connectors=("openalex", "github"))

    assert isinstance(excinfo.value, ResearchError)
    assert excinfo.value.reason == "search_failed"
    assert "openalex: rate_limited" in excinfo.value.detail
    assert "github: blocked" in excinfo.value.detail
    assert gateway.ask_calls == 0  # no evidence, no synthesis


def test_research_no_results_is_no_sources_not_an_answer(
    settings: Settings, monkeypatch
) -> None:
    specs = {"openalex": _FakeSearch(items=(), total=0)}
    _install_connectors(monkeypatch, specs)
    gateway = _install_gateway(monkeypatch, _FakeGateway())

    with pytest.raises(Exception) as excinfo:
        run_research(question="q", settings=settings, connectors=("openalex",))

    assert isinstance(excinfo.value, ResearchError)
    assert excinfo.value.reason == "no_sources"
    assert gateway.ask_calls == 0


def test_research_unknown_connector_fails_before_any_request(
    settings: Settings, monkeypatch
) -> None:
    built = _install_connectors(monkeypatch, {})

    with pytest.raises(ConnectorError) as excinfo:
        run_research(question="q", settings=settings, connectors=("wikipedia",))

    assert excinfo.value.reason == "unknown_connector"
    assert "github" in excinfo.value.detail
    assert built == []  # nothing was built, nothing was searched


def test_research_item_without_url_still_becomes_evidence(
    settings: Settings, monkeypatch
) -> None:
    specs = {
        "openalex": _FakeSearch(
            items=(
                ResultItem(
                    title="no-url", url=None, date=None, snippet="Snippet only", facts=()
                ),
            ),
            total=1,
        )
    }
    _install_connectors(monkeypatch, specs)
    fetcher = _install_fetcher(monkeypatch, {})
    _install_gateway(monkeypatch, _FakeGateway())

    result = run_research(question="q", settings=settings, connectors=("openalex",))

    assert result["sources"][0]["via"] == "snippet"
    assert result["sources"][0]["url"] is None
    assert fetcher.fetched == []  # nothing fetchable, so nothing was fetched


def test_research_gateway_failure_propagates_unwrapped(
    settings: Settings, monkeypatch
) -> None:
    specs = {
        "openalex": _FakeSearch(
            items=(
                ResultItem(
                    title="t", url="https://x.example/a", date=None, snippet="s", facts=()
                ),
            ),
            total=1,
        )
    }
    _install_connectors(monkeypatch, specs)
    _install_fetcher(monkeypatch, {})
    _install_gateway(
        monkeypatch, _FakeGateway(error=NoProviderConfigured("no provider keys configured"))
    )

    with pytest.raises(NoProviderConfigured):
        run_research(question="q", settings=settings, connectors=("openalex",))


def test_research_cancellation_stops_before_synthesis(
    settings: Settings, monkeypatch
) -> None:
    specs = {
        "openalex": _FakeSearch(
            items=(
                ResultItem(
                    title="t", url="https://x.example/a", date=None, snippet="s", facts=()
                ),
            ),
            total=1,
        )
    }
    _install_connectors(monkeypatch, specs)
    _install_fetcher(monkeypatch, {})
    gateway = _install_gateway(monkeypatch, _FakeGateway())

    def cancelled() -> None:
        raise RuntimeError("cancel observed")

    with pytest.raises(RuntimeError, match="cancel observed"):
        run_research(
            question="q",
            settings=settings,
            connectors=("openalex",),
            check_cancelled=cancelled,
        )
    assert gateway.ask_calls == 0


# --- task-level tests (real job row, real event trail) --------------------------


def test_research_task_runs_end_to_end_and_emits_events(
    settings: Settings, monkeypatch
) -> None:
    url = "https://doi.org/10.1/example"
    specs = {
        "openalex": _FakeSearch(
            items=(
                ResultItem(
                    title="Paper A",
                    url=url,
                    date="2026-01-01",
                    snippet="Abs",
                    facts=(),
                ),
            ),
            total=None,
        )
    }
    _install_connectors(monkeypatch, specs)
    _install_fetcher(monkeypatch, {url: _FakeResult(url, text="<p>Full text</p>")})
    _install_gateway(monkeypatch, _FakeGateway())
    ctx = _ctx(settings)

    result = research_task(
        {"question": "  what is known?  ", "connectors": ["openalex"]}, ctx
    )

    assert result["question"] == "what is known?"  # stripped once, at validation
    assert result["answer"] == "Synthesised answer citing [1]."
    assert result["sources"][0]["via"] == "fetched"
    assert result["searches"][0]["total"] is None

    with session_scope(settings.database_url) as session:
        kinds = [
            row.event_type
            for row in session.query(JobEvent).filter(JobEvent.job_id == ctx.job_id)
        ]
    assert "research_started" in kinds
    assert "research_searched" in kinds
    assert "research_synthesis_completed" in kinds


def test_research_task_maps_connector_error_to_task_failure(
    settings: Settings, monkeypatch
) -> None:
    _install_connectors(monkeypatch, {})

    with pytest.raises(TaskFailure) as excinfo:
        research_task({"question": "q", "connectors": ["wikipedia"]}, _ctx(settings))

    message = str(excinfo.value)
    assert message.startswith("unknown_connector:")
    assert "frankfurter" in message  # the real names are listed


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": "   "},
        {"question": 42},
        {"question": "q" * 5000},
        {"question": "q", "connectors": []},
        {"question": "q", "connectors": "openalex"},
        {"question": "q", "connectors": [""]},
        {"question": "q", "connectors": [42]},
        {"question": "q", "max_sources": 0},
        {"question": "q", "max_sources": 99},
        {"question": "q", "max_sources": "3"},
        {"question": "q", "max_sources": True},
        {"question": "q", "model": 42},
        {"question": "q", "max_tokens": 0},
        {"question": "q", "max_tokens": "many"},
    ],
)
def test_research_task_rejects_bad_payloads(settings: Settings, payload) -> None:
    with pytest.raises(TaskFailure):
        research_task(payload, _ctx(settings))


def test_research_task_registered_in_task_registry() -> None:
    from synapse.worker.tasks import TASKS

    assert TASKS["research"] is research_task
