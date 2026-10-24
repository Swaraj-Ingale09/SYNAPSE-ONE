# ADR 0001: Modular monolith for the core service

## Status

Accepted — 2026-10-24

## Context

The core must host an HTTP gateway, a background job worker, a task registry, and
(much later) a model gateway, connectors, and a knowledge layer. Early microservice
splitting would add network boundaries, deployment units, and failure modes before
the domain model has stabilized.

## Decision

Build the core as a **modular monolith**: a single installable Python package
(`src/synapse`) with strict internal module boundaries:

- `api/` — FastAPI gateway (transport only; no business logic)
- `worker/` — job execution (claims, leases, tasks)
- `db/` — models, sessions, migrations
- `contracts.py` — the single source of truth for states, transitions, schemas
- `cli/` — thin client over the HTTP API (never imports business logic directly)
- `security/` — token handling and secret redaction

Modules communicate through `contracts` and short database transactions, not by
reaching into each other's internals.

## Consequences

- One process to supervise, one package to test, one database to migrate.
- The CLI is forced to be a true thin client because it only imports contracts,
  config, and its HTTP client.
- Future services (if ever split out) already have clean seams: the job table is the
  queue, the event table is the bus.
