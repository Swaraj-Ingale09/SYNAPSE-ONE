# SYNAPSE ONE — Project Brief

> This document is the working specification for the project. It is the source of
> truth for what gets built; code that contradicts it is wrong until this brief is
> amended through a recorded decision (see `docs/decisions/`).

## 1. Vision

SYNAPSE ONE is a persistent, extensible, multi-model AI workstation. One background
service owns intelligence, knowledge, and job state; every terminal, IDE, MCP client,
and dashboard connects to it as a thin client.

Start the station once. From any directory, any terminal:

```
synapse start      -> persistent API + worker now running in the background
synapse status      -> real service health, queue depth, job counts
synapse submit ...  -> durable job that survives terminal closure
synapse stop        -> clean shutdown
```

Closing a terminal must never stop the core. Rebooting the workflow must never lose
submitted work: jobs are durable, recoverable, and observable.

## 2. Non-negotiable rules

1. **No fake commands.** Every command has real backend behaviour. No placeholder
   success, no simulated AI activity, no "coming soon" that pretends to work.
2. **Success is printed only after verification.** A ✓ means the underlying check
   actually passed (health endpoint answered, process alive, job reached terminal
   state). Failure prints ✗ with the real reason and a stable exit code.
3. **Tests before features** (§16). A feature is not done until automated tests cover
   its happy path and its failure paths, and the suite runs against real
   infrastructure (PostgreSQL — never SQLite).
4. **Loopback-only gateway with bearer token.** The API binds to 127.0.0.1. Every
   `/v1` route requires the local client token. A local process is untrusted until it
   presents the token.
5. **Secrets never committed.** `.env` is gitignored; `.env.example` contains
   placeholder names only. Provider keys, DB passwords, and tokens are redacted from
   logs and error messages.
6. **External content is untrusted data, never policy.** Scraped or fetched content
   is treated as inert data: it can never trigger actions, change configuration, or
   inject instructions into the agent's control flow.
7. **No arbitrary shell / filesystem exposure through the API.** Capabilities are
   explicit, bounded, and permissioned.

## 3. CLI contract (Phase 1)

Commands: `start`, `status`, `doctor`, `submit`, `jobs`, `job inspect`, `job cancel`,
`events`, `stop`. Every command supports `--json` for scripting.

Stable exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error |
| 2 | core not running |
| 3 | job failed |
| 4 | job cancelled |

Deferred to later phases (implemented honestly, never faked): `ask`, `research`,
`code`, `search`, `connectors`, `dashboard`.

## 4. Job model

- Jobs are durable rows in PostgreSQL, claimed with `FOR UPDATE SKIP LOCKED`.
- Legal state transitions are defined in exactly one table
  (`synapse.contracts.LEGAL_TRANSITIONS`); every transition is validated.
- Execution uses short independent transactions (heartbeat, cancel checks, events,
  finalization) so a long-running task never blocks cancellation.
- Lease-based crash recovery: a worker restart requeues or fails stale jobs safely.
- Every lifecycle change emits a job event (`job_queued`, `job_started`,
  `job_completed`, `job_failed`, `job_cancelled`, `job_lease_expired`,
  `job_requeued`, `cancellation_requested`, `task_progress`).

## 5. Technology

- Core: Python 3.11+ (developed on 3.14), FastAPI, Typer, SQLAlchemy 2, Alembic,
  psycopg2, uvicorn, sse-starlette, psutil, rich.
- Database: PostgreSQL 17 + pgvector in Docker (`synapse-postgres`), credentials from
  `.env` `DB_PASSWORD`. Never SQLite.
- Dashboard (later): Next.js + React Three Fiber; WebGL visualization driven by real
  SSE events only, labelled as a live operational network — never fake neural theatre.

## 6. Phase plan

- **Phase 1 (current): vertical slice** — gateway, durable job queue, worker with
  cancellation and recovery, event feed, thin CLI, real tests.
- **Phase 2** — model gateway (multiple providers, redacted keys, routing),
  connectors/ingestion, `synapse ask`.
- **Phase 3** — knowledge layer (embeddings via pgvector, retrieval), `research`.
- **Phase 4** — dashboard with live event-driven visualization; MCP and IDE
  integrations.

## 7. Engineering standards

- Every phase ends with an executed, verified acceptance run recorded in the README.
- Design trade-offs are recorded as ADRs in `docs/decisions/`.
- External scraping (when implemented) prefers official APIs/RSS, respects
  robots.txt, ToS, and rate limits, and never bypasses paywalls, CAPTCHAs, or quotas.
