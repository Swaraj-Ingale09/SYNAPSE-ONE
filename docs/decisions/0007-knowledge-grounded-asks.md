# ADR 0007: Ask grounds itself in stored knowledge; ingest persists

## Status

Accepted — 2026-10-25

## Context

Phases 2 and 3 shipped as parallel islands: `ask` could call the model gateway
but never consulted stored knowledge, and `ingest_url` fetched a URL through
the SSRF gate but its extracted text stopped at the job result. The knowledge
layer (ADR 0005) had an ingestion path and a hybrid retriever, and neither piece
of existing machinery used them at runtime. The workstation's core promise —
ingest documents, then ask questions that benefit — was not yet true end-to-end.

## Decision

**1. `ask` auto-retrieves by default.**
When a payload provides no explicit `context`, the task searches the knowledge
store with the prompt itself (hybrid retrieval: full-text + vector fused with
RRF, ADR 0005; query embedding comes from the configured embedder). Retrieved
chunks are assembled as provenance-labelled untrusted context (same fence as
explicit context, spec 15.3) and the answer's result carries `sources` — real
document/chunk ids, rank scores, and the retriever label.

- `payload.knowledge: false` disables retrieval for one ask.
- An explicit `payload.context` wins and no retrieval runs: callers who bring
  their own context keep exact control, nothing is silently mixed in.
- Fetch budget: up to 5 hits (rank-ordered); whole chunks are dropped before
  splitting one mid-chunk, so every cited source is present verbatim.

**2. `ingest_url` persists into the knowledge store by default.**
`payload.store: false` keeps the old fetch-only behaviour. When stored, the
complete text lives in `documents` (content-hash deduped); the job result keeps
a bounded preview plus the `knowledge` facts (document id, chunk count,
deduplicated flag). The fetch's former full-text result copy is removed when
stored, because duplicating up to 200 kB per job row adds no information.

**3. Failures degrade honestly, in one direction only.**
- Retrieval failing (database unreachable, embedder error) must **not** fail
  the ask: it emits `knowledge_context_unavailable` and the answer proceeds
  ungrounded with `sources: []`. A missing API provider key still fails the
  ask outright (bad-input honesty, unchanged).
- Storage failing after a successful fetch **does** fail the ingest job
  (`knowledge store refused/failed`): reporting "ingested" while dropping the
  text would be a lie the user pays for later.

**4. Every grounding is observable.**
`knowledge_context_used`, `knowledge_context_unavailable`,
`knowledge_stored`, and `knowledge_deduplicated` events carry real ids and
counts. Nothing here fabricates a score, a "confidence", or a fallback answer;
the dashboard's later model-router/graph views must keep treating these fields
as their only source (null → n/a).

## Consequences

- The prompt bar (dashboard and `synapse ask`) becomes genuinely useful after
  an ingest: grounded answers, traceable to chunks.
- `ask` now performs one extra database read per ask with no explicit context —
  bounded (≤ 5 chunks), negligible against a real model call.
- Tests (`tests/test_knowledge_tasks.py`) prove persistence, dedup, grounding
  provenance, the explicit-context override, and the degrade-without-lie path.
