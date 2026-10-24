# ADR 0003: Model gateway as the single audited egress point for LLM providers

## Status

Accepted — 2026-10-24

## Context

SYNAPSE ONE must answer questions through real LLM providers (`ask` task), and
it must account for what that costs. Calling provider HTTP APIs directly from
task code would scatter API keys across the codebase, make cost accounting
optional, and leave every call site responsible for its own error handling and
redaction. The spec's honesty rule (spec 2) also demands that a missing key
produces a clear failure rather than a fabricated answer.

## Decision

- **One gateway module** (`synapse.gateway`) is the only code permitted to talk
  to Anthropic or OpenAI. Tasks call `ModelGateway.ask()` and receive text plus
  usage; they never build provider requests themselves.
- **API keys live only in the environment.** They are read by the provider
  adapters at construction, never persisted, never interpolated into logs, and
  every error string passes through `redact()` before it can be displayed.
- **Requests are built as dicts** and serialized by httpx's `json=`. No string
  interpolation of prompt content into headers or JSON, so content cannot
  escape its structure.
- **Retry only on 429.** A `429` means the provider rejected the request before
  processing it, so re-sending cannot double-bill. A `5xx` or transport error is
  ambiguous — the provider may have processed and charged us — so it is surfaced
  immediately instead of retried. For a system whose purpose is honest
  accounting, a silent double-spend is the worse failure.
- **Durable `model_usage` ledger** (migration `0002`): every call appends a row
  with the provider's own token counts, latency, attempts, and status. Cost is
  stored as `NUMERIC(12,6)`, never binary float.
- **Unknown price records `NULL`, never `0`.** `price_known=false` marks it.
  Costs are recomputable later from real token counts once a price is supplied
  (via `SYNAPSE_PRICING_JSON` override, no code change).
- **Honest failure (spec 2):** with no key configured, `ask` raises
  `NoProviderConfigured`. There is no simulated mode, no canned response, no
  fallback fake.
- **Prompt-injection defence (spec 15.3):** the system prompt is fixed in code
  and is never read from the task payload. Any ingested `context` is wrapped in
  explicit `BEGIN/END UNTRUSTED CONTENT` markers and told it is data to
  summarise, not instructions to obey.

## Consequences

- Key revocation or rotation is a single environment change; no key can be
  found in code, logs, or the database.
- Cost is auditable per job and per model, and unknown prices are visible as
  `NULL` instead of silently under-reporting spend.
- Adding a provider means one adapter in `providers.py` plus its alias entries;
  tasks and the ledger need no changes.
- Gateway failures are recorded in the ledger with zero tokens and the redacted
  error, so failed attempts are observable rather than disappearing.
- The gateway is deliberately **not** retried on `5xx`; a transient provider
  outage surfaces to the user as a real error instead of being hidden.
