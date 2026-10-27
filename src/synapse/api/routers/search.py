"""Synchronous search routes over the free public-API connectors (ADR 0008).

Honesty rules (project-wide, non-negotiable):

- ``total`` is JSON null when the provider reported no count — distinct from
  a measured ``0``; nothing here coerces one into the other;
- failures carry the connector's stable ``reason`` plus the real server
  ``message`` in the error detail — a rate limit is never rendered as an
  empty result set;
- nothing is invented: items, dates, and rate limits are exactly what the
  provider returned.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, HTTPException, Request

from synapse.api.search_contracts import (
    ConnectorCatalogOut,
    ConnectorInfoOut,
    SearchOut,
    SearchRequest,
)
from synapse.config import Settings
from synapse.connectors import ConnectorError, build_connector, connector_catalog

router = APIRouter()

# Stable ConnectorError reason -> HTTP status. Unknown (future) reasons fall
# back to 500 rather than being mislabelled.
STATUS_FOR_REASON: dict[str, int] = {
    "invalid_query": 400,
    "unknown_connector": 404,
    "blocked": 403,
    "rate_limited": 429,
    "http_error": 502,
    "fetch_failed": 502,
    "invalid_response": 502,
}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/connectors", response_model=ConnectorCatalogOut)
def list_connectors() -> ConnectorCatalogOut:
    return ConnectorCatalogOut(
        connectors=[ConnectorInfoOut.model_validate(row) for row in connector_catalog()]
    )


@router.post("", response_model=SearchOut)
def search(request: Request, body: SearchRequest) -> SearchOut:
    settings = _settings(request)
    try:
        with build_connector(body.connector, settings) as conn:
            result = conn.search(body.query, limit=body.limit)
    except ConnectorError as exc:
        raise HTTPException(
            status_code=STATUS_FOR_REASON.get(exc.reason, 500),
            detail={"reason": exc.reason, "message": exc.detail},
        ) from None
    return SearchOut.model_validate(dataclasses.asdict(result))
