"""Model gateway: one audited egress point for all LLM providers (ADR 0003).

Why a gateway instead of calling providers from tasks:
- API keys exist in exactly one place (environment), are never persisted,
  never logged (all errors pass through ``redact``), and never leave this layer
- every request is timed, retried only where safe, costed, and recorded in the
  durable ``model_usage`` ledger
- tasks see a tiny interface: send messages, get text + usage back

Honesty rules (spec 2): with no provider key configured, ``ask`` fails with a
clear error. There is no simulated mode, no canned response, no fallback fake.
"""

from synapse.gateway.errors import (
    GatewayError,
    ModelNotFoundError,
    NoProviderConfigured,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderServerError,
)
from synapse.gateway.pricing import ModelPrice, price_for
from synapse.gateway.router import (
    GatewayUsage,
    ModelGateway,
    ModelReply,
    build_gateway,
)
from synapse.gateway.types import ChatMessage, ModelRef

__all__ = [
    "ChatMessage",
    "GatewayError",
    "GatewayUsage",
    "ModelGateway",
    "ModelNotFoundError",
    "ModelPrice",
    "ModelRef",
    "ModelReply",
    "NoProviderConfigured",
    "ProviderAuthError",
    "ProviderRateLimited",
    "ProviderServerError",
    "build_gateway",
    "price_for",
]
