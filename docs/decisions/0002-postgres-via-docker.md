# ADR 0002: PostgreSQL in Docker as the only database

## Status

Accepted — 2026-10-24

## Context

The knowledge layer will need vector similarity search; the job queue needs real row
locking (`FOR UPDATE SKIP LOCKED`); tests must exercise the same engine as production.
SQLite provides none of these, and "works on SQLite, breaks on Postgres" is a known
failure class that this project refuses to risk.

## Decision

- **PostgreSQL 17 + pgvector** running in a local Docker container
  (`synapse-postgres`, image `pgvector/pgvector:pg17`) is the only supported database.
- Connection settings come from the environment (`.env`, `DB_PASSWORD`); the
  password is never hardcoded or committed.
- Tests run against a dedicated real database (`synapseone_test`) created and
  migrated by the test fixtures — never SQLite, never mocks of the storage layer.
- The `vector` extension is created in the initial Alembic migration so the knowledge
  layer can add embedding columns without a special bootstrap path.

## Consequences

- Row locking, JSONB payloads, timezones, and migrations behave identically in dev,
  test, and future deployments.
- Docker Desktop must be running before `synapse start` or `pytest`; `synapse doctor`
  diagnoses this explicitly.
- Costs a few hundred MB of RAM locally; acceptable for correctness.
