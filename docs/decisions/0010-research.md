# 0010. Research pipeline: evidence-first synthesis with per-source honesty

Date: 2026-10-27

Status: Accepted

## Context

Phase 3's flagship deliverable is a `research` capability: ask a question, gather
evidence from the web, and synthesise an answer with citations. Every layer it
needs already exists and is trusted: the connector layer (ADR 0008, eight keyless
providers), the SSRF-safe fetcher with robots enforcement (ADR 0004), the model
gateway (ADR 0003, the single audited egress for LLM calls), and the job/event
machinery (ADR 0005).

The hard part is not wiring those together — it is doing so without ever letting
the pipeline lie. A research answer is the most dangerous output this workstation
can produce: a fluent paragraph that *sounds* sourced. If the evidence gathering
partly fails (a robots-blocked page, a rate-limited connector, a provider that
returns nothing) the temptation is to answer anyway from the model's memory. That
would produce confident, uncited, unfalsifiable text — exactly the failure mode
the honesty spec (2) forbids.

## Decision

`synapse research` is **evidence-first**: the pipeline runs search → fetch →
synthesise, and synthesis only ever sees material that was actually gathered. The
commitments, in order:

1. **No evidence, no answer.** If every connector fails (`search_failed`) or the
   pool contains nothing with text (`no_sources`), the job fails with the real
   reason — every failure with its stable reason prefix and detail, e.g.
   `search_failed: every connector failed — openalex: rate_limited; github: blocked`.
   The model is never invoked to "try anyway"; there is no fallback to parametric
   knowledge, and no canned answer.

2. **Per-source degradation.** A research job survives partial failure. A blocked
   or failed fetch of one URL falls back to that result's **provider snippet** —
   the same snippet a search panel shows the user — so one dead page never zeroes
   out the question. The skip is recorded in the result's `fetch_skipped` list
   with its real reason (`blocked`, `fetch_failed`, `empty_text`) and per-connector
   search failures are recorded in `searches[].error` while the other connectors'
   results still feed the answer.

3. **Untrusted content stays data (spec 15.3).** Every source reaches the model
   as a numbered block inside an explicit fence
   (`----- BEGIN UNTRUSTED SOURCES -----` … `----- END UNTRUSTED SOURCES -----`),
   labelled with provenance (title, URL, connector, via fetched/snippet), and
   instructed *inside the message* to be summarised or cited, never obeyed. The
   **citation instruction lives outside the fence**: hostile page text cannot talk
   the model into citing sources that do not exist, because the rule that governs
   citation is not part of the quoted material. The system policy is fixed in code
   and never taken from the payload — same defence as `ask`.

4. **Provenance travels with the answer.** The result carries what entered the
   prompt, not what was merely searched: each source keeps its `index` (the `[n]`
   the model cites), title, URL, connector, `via` (`fetched` vs `snippet`), char
   count, and HTTP status when fetched. Search totals stay `None` when the
   provider reports no count — distinct from a measured `0`. Usage (tokens,
   latency, attempts, cost) comes from the gateway's own ledger; cost stays `None`
   with `price_known: false` when the model is unpriced.

## Pipeline

```
question ──► validate (≤4,000 chars, connectors known BEFORE any request)
        ──► search each connector (limit 5)      — failures recorded, pool degrades
        ──► dedupe by URL                        — first connector keeps the citation
        ──► fetch top N URLs (default 3, cap 8)  — blocked/failed → snippet fallback
        ──► budget evidence (48k chars, 16k/source, whole sources only)
        ──► synthesise via gateway               — fenced context, citations [1..n]
        ──► result with answer + sources + searches + skips + usage
```

Defaults: connectors `openalex, github, stackoverflow` (all accept free-text
queries and need no API key), 5 search results per connector, 3 fetched sources
by default (hard cap 8), 16,000 chars of text per source, 48,000 chars of total
evidence. The evidence budget drops **whole sources** before slicing one, so every
cited source is present in the prompt in full — a half-truncated paragraph cannot
be cited honestly.

Progress events (`research_started`, `research_searched`, `research_search_failed`,
`research_source_fetched`, `research_source_skipped`,
`research_synthesis_started/completed`) land in `job_events` like every other
task, so the dashboard shows the pipeline moving.

## Where it lives

- `src/synapse/research.py` — the whole pipeline as a pure function
  (`run_research`) with injected `progress` / `check_cancelled` callbacks and
  module-level `build_connector` / `build_fetcher` / `build_gateway` factories.
  No database, no worker: the pipeline is testable with fakes for all three
  external boundaries, which is how every test in `tests/test_research.py` runs.
- `worker.tasks.research_task` — payload validation and the honest mapping from
  pipeline failures to `TaskFailure("<reason>: <detail>")`, `NoProviderConfigured`
  ("no provider keys configured…") and `GatewayError`. Registered as `"research"`.
- `cli/main.py` — `synapse research "question" [--connectors …] [--max-sources N]
  [--model …] [--max-tokens …] [--project …] [--timeout …]`. The renderer prints
  the answer first, then sources (index/title/url/via/chars/HTTP status), the
  per-connector search ledger (✓/✗ with "total n/a" for unreported counts), the
  not-fetched list with real reasons, and usage with "n/a (price unknown)".

## Consequences

- An answer that exists is, by construction, an answer over material the user can
  inspect — every claim source is one click away, and the result says exactly
  which sources were read in full versus taken from a snippet.
- Partial outages degrade answer breadth instead of failing the job, but they are
  never invisible: skips and connector errors are in the result and the event
  trail, and the CLI renders them.
- Research with no configured model key fails with the gateway's honest error —
  there is no simulated mode.
- The callback-driven shape means new evidence sources (e.g. a future `code`
  task reusing the fetch-and-cite path, or MCP tools from ADR 0007) can reuse
  `run_research`'s helpers without inheriting a database dependency.
