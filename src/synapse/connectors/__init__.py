"""Free public-API connectors, one per verified provider (ADR 0008).

Every entry in :data:`REGISTRY` was checked before hardcoding: endpoint live,
response shape inspected, rate limits read from the server (never guessed),
and the API host's robots.txt permitting our User-Agent. ``build_connector``
hands each connector its own SSRF-gated fetcher with the provider's pacing
floor, so no connector can reach the network any other way.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from synapse.config import Settings
from synapse.connectors.base import (
    Connector,
    ConnectorError,
    RateLimit,
    ResultItem,
    SearchResponse,
)
from synapse.connectors.code import GitHubConnector, StackOverflowConnector
from synapse.connectors.knowledge import CrossrefConnector, EuropePmCConnector, OpenAlexConnector
from synapse.connectors.world import FrankfurterConnector, UsgsConnector, WeatherConnector
from synapse.net.fetch import SafeFetcher

REGISTRY: dict[str, type[Connector]] = {
    cls.name: cls
    for cls in (
        GitHubConnector,
        StackOverflowConnector,
        OpenAlexConnector,
        CrossrefConnector,
        EuropePmCConnector,
        UsgsConnector,
        WeatherConnector,
        FrankfurterConnector,
    )
}


def connector_names() -> list[str]:
    return sorted(REGISTRY)


def connector_catalog() -> list[dict[str, str]]:
    """Name/domain/description rows for listing — the same fields the CLI shows."""
    return [
        {
            "name": REGISTRY[name].name,
            "domain": REGISTRY[name].domain,
            "description": REGISTRY[name].description,
        }
        for name in connector_names()
    ]


def build_connector(
    name: str,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Connector:
    """Build a connector with its own politeness-paced, SSRF-gated fetcher.

    Raises ``ConnectorError("unknown_connector", ...)`` listing the real names —
    a typo must never be answered with an empty result set.
    """
    key = name.strip().lower()
    connector_type = REGISTRY.get(key)
    if connector_type is None:
        available = ", ".join(connector_names())
        raise ConnectorError(
            "unknown_connector",
            f"unknown connector {name!r} — available: {available}",
        )
    fetcher = SafeFetcher(
        settings,
        min_interval_seconds=connector_type.min_interval_seconds,
    )
    return connector_type(fetcher, sleep=sleep)


__all__ = [
    "REGISTRY",
    "Connector",
    "ConnectorError",
    "RateLimit",
    "ResultItem",
    "SearchResponse",
    "build_connector",
    "connector_catalog",
    "connector_names",
]
