"""Security helpers: token auth and secret redaction."""

from synapse.security.redact import redact
from synapse.security.tokens import ensure_token

__all__ = ["ensure_token", "redact"]
