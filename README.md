# SYNAPSE ONE

**A persistent, extensible, multi-model AI workstation** — one background intelligence
service that every terminal, IDE, MCP client, and dashboard connects to as a client.

> Status: **Phase 1 vertical slice in development.** See `docs/BRIEF.md` for the full
> product specification. Nothing in this README is claimed to work until it has been
> executed and verified; the "Verified" section below records actual test results.

## Core idea

Start the station once. From any directory, any terminal, any supported IDE:

```
synapse start      -> persistent API + worker now running in the background
synapse status      -> real service health, queue depth, job counts
synapse submit ...  -> durable job that survives terminal closure
synapse stop        -> clean shutdown
```

Clients are thin: the intelligence, knowledge, and job state live in one service
(PostgreSQL + local gateway), not in each terminal.

## Requirements

- Python 3.11+ (developed on 3.14)
- Docker (PostgreSQL 17 + pgvector container)
- Node.js 24+ (only for the dashboard, later phases)

## Setup

```powershell
# 1. Database (once)
copy .env.example .env          # then set DB_PASSWORD in .env
docker compose --env-file .env -f infrastructure/docker/docker-compose.yml up -d

# 2. Environment (once)
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# 3. Migrations (applied automatically by `synapse start`; manual equivalent:)
.venv\Scripts\python -c "from synapse.config import get_settings; from synapse.db.migrate import upgrade_head; upgrade_head(get_settings().database_url)"

# 4. Run the test suite
.venv\Scripts\pytest
```

## Commands (Phase 1)

| Command | Behaviour |
|---|---|
| `synapse start` | Migrate DB, launch API + worker as detached processes, verify health before printing success |
| `synapse status` | Report PIDs, `/health`, `/ready`, queue depth (exit 2 if not running) |
| `synapse doctor` | Diagnose environment: Python, DB reachability, token, ports, pgvector |
| `synapse submit <task> [--data JSON] [--wait]` | Create a durable job; `--wait` streams events until terminal state |
| `synapse jobs [--status S]` | List recent jobs |
| `synapse job inspect <id>` | Job detail + event trail |
| `synapse job cancel <id>` | Cancel a queued job or flag a running one |
| `synapse events [--follow]` | Poll / stream the real event feed |
| `synapse stop` | Graceful worker stop-file + API shutdown, verified by PID and health checks |

Exit codes: `0` success · `1` error · `2` core not running · `3` job failed · `4` job cancelled.
All commands support `--json` for scripting.

Built-in Phase 1 tasks: `echo`, `text_stats`, `slow` (cancellable, used to prove
lifecycle behaviour). Model-backed tasks (`ask`, `research`, `code`) arrive in Phase 2
with the model gateway — they are **not** faked.

## MCP clients

The MCP server exposes the running core to MCP-compatible hosts over stdio:

```powershell
synapse mcp --print-config
```

Add the printed configuration to your MCP host. The host launches `synapse mcp`;
the server uses the local token without printing it. Start the core with
`synapse start` before connecting.

## Architecture

```
src/synapse/
├── contracts.py      # Pydantic: job states, transitions, event schema (spec 4.3)
├── config.py         # env-driven settings, loopback gateway (spec 15.1)
├── events.py         # job event emission (spec 14.3)
├── api/              # FastAPI gateway: health, jobs, SSE stream, system status
├── worker/           # durable job loop: claim (SKIP LOCKED), execute, recover
├── db/               # SQLAlchemy models + packaged Alembic migrations
├── security/         # bearer token + secret redaction (spec 15.2)
├── cli/              # typer CLI: thin client over the gateway (spec 10.1)
└── mcp/              # JSON-RPC 2.0 stdio server and bounded MCP tools
```

Key guarantees implemented in this phase:

- Jobs are **durable**: persisted in PostgreSQL with leases; a worker restart
  requeues or fails them safely (spec 9.2, 16.3 #4).
- State transitions are **validated** against a single legal-transition table.
- Every `/v1` route requires the **local bearer token**; the gateway binds to 127.0.0.1.
- Secrets are generated into the state directory, never committed; logs pass through
  a redaction filter.
- Success messages are printed **only after the underlying check actually passed**.

## Safety rules (non-negotiable)

- No fake commands, no placeholder success, no simulated AI activity.
- External content is untrusted data, never policy (spec 15.3).
- No arbitrary shell/filesystem exposure through the API.
- `.env` is gitignored; `.env.example` contains placeholder names only.

## Verified

Recorded from real executions on this workstation (2026-10-24, Windows 11,
Python 3.14.4, PostgreSQL 17.11 + pgvector 0.8.7 in Docker):

**Automated tests** — `pytest`: **49/49 passed** against a real PostgreSQL test
database (`synapseone_test`): state-machine contracts, secret redaction, token
persistence, API integration (auth, job lifecycle, payload limits, event feed
including SSE), worker lifecycle (execution, mid-run cancellation, failure,
lease-restart recovery), CLI behaviour (exit codes, JSON mode, honest failures).

**Lint** — `ruff check src tests`: **All checks passed.**

**Live acceptance run** (real processes, real database, real exit codes):

| Step | Result |
|---|---|
| `synapse start` | ✓ schema migrated, API pid, worker pid — exit 0 |
| `synapse status` | core ✓, health ✓, ready ✓ (db connected), worker ✓ — exit 0 |
| `synapse doctor` | 10/10 checks ✓ (python, state dir, db password, TCP, database, pgvector 0.8.7, migrations at head, token, health, readiness) |
| `synapse submit echo --data-file … --wait` | job_queued → job_started → job_completed, real result — exit 0 |
| `synapse submit text_stats --data-file … --wait` | correct word/unique/line counts — exit 0 |
| `synapse --json submit slow` + `job cancel` mid-run | cancellation observed by worker within ~1s — status `cancelled` |
| `synapse submit does_not_exist --wait` | honest failure: `unknown task_type` — exit 3 |
| `synapse jobs` | real table of persisted jobs |
| `synapse --json events` | full ordered event trail with correlation ids |
| `synapse stop` | worker stopped, core stopped, both verified — exit 0 |
| Detached persistence | core survives new shell sessions (verified across separate terminals) |

Known platform quirk: PowerShell strips embedded double quotes from native-command
arguments; use `--data-file` (or stdin) for JSON payloads containing spaces.

## Repository layout

See spec section 17 in `docs/BRIEF.md`. This repository starts as a single Python
package (`src/synapse`) plus `infrastructure/`, `tests/`, and `docs/`; the dashboard
(Next.js) and VS Code extension arrive in later phases under `apps/`.
