"""Client token management for the local gateway (spec 15.1).

A random token is generated on first start and stored in the SYNAPSE state
directory (user-profile scoped on Windows). CLI and dashboard clients read
the same file. The gateway never accepts anonymous requests to /v1 routes.
"""

from __future__ import annotations

import secrets

from synapse.config import Settings


def ensure_token(settings: Settings) -> str:
    """Return the persistent client token, generating it on first use."""
    if settings.token:
        return settings.token
    if settings.token_file.exists():
        existing = settings.token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(token, encoding="utf-8")
    return token
