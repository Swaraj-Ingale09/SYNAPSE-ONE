"""Typed gateway errors. Every message is user-facing and redacted upstream."""

from __future__ import annotations


class GatewayError(RuntimeError):
    """Base class for model-gateway failures."""


class NoProviderConfigured(GatewayError):
    """No API key is available for the requested provider.

    Raised honestly instead of fabricating an answer (spec 2).
    """


class ModelNotFoundError(GatewayError):
    """The requested model alias/id is unknown to the gateway."""


class ProviderAuthError(GatewayError):
    """The provider rejected our credentials (401/403)."""


class ProviderRateLimited(GatewayError):
    """The provider signalled rate limiting (429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderServerError(GatewayError):
    """The provider answered 5xx or returned an unusable payload."""
