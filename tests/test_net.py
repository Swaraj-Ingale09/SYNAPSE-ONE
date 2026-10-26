"""SSRF-safe fetcher: prove the attack paths are blocked (ADR 0004, spec 15.3).

Every test here models a real attack: cloud-metadata redirect, private-network
bounce, robots.txt disallow, oversized body, non-text content-type. The
fetcher must refuse all of them *before* returning content.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from synapse.net.fetch import FetchBlocked, FetchFailed, FetchHTTPError, SafeFetcher
from synapse.net.ssrf import (
    EgressBlocked,
    assert_safe_ip,
    validate_public_url,
)

# A publicly routable address (example.com's former IP) used as the fake
# DNS answer for ordinary hostnames so no test touches the real network.
PUBLIC_IP = "93.184.216.34"


def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make hostnames resolve to a public IP; validate IP literals for real.

    This models a hostile server that redirects a public hostname to a private
    or metadata address: the hostname resolves fine, but the redirect target is
    an IP literal that must be caught by the per-hop check.
    """

    def fake_resolve(hostname: str, port: int) -> list[str]:
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return [PUBLIC_IP]  # ordinary hostname -> public
        assert_safe_ip(hostname)  # IP literal -> real validation, may raise
        return [str(ip)]

    monkeypatch.setattr("synapse.net.ssrf.resolve_and_validate", fake_resolve)


# --- SSRF primitives ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad_ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local
    ],
)
def test_assert_safe_ip_blocks_non_public(bad_ip: str) -> None:
    with pytest.raises(EgressBlocked):
        assert_safe_ip(bad_ip)


def test_assert_safe_ip_allows_public() -> None:
    assert assert_safe_ip("93.184.216.34") is not None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com/x",  # non-http scheme
        "http://user:pass@example.com/",  # userinfo
        "http://127.0.0.1:8080/",  # private host + bad port
        "http://169.254.169.254/latest/meta-data/",  # metadata
        "http://example.com:22/",  # port outside allowlist
    ],
)
def test_validate_public_url_blocks_dangerous_urls(url: str) -> None:
    with pytest.raises(EgressBlocked):
        validate_public_url(url)


def test_validate_public_url_rejects_malformed() -> None:
    with pytest.raises(EgressBlocked):
        validate_public_url("http://")


def test_dns_rebinding_one_bad_answer_blocks_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname with one private answer among public ones must be refused.

    Patches the OS resolver, not our validation, so the real
    ``resolve_and_validate`` runs and must reject on the first bad answer.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        return [
            (2, 1, 6, "", (PUBLIC_IP, port or 80)),
            (2, 1, 6, "", ("10.0.0.99", port or 80)),  # round-robin rebinding
        ]

    monkeypatch.setattr("synapse.net.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EgressBlocked, match="10.0.0.99"):
        validate_public_url("http://rebind.example.com/")


def test_dns_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolution error is a hard block, never a silent pass."""

    import socket as _socket

    def fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        raise _socket.gaierror("no such host")

    monkeypatch.setattr("synapse.net.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EgressBlocked, match="cannot resolve"):
        validate_public_url("http://unresolvable.example.com/")


# --- fetcher: redirect revalidation ------------------------------------------


def _build(monkeypatch: pytest.MonkeyPatch, handler, **kwargs) -> SafeFetcher:
    """Build a SafeFetcher backed by a MockTransport (no real network)."""
    _fake_dns(monkeypatch)
    # min_interval defaults to 0 in tests so politeness never sleeps.
    kwargs.setdefault("min_interval_seconds", 0.0)
    return SafeFetcher(
        settings=None,  # type: ignore[arg-type] - settings unused by the fetch path
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_redirect_to_metadata_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public site bouncing us to 169.254.169.254 must be refused per-hop."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, text="metadata")

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(EgressBlocked):
        fetcher.fetch("http://evil.example.com/")
    fetcher.close()


def test_redirect_to_private_network_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://192.168.0.10/"})

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(EgressBlocked):
        fetcher.fetch("http://evil.example.com/")
    fetcher.close()


def test_redirect_loop_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)  # no robots -> default allow
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"http://loop.example.com/{n + 1}"})

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchFailed, match="too many redirects"):
        fetcher.fetch("http://loop.example.com/0")
    fetcher.close()


# --- fetcher: robots.txt -----------------------------------------------------


def test_robots_disallow_blocks_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="secret")

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchBlocked, match="robots"):
        fetcher.fetch("http://site.example.com/private/data")
    fetcher.close()


def test_robots_allow_permits_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="public content")

    fetcher = _build(monkeypatch, handler)
    result = fetcher.fetch("http://site.example.com/page")
    assert result.text == "public content"
    fetcher.close()


def test_missing_robots_defaults_to_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="content")

    fetcher = _build(monkeypatch, handler)
    result = fetcher.fetch("http://no-robots.example.com/page")
    assert result.status_code == 200
    fetcher.close()


def test_robots_403_assumes_disallow(monkeypatch: pytest.MonkeyPatch) -> None:
    """401/403 on robots.txt is the polite full-disallow, not an invitation."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403)
        return httpx.Response(200, text="should not be reached")

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchBlocked):
        fetcher.fetch("http://guarded.example.com/page")
    fetcher.close()


# --- fetcher: content-type and size caps -------------------------------------


def test_non_text_content_type_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchFailed, match="content-type"):
        fetcher.fetch("http://file.example.com/doc.pdf")
    fetcher.close()


def test_oversized_body_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than the cap is refused mid-stream, not fully buffered."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"A" * 5000, headers={"content-type": "text/plain"})

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchFailed, match="limit|exceeded"):
        fetcher.fetch("http://big.example.com/huge", max_bytes=1000)
    fetcher.close()


def test_declared_content_length_over_cap_refused_early(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=b"A" * 10,
            headers={"content-type": "text/plain", "content-length": "99999999"},
        )

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchFailed, match="declares"):
        fetcher.fetch("http://liar.example.com/page", max_bytes=1000)
    fetcher.close()


def test_http_error_status_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500, text="boom")

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchFailed, match="HTTP 500"):
        fetcher.fetch("http://down.example.com/page")
    fetcher.close()


# --- fetcher: HTTP errors expose what rate-aware callers need (ADR 0008) -------------


def test_http_error_carries_status_headers_and_body_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connectors must see 429 + Retry-After, not a flattened string message."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            429,
            headers={"retry-after": "3", "x-ratelimit-remaining": "0"},
            text="slow down a moment",
        )

    fetcher = _build(monkeypatch, handler)
    with pytest.raises(FetchHTTPError) as excinfo:
        fetcher.fetch("http://api.example.com/data")
    error = excinfo.value
    assert isinstance(error, FetchFailed)  # existing except-clauses keep working
    assert error.status_code == 429
    assert error.headers["retry-after"] == "3"
    assert error.headers["x-ratelimit-remaining"] == "0"
    assert "slow down a moment" in error.body_snippet
    fetcher.close()


def test_success_result_exposes_response_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="payload",
            headers={"content-type": "text/plain", "x-ratelimit-limit": "10"},
        )

    fetcher = _build(monkeypatch, handler)
    result = fetcher.fetch("http://api.example.com/data")
    assert result.headers["x-ratelimit-limit"] == "10"  # keys are lowercase
    assert result.headers["content-type"] == "text/plain"
    fetcher.close()
