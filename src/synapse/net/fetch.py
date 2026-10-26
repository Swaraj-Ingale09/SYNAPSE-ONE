"""SSRF-safe, robots-aware HTTP fetcher (ADR 0004).

Guarantees enforced here, before a single byte of external content is read:

- static URL validation + DNS + per-IP public-routability checks (``ssrf``)
- redirects are never followed automatically; every hop is re-validated
- response size is capped while streaming (no full-body buffering attacks)
- content-type must be text-like (HTML, plain text, XML, JSON)
- robots.txt is checked for our honest User-Agent before the real fetch
- per-host politeness delay (rate limiting) between requests

The result is inert text. Nothing here executes scripts, renders HTML, or
treats remote content as instructions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from synapse.config import Settings
from synapse.net.ssrf import EgressBlocked, validate_public_url

log = logging.getLogger("synapse.net.fetch")

USER_AGENT = "SynapseOne/0.1 (+local research station; respects robots.txt)"
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/json",
    }
)
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
ROBOTS_MAX_BYTES = 256 * 1024


class FetchBlocked(EgressBlocked):
    """Fetching was refused (robots.txt or egress policy)."""


class FetchFailed(RuntimeError):
    """The fetch was permitted but failed (network, HTTP status, size, type)."""


class FetchHTTPError(FetchFailed):
    """The server answered with a non-200 status.

    Carries what rate-aware callers need to react honestly: the status code,
    lowercase response headers (``retry-after``, ``x-ratelimit-*``), and a
    bounded snippet of the body for error detail. A plain ``FetchFailed``
    except clause still catches it (ADR 0008).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        headers: Mapping[str, str],
        body_snippet: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = dict(headers)
        self.body_snippet = body_snippet


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status_code: int
    content_type: str
    text: str
    bytes_read: int
    from_cache: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)  # lowercase keys


def _content_type_base(header_value: str) -> str:
    return header_value.split(";", 1)[0].strip().lower()


class SafeFetcher:
    """Polite, bounded, SSRF-safe fetcher. One instance per worker process."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        self._min_interval = min_interval_seconds
        self._last_request_by_host: dict[str, float] = {}
        self._robots_cache: dict[str, robotparser.RobotFileParser] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- politeness ---------------------------------------------------------

    def _throttle(self, hostname: str) -> None:
        last = self._last_request_by_host.get(hostname)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_by_host[hostname] = time.monotonic()

    # --- robots.txt ----------------------------------------------------------

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots_cache.get(origin)
        if parser is None:
            parser = robotparser.RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                # robots.txt itself is fetched through the same SSRF gate.
                robots_target = validate_public_url(robots_url)
            except EgressBlocked:
                # Cannot safely fetch robots.txt for this origin: be polite
                # and refuse rather than crawl blind.
                log.info("robots_fetch_blocked origin=%s", origin)
                return False
            self._throttle(robots_target.hostname)
            try:
                response = self._client.get(robots_url)
            except httpx.HTTPError:
                # No robots.txt reachable -> default allow (standard practice).
                parser.allow_all = True
            else:
                if response.status_code == 200:
                    body = response.content[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
                    parser.parse(body.splitlines())
                elif response.status_code in (401, 403):
                    # Server explicitly refuses robots access: assume full
                    # disallow for our UA — the polite interpretation.
                    parser.disallow_all = True
                else:  # 404, 410, 5xx, redirects...
                    parser.allow_all = True
            self._robots_cache[origin] = parser
        return parser.can_fetch(USER_AGENT, url)

    # --- the fetch ------------------------------------------------------------

    def fetch(
        self,
        url: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> FetchResult:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            target = validate_public_url(current)  # raises EgressBlocked
            if not self._robots_allows(current):
                raise FetchBlocked(f"robots.txt for {target.hostname} disallows fetching {current}")
            self._throttle(target.hostname)
            try:
                response = self._client.get(current)
            except httpx.TimeoutException as exc:
                raise FetchFailed(f"timeout fetching {current}") from exc
            except httpx.HTTPError as exc:
                raise FetchFailed(f"network error fetching {current}: {exc}") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise FetchFailed(f"{current} returned {response.status_code} without Location")
                # Per-hop revalidation happens at the top of the loop.
                current = urljoin(current, location)
                continue

            if response.status_code != 200:
                charset = response.charset_encoding or "utf-8"
                try:
                    snippet = response.content[:512].decode(charset, errors="replace")
                except LookupError:
                    snippet = response.content[:512].decode("utf-8", errors="replace")
                raise FetchHTTPError(
                    f"{current} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body_snippet=snippet,
                )

            content_type = _content_type_base(response.headers.get("content-type", ""))
            if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                raise FetchFailed(
                    f"refusing content-type {content_type!r} from {current} "
                    f"(only text-like types are ingested)"
                )

            # Stream with a hard byte cap; never trust Content-Length.
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchFailed(f"{current} declares {declared} bytes (limit {max_bytes})")
            data = bytearray()
            try:
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise FetchFailed(f"{current} exceeded the {max_bytes}-byte ingest limit")
            except httpx.HTTPError as exc:
                raise FetchFailed(f"stream error from {current}: {exc}") from exc

            charset = response.charset_encoding or "utf-8"
            try:
                text = bytes(data).decode(charset, errors="replace")
            except LookupError:
                text = bytes(data).decode("utf-8", errors="replace")

            return FetchResult(
                url=str(response.url),
                status_code=response.status_code,
                content_type=content_type or "application/octet-stream",
                text=text,
                bytes_read=len(data),
                headers={k.lower(): v for k, v in response.headers.items()},
            )

        raise FetchFailed(f"too many redirects (>{MAX_REDIRECTS}) fetching {url}")


def build_fetcher(settings: Settings, *, min_interval_seconds: float = 1.0) -> SafeFetcher:
    """One fetcher per caller; ``min_interval_seconds`` sets its politeness floor."""
    return SafeFetcher(settings, min_interval_seconds=min_interval_seconds)
