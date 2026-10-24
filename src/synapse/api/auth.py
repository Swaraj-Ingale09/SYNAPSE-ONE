"""Bearer-token authentication for the local gateway (spec 15.1).

The gateway binds to loopback, but a local process is still untrusted until
it presents the shared client token stored in the SYNAPSE state directory.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Reject any /v1 request that does not present the valid bearer token."""
    expected = getattr(request.app.state, "token", None)
    if not expected:
        raise HTTPException(status_code=503, detail="gateway token not initialised")
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
