"""Connector contract: typed results, honest failures, server-honoring rate limits.

Every connector in this package is a *free public API* verified before use
(host reachable, response shape inspected live, robots.txt permitting our
User-Agent — ADR 0008). All traffic goes through :class:`SafeFetcher`, so the
SSRF gate, robots checks, content-type allowlist, and byte caps apply unchanged.

Honesty rules enforced here:

- failures are typed (``ConnectorError`` with a stable ``reason``) and always
  say what actually happened — a rate limit is never rendered as "no results";
- ``total`` stays ``None`` when the provider does not report a count, distinct
  from a measured ``0``;
- rate-limit waits honor only ``Retry-After`` (RFC 7231); ``x-ratelimit-reset``
  is displayed nowhere and interpreted never, because its semantics differ per
  provider (GitHub: epoch seconds, OpenAlex: relative seconds — both verified);
- retries happen on HTTP 429 only, bounded in count and in wait time.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar

from synapse.net.fetch import FetchFailed, FetchHTTPError, SafeFetcher
from synapse.net.ssrf import EgressBlocked

CONNECTOR_MAX_BYTES = 1024 * 1024  # search payloads are small; cap hard
SNIPPET_MAX_CHARS = 600
MAX_429_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 60.0
DEFAULT_RETRY_WAIT_SECONDS = 1.0


class ConnectorError(RuntimeError):
    """An honest, machine-classifiable connector failure.

    ``reason`` is stable (``rate_limited``, ``blocked``, ``http_error``,
    ``fetch_failed``, ``invalid_response``, ``invalid_query``,
    ``unknown_connector``); ``detail`` is the human truth of this occurrence.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RateLimit:
    """Server-reported quota. ``source`` says who reported it — we never infer."""

    limit: int | None
    remaining: int | None
    source: str  # "headers" | "body"


@dataclass(frozen=True)
class ResultItem:
    """One search hit in connector-neutral shape.

    ``date`` is the provider's own date for the record (publication, update,
    event time — each connector documents which in its description). Fields the
    provider does not report are ``None``, never invented.
    """

    title: str
    url: str | None
    date: str | None
    snippet: str | None
    facts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SearchResponse:
    connector: str
    domain: str
    query: str
    requested: int  # what the caller asked for (--limit)
    page_size: int  # what was actually requested from the API (clamped)
    items: tuple[ResultItem, ...]
    total: int | None  # provider-reported total; None = not reported
    fetched_at: str  # measured locally, UTC ISO-8601
    rate: RateLimit | None


# --- small typed extractors (defensive against hostile/unexpected JSON) --------


def text_of(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def int_of(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def float_of(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def na(value: object) -> str:
    """Render a fact value honestly: a real 0 stays "0", an absence is "n/a"."""
    return "n/a" if value is None else str(value)


def clip_snippet(
    text: str | None,
    *,
    max_chars: int = SNIPPET_MAX_CHARS,
) -> tuple[str | None, tuple[str, str] | None]:
    """Trim a long snippet *and disclose the trim* (returns a note to add as a fact)."""
    if not text:
        return None, None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None, None
    if len(cleaned) <= max_chars:
        return cleaned, None
    trimmed = cleaned[:max_chars].rstrip()
    return trimmed + "…", ("snippet", f"{max_chars} of {len(cleaned)} chars")


def rate_from_headers(headers: Mapping[str, str]) -> RateLimit | None:
    limit = _header_int(headers, "x-ratelimit-limit")
    remaining = _header_int(headers, "x-ratelimit-remaining")
    if limit is None and remaining is None:
        return None
    return RateLimit(limit=limit, remaining=remaining, source="headers")


def _header_int(headers: Mapping[str, str], key: str) -> int | None:
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


class Connector:
    """Base class: one request path with 429-only retries and bounded bytes.

    Subclasses set the class attributes, implement :meth:`build_url` and
    :meth:`parse`, and inherit everything else. Connectors that need their own
    orchestration (multi-host flows) override :meth:`_run` instead.
    """

    name: ClassVar[str] = ""
    domain: ClassVar[str] = ""
    description: ClassVar[str] = ""
    min_interval_seconds: ClassVar[float] = 1.0  # local politeness floor
    max_results: ClassVar[int] = 100  # never request a larger page than this
    allow_empty_query: ClassVar[bool] = False

    def __init__(
        self,
        fetcher: SafeFetcher,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._fetcher = fetcher
        self._sleep = sleep
        self._backoff_until = 0.0

    # --- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._fetcher.close()

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- the public entry -------------------------------------------------------

    def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        cleaned = query.strip()
        if not cleaned and not self.allow_empty_query:
            raise ConnectorError("invalid_query", f"{self.name} needs a non-empty query")
        if limit < 1:
            raise ConnectorError("invalid_query", "limit must be at least 1")
        page_size = min(limit, self.max_results)
        return self._run(cleaned, limit, page_size)

    def _run(self, query: str, limit: int, page_size: int) -> SearchResponse:
        url = self.build_url(query, page_size)
        text, headers = self._get(url)
        envelope = self._json(text)
        if isinstance(envelope, dict):
            self._note_server_backoff(envelope)
        items, total, body_rate = self.parse(query, page_size, envelope)
        header_rate = rate_from_headers(headers)
        return self._assemble(
            query=query,
            limit=limit,
            page_size=page_size,
            items=items,
            total=total,
            rate=header_rate if header_rate is not None else body_rate,
        )

    def build_url(self, query: str, page_size: int) -> str:
        raise NotImplementedError

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        raise NotImplementedError

    def _assemble(
        self,
        *,
        query: str,
        limit: int,
        page_size: int,
        items: list[ResultItem],
        total: int | None,
        rate: RateLimit | None,
    ) -> SearchResponse:
        return SearchResponse(
            connector=self.name,
            domain=self.domain,
            query=query,
            requested=limit,
            page_size=page_size,
            items=tuple(items[:page_size]),
            total=total,
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            rate=rate,
        )

    # --- the request path ---------------------------------------------------------

    def _get(self, url: str) -> tuple[str, Mapping[str, str]]:
        """Fetch + return (body, lowercase headers). Retries 429 only, bounded."""
        self._wait_out_backoff()
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self._fetcher.fetch(url, max_bytes=CONNECTOR_MAX_BYTES)
            except FetchHTTPError as exc:
                if exc.status_code == 429 and attempt <= MAX_429_RETRIES:
                    wait = self._retry_wait(exc.headers)
                    if wait <= MAX_RETRY_WAIT_SECONDS:
                        self._sleep(wait)
                        continue
                raise self._http_error(exc) from None
            except EgressBlocked as exc:  # includes robots disallow (FetchBlocked)
                raise ConnectorError("blocked", str(exc)) from None
            except FetchFailed as exc:
                raise ConnectorError("fetch_failed", str(exc)) from None
            return result.text, result.headers

    def _http_error(self, exc: FetchHTTPError) -> ConnectorError:
        rate_headers = rate_from_headers(exc.headers)
        out_of_quota = rate_headers is not None and rate_headers.remaining == 0
        if exc.status_code == 429 or out_of_quota:
            retry_after = exc.headers.get("retry-after")
            wait_note = f"; server says wait {retry_after}s" if retry_after else ""
            return ConnectorError(
                "rate_limited",
                f"{self.name} is rate limited (HTTP {exc.status_code}){wait_note}",
            )
        detail = f"{self.name} returned HTTP {exc.status_code}"
        snippet = " ".join(exc.body_snippet.split())
        if snippet:
            detail = f"{detail}: {snippet[:200]}"
        return ConnectorError("http_error", detail)

    @staticmethod
    def _retry_wait(headers: Mapping[str, str]) -> float:
        """Honor Retry-After only (delay-seconds or HTTP-date); default 1s if absent."""
        raw = (headers.get("retry-after") or "").strip()
        if not raw:
            return DEFAULT_RETRY_WAIT_SECONDS
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_WAIT_SECONDS
        if when is None:
            return DEFAULT_RETRY_WAIT_SECONDS
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())

    # --- server-directed pacing (StackExchange-style backoff envelopes) ----------

    def _note_server_backoff(self, envelope: Mapping[str, object]) -> None:
        value = envelope.get("backoff")
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return
        if value > MAX_BACKOFF_SECONDS:
            raise ConnectorError(
                "rate_limited",
                f"{self.name} asked to back off {value}s before the next request",
            )
        self._backoff_until = time.monotonic() + float(value)

    def _wait_out_backoff(self) -> None:
        remaining = self._backoff_until - time.monotonic()
        if remaining > 0:
            self._backoff_until = 0.0
            self._sleep(remaining)

    # --- shared parsing helpers ----------------------------------------------------

    def _json(self, text: str) -> object:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectorError(
                "invalid_response",
                f"{self.name} returned a non-JSON body ({exc.msg} at position {exc.pos})",
            ) from None

    def _obj(self, value: object, what: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ConnectorError(
                "invalid_response", f"{self.name} expected {what} to be a JSON object"
            )
        return value

    def _arr(self, value: object, what: str) -> list[object]:
        if not isinstance(value, list):
            raise ConnectorError(
                "invalid_response", f"{self.name} expected {what} to be a JSON array"
            )
        return value
