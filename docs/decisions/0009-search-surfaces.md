# ADR 0009: Search reaches users through three surfaces, one connector layer

## Status

Accepted — 2026-10-26

## Context

ADR 0008 shipped eight free, keyless connectors — but only from the CLI
(`synapse search`, direct to the provider, no core required). Two callers then
wanted in: the dashboard needs interactive search with instant results, and
the job queue's parity rule (every capability observable as a durable job,
like `ask` and `ingest_url`) suggests search should be queueable too. These
pull in opposite directions: an interactive result should arrive in one HTTP
round-trip, while a queued job costs a row, a worker lease, and seconds of
latency before it even starts. A third constraint binds both: search leaves
the machine, so every path must keep the SSRF gate, robots checks, pacing
floors, and honest failure reasons intact — no surface may become a softer
door to the same network.

## Decision

**1. One connector layer, three surfaces.**
All surfaces call the same `build_connector(...).search(...)`; none rewrites
URLs, parses responses, or interprets rate limits itself.

| Surface | Path | Latency | Use |
|---|---|---|---|
| CLI direct | `synapse search` → connectors in-process | one round-trip | scripting, no core needed |
| Sync API | `POST /v1/search` (+ `GET /v1/search/connectors`) | one round-trip | interactive dashboard panel |
| Durable task | `search` in `TASKS`, CLI `synapse search --queue` | queued job | parity with `ask`/`ingest`, inspectable results |

**2. The interactive path is synchronous, and that is deliberate.**
A search is one bounded HTTP call: 1 MiB response cap, a 1 s pacing floor,
429-retries capped at 10 s each (ADR 0008). It creates no durable state worth
keeping, and the dashboard has no job-result view — queueing an interactive
search would add a row the user cannot read from. So `POST /v1/search` runs
the connector in the request thread (FastAPI threadpool) and returns the
connector's `SearchResponse` verbatim.

**3. The task exists for callers who want durability.**
`search` in the worker registry validates the payload (non-empty connector
and query, integer limit 1–100), runs the same connector, and stores
`dataclasses.asdict(response)` as the job result — provider total, rate
report and all, JSON-round-tripped without loss. The CLI's `--queue` flag
submits it and renders the job result with the *same* renderer the direct
path uses (both read dict keys), so the two paths cannot drift apart. A
failure becomes `TaskFailure("<reason>: <detail>")` — the stable reason stays
machine-parseable in `job.error`.

**4. Errors carry their stable reason, mapped once.**
The sync endpoint converts `ConnectorError.reason` to a status and returns
`{"detail": {"reason": ..., "message": ...}}`:

| reason | HTTP |
|---|---|
| `invalid_query` | 400 |
| `blocked` | 403 |
| `unknown_connector` | 404 |
| `rate_limited` | 429 |
| `http_error` / `fetch_failed` / `invalid_response` | 502 |
| (any future reason) | 500 |

The dashboard renders the reason as a chip and the message as text; a search
failure can never look like an empty result set.

**5. The panel shows only what the response contained.**
`total: null` renders "count not reported by provider" (never `0`); a missing
URL or date renders "n/a"; facts — including snippet-trim disclosures — are
shown verbatim; an empty item list states "The provider returned no results
for this query." rather than showing a blank box.

## Consequences

- Adding a ninth connector changes nothing but the registry: catalog, CLI,
  task, endpoint, and dropdown all derive from it.
- The dashboard gained a live Search panel (right column) that fails loudly
  with the server's real reason (`rate_limited`, `blocked`, …).
- Tests cover every surface without network: fakes replace `build_connector`
  at each call site, and validation paths fail before any fetch
  (`tests/test_search_tasks.py`, `test_api.py`, `test_cli.py`).
- A future MCP or IDE client can reuse `POST /v1/search` unchanged — the
  contract file (`api/search_contracts.py`) and its TypeScript mirror
  (`dashboard/src/api.ts`) are the single wire definition.
