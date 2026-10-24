"""Network egress controls: SSRF-safe URL fetching (ADR 0004)."""

from synapse.net.fetch import FetchResult, SafeFetcher, build_fetcher
from synapse.net.ssrf import EgressBlocked, validate_public_url

__all__ = [
    "EgressBlocked",
    "FetchResult",
    "SafeFetcher",
    "build_fetcher",
    "validate_public_url",
]
