"""Connector layer: verified parsers, honest failures, server-honoring rate limits (ADR 0008).

Fixtures mirror live responses captured on 2026-10-25 (field names and quirks
included: GitHub's search-specific limit of 10/min, Stack Exchange quotas in
the envelope, OpenAlex's inverted abstract index, USGS's missing total). No
test touches the real network — a MockTransport answers after the SSRF gate.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from synapse.config import Settings
from synapse.connectors import (
    REGISTRY,
    ConnectorError,
    build_connector,
    connector_catalog,
    connector_names,
)
from synapse.connectors.base import RateLimit, ResultItem, SearchResponse
from synapse.connectors.code import GitHubConnector, StackOverflowConnector
from synapse.connectors.knowledge import (
    CrossrefConnector,
    EuropePmCConnector,
    OpenAlexConnector,
)
from synapse.connectors.world import FrankfurterConnector, UsgsConnector, WeatherConnector
from synapse.net.fetch import SafeFetcher
from synapse.net.ssrf import assert_safe_ip

PUBLIC_IP = "93.184.216.34"  # public example IP so no test resolves the real DNS


def _settings() -> Settings:
    return Settings(
        api_host="127.0.0.1",
        api_port=8765,
        database_url="postgresql+psycopg2://user:pass@localhost/test",
        database_password_set=True,
        token=None,
        state_dir=Path("C:/synapse-test-state"),
    )


class _Traffic:
    """Records sleeps and API calls (robots requests excluded) for assertions."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.api_calls: list[str] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(hostname: str, port: int) -> list[str]:
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return [PUBLIC_IP]
        assert_safe_ip(hostname)
        return [str(ip)]

    monkeypatch.setattr("synapse.net.ssrf.resolve_and_validate", fake_resolve)


def _connector(
    monkeypatch: pytest.MonkeyPatch,
    connector_type: type,
    handler,
    traffic: _Traffic | None = None,
):
    """Build a connector on a MockTransport with politeness sleeping disabled."""
    _fake_dns(monkeypatch)
    traffic = traffic or _Traffic()
    fetcher = SafeFetcher(
        settings=None,  # type: ignore[arg-type] - unused by the fetch path
        transport=httpx.MockTransport(handler),
        min_interval_seconds=0.0,
    )
    return connector_type(fetcher, sleep=traffic.sleep), traffic


def _api_handler(traffic: _Traffic, respond):
    """Route: robots.txt → 404 (default allow); everything else → ``respond``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        traffic.api_calls.append(f"{request.url.host}{request.url.path}")
        return respond(request)

    return handler


# --- registry & construction -----------------------------------------------------


def test_registry_covers_the_required_domains() -> None:
    assert connector_names() == [
        "crossref",
        "europepmc",
        "frankfurter",
        "github",
        "openalex",
        "stackoverflow",
        "usgs",
        "weather",
    ]
    domains = {entry["domain"] for entry in connector_catalog()}
    assert domains == {"coding", "knowledge", "medical", "earth", "weather", "finance"}
    for entry in connector_catalog():
        assert entry["name"] in REGISTRY
        assert entry["description"].strip()  # every listing states what it sources


@pytest.mark.parametrize("name", connector_names())
def test_every_connector_declares_a_pacing_floor(name: str) -> None:
    """Local politeness exists before any server header is seen (ADR 0008)."""
    assert REGISTRY[name].min_interval_seconds > 0


def test_unknown_connector_lists_the_real_names() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        build_connector("wikipedia", _settings())
    assert excinfo.value.reason == "unknown_connector"
    assert "github" in excinfo.value.detail
    assert "frankfurter" in excinfo.value.detail


def test_build_connector_opens_and_closes_cleanly() -> None:
    connector = build_connector("github", _settings())
    try:
        assert connector.name == "github"
    finally:
        connector.close()


# --- contract: query validation and honest failure states -------------------------


@pytest.mark.parametrize(
    "connector_type",
    [
        GitHubConnector,
        StackOverflowConnector,
        OpenAlexConnector,
        CrossrefConnector,
        EuropePmCConnector,
        FrankfurterConnector,
        WeatherConnector,
    ],
)
def test_empty_query_is_rejected_before_any_network(
    monkeypatch: pytest.MonkeyPatch, connector_type: type
) -> None:
    connector, _ = _connector(
        monkeypatch,
        connector_type,
        lambda request: pytest.fail("no request may happen for an empty query"),
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("   ")
    assert excinfo.value.reason == "invalid_query"
    connector.close()


def test_non_json_body_is_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        lambda request: httpx.Response(200, text="<html>not json</html>"),
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "invalid_response"
    connector.close()


def test_robots_disallow_maps_to_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, json={})  # pragma: no cover - must be unreachable

    connector, traffic = _connector(monkeypatch, GitHubConnector, handler)
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "blocked"
    assert "robots" in excinfo.value.detail
    assert traffic.api_calls == []
    connector.close()


def test_http_500_is_http_error_with_body_detail_and_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic, lambda request: httpx.Response(500, text="upstream exploded")
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "http_error"
    assert "HTTP 500" in excinfo.value.detail
    assert "upstream exploded" in excinfo.value.detail
    assert len(traffic.api_calls) == 1  # only 429 is ever retried
    assert traffic.sleeps == []
    connector.close()


# --- rate limits: 429-only retries, Retry-After honored, bounded -------------------


def _github_payload() -> dict:
    return {
        "total_count": 11935,
        "items": [
            {
                "full_name": "public-apis/public-apis",
                "html_url": "https://github.com/public-apis/public-apis",
                "description": "A collective list of free APIs",
                "stargazers_count": 492035,
                "language": "Python",
                "updated_at": "2026-10-25T14:15:36Z",
                "license": {"spdx_id": "MIT"},
            }
        ],
    }


def test_429_retries_after_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(
            200,
            json=_github_payload(),
            headers={"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"},
        )

    connector, _ = _connector(monkeypatch, GitHubConnector, _api_handler(traffic, respond), traffic)
    response = connector.search("rag")
    assert traffic.sleeps == [2.0]
    assert len(calls) == 2
    assert response.total == 11935
    connector.close()


def test_429_with_a_long_retry_after_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 60s wait must be reported, not slept through in an interactive CLI."""
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(429, headers={"retry-after": "120"}, text="busy"),
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "rate_limited"
    assert "120" in excinfo.value.detail
    assert traffic.sleeps == []
    assert len(traffic.api_calls) == 1  # refused before waiting, not after
    connector.close()


def test_persistent_429_is_bounded_to_two_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(429, headers={"retry-after": "0"}, text="busy"),
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "rate_limited"
    assert len(traffic.api_calls) == 3  # initial attempt + exactly two retries
    assert traffic.sleeps == [0.0, 0.0]
    connector.close()


def test_403_with_exhausted_quota_is_rate_limited_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub signals exhaustion with 403 + X-RateLimit-Remaining: 0 (verified)."""
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "10"},
                text="API rate limit exceeded",
            ),
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("rag")
    assert excinfo.value.reason == "rate_limited"
    assert len(traffic.api_calls) == 1
    assert traffic.sleeps == []
    connector.close()


def test_rate_limit_headers_are_reported_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200,
                json=_github_payload(),
                headers={"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"},
            ),
        ),
        traffic,
    )
    response = connector.search("rag")
    assert response.rate == RateLimit(limit=10, remaining=9, source="headers")
    connector.close()


def test_retry_after_http_date_is_interpreted(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, _ = _connector(
        monkeypatch, GitHubConnector, lambda request: httpx.Response(200, json={})
    )
    when = datetime.now(UTC).replace(tzinfo=None)
    http_date = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    wait = connector._retry_wait({"retry-after": http_date})  # noqa: SLF001 - unit under test
    assert 0.0 <= wait <= 5.0  # roughly "now + 0s", parsed as an HTTP-date
    assert connector._retry_wait({}) == 1.0  # noqa: SLF001 - bounded default when absent
    assert connector._retry_wait({"retry-after": "soon"}) == 1.0  # noqa: SLF001
    connector.close()


def test_server_backoff_envelope_is_honored_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        StackOverflowConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200, json={"items": [], "has_more": False, "backoff": 5}
            ),
        ),
        traffic,
    )
    connector.search("python")
    assert traffic.sleeps == []  # recorded, not slept mid-response
    connector.search("python")
    assert len(traffic.sleeps) == 1
    assert 4.5 <= traffic.sleeps[0] <= 5.0  # waited out the server's backoff
    connector.close()


def test_huge_server_backoff_refuses_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        StackOverflowConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200, json={"items": [], "backoff": 3600}
            ),
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("python")
    assert excinfo.value.reason == "rate_limited"
    assert "3600" in excinfo.value.detail
    connector.close()


# --- GitHub (coding) ----------------------------------------------------------------


def test_github_parses_verified_fields_and_reports_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200,
                json=_github_payload(),
                headers={"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"},
            ),
        ),
        traffic,
    )
    response = connector.search("public apis", limit=25)
    assert response.connector == "github"
    assert response.domain == "coding"
    assert response.requested == 25
    assert response.page_size == 25
    item = response.items[0]
    assert item.title == "public-apis/public-apis"
    assert item.url == "https://github.com/public-apis/public-apis"
    assert item.date == "2026-10-25T14:15:36Z"
    assert item.snippet == "A collective list of free APIs"
    assert ("stars", "492035") in item.facts
    assert ("language", "Python") in item.facts
    assert ("license", "MIT") in item.facts
    assert response.total == 11935
    assert response.rate == RateLimit(10, 9, "headers")
    connector.close()


def test_github_page_size_is_clamped_and_the_clamp_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    seen: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=_github_payload())

    connector, _ = _connector(monkeypatch, GitHubConnector, _api_handler(traffic, respond), traffic)
    response = connector.search("rag", limit=500)
    assert seen["per_page"] == "100"  # documented API max, requested verbatim
    assert response.requested == 500
    assert response.page_size == 100
    connector.close()


# --- Stack Overflow (coding) ----------------------------------------------------------


def test_stackoverflow_envelope_quota_and_missing_total(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    envelope = {
        "items": [
            {
                "title": "How do I execute a program or call a system command?",
                "link": "https://stackoverflow.com/questions/89228/how-do-i-execute",
                "score": 6297,
                "answer_count": 66,
                "is_answered": True,
                "creation_date": 1221701730,
                "tags": ["python", "command-line", "subprocess", "unix", "popen", "shell"],
            }
        ],
        "has_more": True,
        "quota_max": 300,
        "quota_remaining": 297,
    }
    connector, _ = _connector(
        monkeypatch,
        StackOverflowConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=envelope)),
        traffic,
    )
    response = connector.search("python typing")
    item = response.items[0]
    assert item.title.startswith("How do I execute")
    assert item.url == "https://stackoverflow.com/questions/89228/how-do-i-execute"
    assert item.snippet is None  # default filter returns no excerpt — invent none
    expected_date = datetime.fromtimestamp(1221701730, tz=UTC).isoformat(timespec="seconds")
    assert item.date == expected_date
    assert ("score", "6297") in item.facts
    assert ("answers", "66") in item.facts
    assert ("answered", "yes") in item.facts
    assert ("tags", "python, command-line, subprocess, unix, popen") in item.facts
    assert response.total is None  # the API reports has_more, never a total
    assert response.rate == RateLimit(300, 297, "body")
    connector.close()


# --- OpenAlex (knowledge) --------------------------------------------------------------


def test_openalex_rebuilds_inverted_abstracts_and_doi_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    envelope = {
        "meta": {"count": 7257814, "db_response_time_ms": 42},
        "results": [
            {
                "title": "Climate Change 1995: The Science of Climate Change",
                "doi": "https://doi.org/10.1007/978-3-030-12345-6_1",
                "publication_date": "1996-06-01",
                "cited_by_count": 3503,
                "type": "book",
                "primary_location": {"landing_page_url": "https://example.org/book"},
                "abstract_inverted_index": {"science": [2], "Climate": [0], "change": [1]},
            },
            {  # absent-everything record: None everywhere, placeholder title, no invention
                "title": None,
                "doi": None,
                "publication_date": None,
                "cited_by_count": None,
                "type": None,
                "primary_location": {},
                "abstract_inverted_index": None,
            },
        ],
    }
    connector, _ = _connector(
        monkeypatch,
        OpenAlexConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=envelope)),
        traffic,
    )
    response = connector.search("climate change")
    assert response.total == 7257814
    first, second = response.items
    assert first.snippet == "Climate change science"  # positions respected, not dict order
    assert first.url == "https://doi.org/10.1007/978-3-030-12345-6_1"
    assert first.date == "1996-06-01"
    assert ("cited by", "3503") in first.facts
    assert second.title == "(no title reported)"
    assert second.url is None
    assert second.snippet is None
    assert ("cited by", "n/a") in second.facts  # absent is n/a, never a fake 0
    connector.close()


# --- Crossref (knowledge) ----------------------------------------------------------------


def test_crossref_unwraps_titles_strips_jats_and_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    envelope = {
        "status": "ok",
        "message": {
            "total-results": 3067087,
            "items": [
                {
                    "DOI": "10.1093/oso/9780198828044.003.0003",
                    "title": ["Machine learning with sklearn"],
                    "URL": "https://doi.org/10.1093/oso/9780198828044.003.0003",
                    "abstract": "<p>This chapter's goal is to show how to apply "
                    "machine learning algorithms in a project.</p>",
                    "container-title": ["Fundamentals of Machine Learning"],
                    "is-referenced-by-count": 19,
                    "published": {"date-parts": [[2019, 11, 28]]},
                    "type": "book-chapter",
                }
            ],
        },
    }
    connector, _ = _connector(
        monkeypatch,
        CrossrefConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=envelope)),
        traffic,
    )
    response = connector.search("machine learning")
    item = response.items[0]
    assert item.title == "Machine learning with sklearn"  # list unwrapped
    assert item.date == "2019-11-28"
    assert item.snippet is not None and "<p>" not in item.snippet
    assert "machine learning algorithms" in item.snippet
    assert ("journal", "Fundamentals of Machine Learning") in item.facts
    assert ("cited", "19") in item.facts
    assert ("doi", "10.1093/oso/9780198828044.003.0003") in item.facts
    assert response.total == 3067087
    connector.close()


# --- Europe PMC (medical) ------------------------------------------------------------------


def test_europepmc_links_doi_pubmed_or_none_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    envelope = {
        "hitCount": 24606,
        "resultList": {
            "result": [
                {
                    "id": "29876543",
                    "source": "MED",
                    "pmid": "29876543",
                    "doi": "10.1002/bcp.12345",
                    "title": "Serum Concentration Relationships in Migraine Prevention",
                    "authorString": "Riise H, Simpson M, Espnes K",
                    "journalInfo": {
                        "journal": {"title": "Basic & clinical pharmacology"}
                    },
                    "abstractText": "Migraine prevention remains a challenge.",
                    "citedByCount": 0,
                    "firstPublicationDate": "2018-05-15",
                },
                {
                    "id": "PMC13657683",
                    "source": "PMC",
                    "title": "A record with no ids at all",
                    "citedByCount": 0,
                },
            ]
        },
    }
    connector, _ = _connector(
        monkeypatch,
        EuropePmCConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=envelope)),
        traffic,
    )
    response = connector.search("migraine prevention")
    first, second = response.items
    assert first.url == "https://doi.org/10.1002/bcp.12345"
    assert first.snippet == "Migraine prevention remains a challenge."
    assert first.date == "2018-05-15"
    assert ("journal", "Basic & clinical pharmacology") in first.facts
    assert ("cited", "0") in first.facts  # measured zero stays 0, not n/a
    assert second.url is None  # no verified deep link → none printed
    assert second.snippet is None
    assert ("id", "PMC/PMC13657683") in second.facts
    assert response.total == 24606
    connector.close()


def test_europepmc_query_error_envelope_is_invalid_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query errors arrive as HTTP 200 with an errCode envelope — surface them as-is."""
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        EuropePmCConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200, json={"errCode": 400, "errMsg": "Query too complex for the service"}
            ),
        ),
        traffic,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("migraine AND prevention AND (cluster headache OR tension)")
    assert excinfo.value.reason == "invalid_query"
    assert "too complex" in excinfo.value.detail
    connector.close()


# --- USGS (earth) -------------------------------------------------------------------------


def test_usgs_parses_events_and_keeps_total_none(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    envelope = {
        "type": "FeatureCollection",
        "metadata": {
            "generated": 1792937983000,
            "status": 200,
            "api": "2.8.2",
            "limit": 1,
            "offset": 0,
        },
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "title": "M 5.9 - 218 km ENE of Nagqu, China",
                    "mag": 5.9,
                    "place": "218 km ENE of Nagqu, China",
                    "time": 1792927557101,
                    "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us6000u4ty",
                    "tsunami": 0,
                },
                "geometry": {"type": "Point", "coordinates": [94.0534, 32.4625, 7]},
            }
        ],
    }
    connector, _ = _connector(
        monkeypatch,
        UsgsConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=envelope)),
        traffic,
    )
    response = connector.search("minmagnitude=5")
    item = response.items[0]
    assert item.title == "M 5.9 - 218 km ENE of Nagqu, China"
    assert item.url is not None and "us6000u4ty" in item.url
    assert item.date is not None and item.date.startswith("2026-10-25")
    assert item.snippet == "218 km ENE of Nagqu, China"
    assert ("magnitude", "5.9") in item.facts
    assert ("depth km", "7.0") in item.facts  # float rendering of the reported depth
    assert ("lat,lon", "32.4625,94.0534") in item.facts
    assert ("tsunami", "no") in item.facts
    assert response.total is None  # verified: the feed has no total-count field
    connector.close()


def test_usgs_builds_the_documented_filter_set(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    seen: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"features": []})

    connector, _ = _connector(monkeypatch, UsgsConnector, _api_handler(traffic, respond), traffic)
    connector.search("minmagnitude=5 starttime=2026-10-01 mindepth=10")
    assert seen["format"] == "geojson"
    assert seen["orderby"] == "time"
    assert seen["minmagnitude"] == "5"
    assert seen["starttime"] == "2026-10-01"
    assert seen["mindepth"] == "10"
    connector.close()


@pytest.mark.parametrize(
    "query",
    ["nonsense", "minmagnitude=five", "bogus=1", "minmagnitude"],
)
def test_usgs_rejects_bad_filters_honestly(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    connector, _ = _connector(
        monkeypatch,
        UsgsConnector,
        lambda request: pytest.fail("invalid filters must not reach the network"),
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search(query)
    assert excinfo.value.reason == "invalid_query"
    connector.close()


def test_usgs_empty_query_means_latest_events(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        UsgsConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json={"features": []})),
        traffic,
    )
    response = connector.search("")
    assert response.items == ()
    assert response.total is None  # still not a fabricated zero — the feed never reports one
    connector.close()


# --- Weather (MET Norway + Open-Meteo geocoding) -------------------------------------------


def _forecast_payload() -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [10.75, 59.91, 3]},
        "properties": {
            "meta": {
                "updated_at": "2026-10-25T13:17:22Z",
                "units": {
                    "air_temperature": "celsius",
                    "wind_speed": "m/s",
                    "rel_humidity": "%",
                    "precipitation_amount": "mm",
                },
            },
            "timeseries": [
                {
                    "time": "2026-10-25T14:00:00Z",
                    "data": {
                        "instant": {
                            "details": {
                                "air_temperature": 19.2,
                                "wind_speed": 3.1,
                                "rel_humidity": None,  # seen live — must render n/a
                            }
                        },
                        "next_1_hours": {
                            "summary": {"symbol_code": "cloudy"},
                            "details": {"precipitation_amount": 0.0},
                        },
                    },
                }
            ],
        },
    }


def _weather_handler(traffic: _Traffic, geo_results: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        traffic.api_calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json={"results": geo_results or []})
        if request.url.host == "api.met.no":
            return httpx.Response(200, json=_forecast_payload())
        return httpx.Response(404)  # pragma: no cover - unknown host in tests

    return handler


def test_weather_direct_coordinates_skips_geocoding(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(monkeypatch, WeatherConnector, _weather_handler(traffic), traffic)
    response = connector.search("59.91, 10.75")
    assert all("geocoding" not in call for call in traffic.api_calls)
    item = response.items[0]
    assert item.title == "Weather — 59.91, 10.75"
    assert item.date == "2026-10-25T14:00:00Z"
    assert item.snippet == "cloudy"
    assert ("temperature", "19.2 (celsius)") in item.facts
    assert ("wind", "3.1 (m/s)") in item.facts
    assert ("humidity", "n/a") in item.facts  # null reported honestly, never 0
    assert ("precipitation next hour", "0.0 (mm)") in item.facts
    assert ("model updated", "2026-10-25T13:17:22Z") in item.facts
    assert response.total == 1
    assert response.page_size == 1
    connector.close()


def test_weather_geocodes_a_place_name_first(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    geo = [{"name": "Tokyo", "latitude": 35.6895, "longitude": 139.69171, "country": "Japan"}]
    connector, _ = _connector(
        monkeypatch, WeatherConnector, _weather_handler(traffic, geo), traffic
    )
    response = connector.search("Tokyo")
    assert traffic.api_calls[0].startswith("geocoding-api.open-meteo.com")
    assert traffic.api_calls[1].startswith("api.met.no")
    assert response.items[0].title == "Weather — Tokyo, Japan"
    assert response.total == 1
    connector.close()


def test_weather_unknown_place_is_a_measured_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    connector, _ = _connector(monkeypatch, WeatherConnector, _weather_handler(traffic), traffic)
    response = connector.search("Nowheresville")
    assert response.items == ()
    assert response.total == 0  # measured zero, distinct from a failure
    assert all("met.no" not in call for call in traffic.api_calls)  # no forecast without a place
    connector.close()


def test_weather_rejects_out_of_range_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, _ = _connector(
        monkeypatch,
        WeatherConnector,
        lambda request: pytest.fail("bad coordinates must not reach the network"),
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search("120, 10")
    assert excinfo.value.reason == "invalid_query"
    connector.close()


# --- Frankfurter (finance) ----------------------------------------------------------------------


def test_frankfurter_parses_rates_and_slices_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    seen: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(
            200,
            json={
                "amount": 1.0,
                "base": "USD",
                "date": "2026-10-24",
                "rates": {"EUR": 0.85, "GBP": 0.75, "JPY": 150.12},
            },
        )

    connector, _ = _connector(
        monkeypatch, FrankfurterConnector, _api_handler(traffic, respond), traffic
    )
    response = connector.search("usd", limit=2)
    assert seen["base"] == "USD"
    assert "symbols" not in seen  # no symbols requested → all rates
    assert len(response.items) == 2  # sliced to the requested limit…
    assert response.total == 3  # …while the provider's count stays whole
    assert response.items[0].title == "USD → EUR"
    assert response.items[0].snippet == "0.85 EUR per 1 USD"
    assert response.items[0].date == "2026-10-24"
    connector.close()


def test_frankfurter_accepts_explicit_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    traffic = _Traffic()
    seen: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(
            200, json={"amount": 1.0, "base": "USD", "date": "2026-10-24", "rates": {"EUR": 0.85}}
        )

    connector, _ = _connector(
        monkeypatch, FrankfurterConnector, _api_handler(traffic, respond), traffic
    )
    response = connector.search("usd eur")
    assert seen["base"] == "USD"
    assert seen["symbols"] == "EUR"
    assert response.total == 1
    connector.close()


@pytest.mark.parametrize("query", ["dollars", "USDD", "USD EUROP"])
def test_frankfurter_rejects_non_currency_queries(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    connector, _ = _connector(
        monkeypatch,
        FrankfurterConnector,
        lambda request: pytest.fail("invalid currency input must not reach the network"),
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.search(query)
    assert excinfo.value.reason == "invalid_query"
    connector.close()


# --- cross-connector honesty guarantees ------------------------------------------------------


def test_response_holds_measured_timestamp_and_requested_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        GitHubConnector,
        _api_handler(traffic, lambda request: httpx.Response(200, json=_github_payload())),
        traffic,
    )
    before = datetime.now(UTC)
    response = connector.search("rag")
    after = datetime.now(UTC)
    fetched = datetime.fromisoformat(response.fetched_at)
    assert fetched.tzinfo is not None  # timezone-qualified, not a bare local string
    # fetched_at carries second precision; compare at that same precision.
    assert before.replace(microsecond=0) <= fetched <= after
    assert response.query == "rag"
    connector.close()


def test_result_items_never_carry_partial_none_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record without a title gets a stated placeholder — never an empty string."""
    traffic = _Traffic()
    connector, _ = _connector(
        monkeypatch,
        OpenAlexConnector,
        _api_handler(
            traffic,
            lambda request: httpx.Response(
                200, json={"meta": {"count": 1}, "results": [{"title": "   "}]}
            ),
        ),
        traffic,
    )
    response = connector.search("x")
    item: ResultItem = response.items[0]
    assert item.title == "(no title reported)"
    connector.close()


def test_search_response_is_json_serializable() -> None:
    """The CLI --json path renders every field without custom encoders."""
    import json
    from dataclasses import asdict

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
                snippet="Example",
                facts=(("stars", "42"),),
            ),
        ),
        total=1,
        fetched_at="2026-10-25T12:00:00+00:00",
        rate=RateLimit(limit=10, remaining=9, source="headers"),
    )
    parsed = json.loads(json.dumps(asdict(response), default=str))
    assert parsed["items"][0]["facts"][0] == ["stars", "42"]
    assert parsed["rate"]["remaining"] == 9
    assert parsed["total"] == 1
