"""Contracts for the synchronous search routes (ADR 0008).

Wire shapes for querying the free public-API connectors directly. They mirror
:class:`synapse.connectors.base.SearchResponse` faithfully — notably ``total``
stays ``None`` (JSON null) when the provider reported no count, distinct from
a measured ``0``, and ``rate`` is the server's own report, never inferred.
"""

from __future__ import annotations

from pydantic import BaseModel


class SearchItemOut(BaseModel):
    title: str
    url: str | None = None
    date: str | None = None
    snippet: str | None = None
    facts: list[tuple[str, str]] = []


class SearchRateOut(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    source: str


class SearchOut(BaseModel):
    connector: str
    domain: str
    query: str
    requested: int
    page_size: int
    items: list[SearchItemOut]
    # None means "provider reported no count", not "zero results".
    total: int | None = None
    fetched_at: str
    rate: SearchRateOut | None = None


class SearchRequest(BaseModel):
    connector: str
    query: str
    limit: int = 10


class ConnectorInfoOut(BaseModel):
    name: str
    domain: str
    description: str


class ConnectorCatalogOut(BaseModel):
    connectors: list[ConnectorInfoOut]
