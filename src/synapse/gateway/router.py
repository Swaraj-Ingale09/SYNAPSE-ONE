"""The model gateway router: resolution, safe retries, cost accounting (ADR 0003).

This is the single place a task touches to talk to an LLM. It:

1. resolves a model alias (``sonnet``) or a literal ``provider/model`` id to a
   :class:`~synapse.gateway.types.ModelRef`
2. refuses honestly with :class:`NoProviderConfigured` when the resolved
   provider has no API key in the environment — there is no simulated mode
3. performs the call with exactly one retry, and only for ``429`` (see below)
4. prices the reply from the bundled table (or ``SYNAPSE_PRICING_JSON``)
5. appends a row to the durable ``model_usage`` ledger

Retry policy — why only 429:
    A ``429`` means the provider *rejected* the request before processing it,
    so re-sending cannot double-bill or double-execute. A ``5xx`` or a
    transport error is ambiguous: the provider may well have processed and
    charged us before failing, so retrying could silently double the cost.
    For a system whose whole point is honest accounting, we surface those
    immediately instead of retrying.

Cost honesty:
    ``cost_usd`` is ``None`` when the model has no known price. It is never
    guessed as 0. The ledger always stores the provider's own token counts,
    so a cost can be recomputed later from real numbers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from synapse.config import Settings
from synapse.db import session_scope
from synapse.db.models import ModelUsage
from synapse.gateway.errors import (
    GatewayError,
    ModelNotFoundError,
    NoProviderConfigured,
    ProviderRateLimited,
)
from synapse.gateway.pricing import ModelPrice, cost_usd, price_for
from synapse.gateway.providers import PROVIDERS, Provider, provider_is_configured
from synapse.gateway.types import ChatMessage, ModelRef, assert_valid_model_id
from synapse.security import redact

log = logging.getLogger("synapse.gateway.router")

DEFAULT_SYSTEM_PROMPT = "You are SYNAPSE ONE, a precise technical assistant."
DEFAULT_MAX_TOKENS = 1024
MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.0

# Friendly aliases -> exact provider model id. Users should not have to know
# provider version strings; explicit "provider/model" still works.
MODEL_ALIASES: dict[str, ModelRef] = {
    "sonnet": ModelRef("anthropic", "claude-sonnet-4-6", alias="sonnet"),
    "haiku": ModelRef("anthropic", "claude-haiku-4-5", alias="haiku"),
    "opus": ModelRef("anthropic", "claude-opus-4-8", alias="opus"),
    "gpt": ModelRef("openai", "gpt-5.2", alias="gpt"),
    "gpt-mini": ModelRef("openai", "gpt-5-mini", alias="gpt-mini"),
}
DEFAULT_MODEL_ALIAS = "sonnet"


@dataclass(frozen=True)
class GatewayUsage:
    """Cost and token accounting for one completed request."""

    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None => price unknown, never guessed
    price_known: bool
    price: ModelPrice | None
    attempts: int
    latency_ms: int


@dataclass(frozen=True)
class ModelReply:
    """What a task sees back from the gateway."""

    text: str
    model: ModelRef
    usage: GatewayUsage
    stop_reason: str | None


def resolve_model(name: str | None) -> ModelRef:
    """Resolve an alias or literal ``provider/model`` id to a ModelRef.

    Raises :class:`ModelNotFoundError` for anything unknown — including a
    known alias that points at a provider we do not implement.
    """
    key = (name or DEFAULT_MODEL_ALIAS).strip()
    if not key:
        key = DEFAULT_MODEL_ALIAS

    alias = MODEL_ALIASES.get(key.lower())
    if alias is not None:
        if alias.provider not in PROVIDERS:
            raise ModelNotFoundError(
                f"model {key!r} targets provider {alias.provider!r}, which is not implemented"
            )
        return alias

    # Not an alias: require an explicit "provider/model" so an unqualified
    # string can never silently select a provider.
    if "/" not in key:
        raise ModelNotFoundError(
            f"unknown model {key!r} (use an alias {sorted(MODEL_ALIASES)} "
            f"or a literal 'provider/model' id)"
        )
    provider, _, model_id = key.partition("/")
    if provider not in PROVIDERS:
        raise ModelNotFoundError(
            f"unknown provider {provider!r} (implemented: {sorted(PROVIDERS)})"
        )
    assert_valid_model_id(model_id)
    return ModelRef(provider, model_id, alias=key)


def available_models() -> list[dict[str, object]]:
    """Model inventory for the CLI: alias, provider, configured, price."""
    rows: list[dict[str, object]] = []
    for alias, ref in sorted(MODEL_ALIASES.items()):
        price = price_for(ref.provider, ref.model_id)
        rows.append(
            {
                "alias": alias,
                "provider": ref.provider,
                "model_id": ref.model_id,
                "configured": provider_is_configured(ref.provider),
                "input_per_mtok": price.input_per_mtok if price else None,
                "output_per_mtok": price.output_per_mtok if price else None,
            }
        )
    return rows


class ModelGateway:
    """One audited egress point for all LLM providers.

    Construct once per process via :func:`build_gateway`. Holds a single
    ``httpx.Client`` so connections are pooled; the API key is read from the
    environment by the provider adapters and never stored on this object in a
    way that could be serialised or logged.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._settings = settings
        self._system_prompt = system_prompt
        self._max_retries = max(0, max_retries)
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ModelGateway:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- the call -----------------------------------------------------------

    def ask(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        job_id: str | None = None,
    ) -> ModelReply:
        """Send a chat request and return text plus honest usage accounting.

        Raises a typed :class:`~synapse.gateway.errors.GatewayError` on any
        failure. The exception message is redacted before it can reach a log
        or a client, so a provider echoing a credential back cannot leak it.
        """
        ref = resolve_model(model)
        if not messages:
            raise ValueError("messages must not be empty")
        provider: Provider = PROVIDERS[ref.provider]
        if not provider_is_configured(ref.provider):
            raise NoProviderConfigured(
                f"no API key configured for {ref.provider!r} — set "
                f"{ref.provider.upper()}_API_KEY in your environment"
            )

        effective_system = system if system is not None else self._system_prompt
        started = time.monotonic()
        attempts = 0
        reply = None
        last_error: Exception | None = None

        while attempts <= self._max_retries:
            attempts += 1
            try:
                reply = provider.complete(
                    ref,
                    messages,
                    system=effective_system,
                    max_tokens=max_tokens,
                    client=self._client,
                )
                break
            except ProviderRateLimited as exc:
                last_error = exc
                if attempts > self._max_retries:
                    break
                # Only 429 is ever retried (see module docstring).
                backoff = RETRY_BACKOFF_SECONDS * attempts
                log.warning(
                    "gateway_retry provider=%s model=%s retry_after=%s backoff=%.1fs",
                    ref.provider,
                    ref.model_id,
                    exc.retry_after,
                    backoff,
                )
                time.sleep(backoff)
            except GatewayError as exc:
                # Non-retryable gateway failure (5xx, auth, unknown model):
                # fall through to the recording branch below so the ledger
                # captures the attempt, then surface it unchanged.
                last_error = exc
                break

        latency_ms = int((time.monotonic() - started) * 1000)

        if reply is None:
            message = redact(str(last_error)) if last_error else "model gateway failed"
            self._record_usage(
                ref,
                input_tokens=0,
                output_tokens=0,
                cost=None,
                attempts=attempts,
                latency_ms=latency_ms,
                status="failed",
                stop_reason=None,
                job_id=job_id,
                error=message[:1000],
            )
            if last_error is not None:
                raise last_error
            raise RuntimeError(message)

        price = price_for(ref.provider, ref.model_id)
        computed = cost_usd(price, reply.input_tokens, reply.output_tokens)
        usage = GatewayUsage(
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost_usd=computed,
            price_known=price is not None,
            price=price,
            attempts=attempts,
            latency_ms=latency_ms,
        )
        self._record_usage(
            ref,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost=computed,
            attempts=attempts,
            latency_ms=latency_ms,
            status="succeeded",
            stop_reason=reply.stop_reason,
            job_id=job_id,
        )
        return ModelReply(text=reply.text, model=ref, usage=usage, stop_reason=reply.stop_reason)

    # --- ledger --------------------------------------------------------------

    def _record_usage(
        self,
        ref: ModelRef,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: float | None,
        attempts: int,
        latency_ms: int,
        status: str,
        stop_reason: str | None,
        job_id: str | None,
        error: str | None = None,
    ) -> None:
        """Append one ledger row. A ledger failure must never lose the reply."""
        try:
            with session_scope(self._settings.database_url) as session:
                session.add(
                    ModelUsage(
                        job_id=job_id,
                        provider=ref.provider,
                        model_id=ref.model_id,
                        alias=ref.alias,
                        input_tokens=max(0, int(input_tokens)),
                        output_tokens=max(0, int(output_tokens)),
                        cost_usd=Decimal(str(cost)) if cost is not None else None,
                        price_known=cost is not None,
                        latency_ms=max(0, int(latency_ms)),
                        attempts=max(1, int(attempts)),
                        status=status,
                        error=error,
                        stop_reason=stop_reason,
                        correlation_id=job_id,
                    )
                )
        except Exception:  # noqa: BLE001 - accounting must not break the answer
            log.exception(
                "ledger_write_failed provider=%s model=%s job_id=%s",
                ref.provider,
                ref.model_id,
                job_id,
            )


def build_gateway(settings: Settings, **kwargs: object) -> ModelGateway:
    """Construct a gateway bound to these settings."""
    return ModelGateway(settings, **kwargs)  # type: ignore[arg-type]
