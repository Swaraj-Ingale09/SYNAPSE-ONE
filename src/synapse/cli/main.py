"""``synapse`` CLI — a thin, honest client of the persistent core (spec 10.1).

Rules enforced here:
- Success messages are printed only after the underlying check actually passed.
- Exit codes are stable and documented: 0 ok, 1 error, 2 core not running,
  3 job failed, 4 job cancelled.
- ``--json`` switches every command to machine-readable output for scripts.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from synapse import __version__
from synapse.cli.client import ApiError, CoreNotRunning, SynapseClient
from synapse.cli.processes import (
    log_size,
    process_alive,
    read_pid_file,
    spawn_detached,
    terminate_process,
    wait_for_log_line,
    write_pid_file,
)
from synapse.config import Settings, dashboard_dist_dir, get_settings
from synapse.connectors import (
    ConnectorError,
    build_connector,
    connector_catalog,
    connector_names,
)
from synapse.contracts import TERMINAL_STATES, Job, JobStatus
from synapse.research import (
    DEFAULT_RESEARCH_CONNECTORS,
    RESEARCH_DEFAULT_SOURCES,
    RESEARCH_MAX_QUESTION_CHARS,
    RESEARCH_MAX_SOURCES,
)
from synapse.security import ensure_token, redact

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_RUNNING = 2
EXIT_JOB_FAILED = 3
EXIT_JOB_CANCELLED = 4


def _force_utf8_console() -> None:
    """Windows terminals often default to cp1252; our output includes ✓ ✗ ⚠ → ·."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass  # captured/test streams without reconfigure support


_force_utf8_console()

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="synapse",
    help="SYNAPSE ONE — persistent AI workstation. Start the core once, connect from anywhere.",
    no_args_is_help=True,
    add_completion=False,
)
job_app = typer.Typer(help="Inspect and control jobs.", no_args_is_help=True)
app.add_typer(job_app, name="job")


@dataclass
class State:
    json_output: bool = False


def _state(ctx: typer.Context) -> State:
    if not isinstance(ctx.obj, State):
        ctx.obj = State()
    return ctx.obj


@contextmanager
def _client_errors() -> Iterator[None]:
    """Map transport/API errors to stable exit codes with honest messages."""
    try:
        yield
    except CoreNotRunning as exc:
        err_console.print(f"[red]✗ {exc}[/red]")
        err_console.print("[dim]Start the core with: synapse start[/dim]")
        raise typer.Exit(EXIT_NOT_RUNNING) from None
    except ApiError as exc:
        err_console.print(f"[red]✗ {exc.detail}[/red]")
        raise typer.Exit(EXIT_ERROR) from None


def _health_ok(settings: Settings) -> bool:
    try:
        return httpx.get(f"{settings.api_url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def _emit(state: State, payload: Any, render) -> None:
    if state.json_output:
        typer.echo(json.dumps(payload, default=str, ensure_ascii=False))
    else:
        render()


# --- root ---------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    """Eager param callback: fires during option parsing, no subcommand needed."""
    if value:
        typer.echo(__version__)
        raise typer.Exit(EXIT_OK)


@app.callback()
def _root(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    ctx.obj = State(json_output=json_output)


# --- start / stop --------------------------------------------------------------


@app.command()
def start(ctx: typer.Context) -> None:
    """Migrate the database and launch the core service + worker in the background."""
    state = _state(ctx)
    settings = get_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    token = ensure_token(settings)
    # PYTHONUTF8 keeps detached child logs safe from cp1252 encoding errors.
    child_env = {**os.environ, "SYNAPSE_TOKEN": token, "PYTHONUTF8": "1"}
    report: dict[str, Any] = {"url": settings.api_url}

    # 1. Schema must be current before serving (idempotent).
    try:
        from synapse.db.migrate import upgrade_head

        upgrade_head(settings.database_url)
    except Exception as exc:  # noqa: BLE001 - surface the real failure, redacted
        err_console.print(f"[red]✗ Database migration failed: {redact(str(exc))}[/red]")
        raise typer.Exit(EXIT_ERROR) from None
    report["database"] = "migrated"
    messages = ["[green]✓[/green] Database schema up to date"]

    # 2. API process.
    api_record = read_pid_file(settings.api_pid_file)
    if process_alive(api_record) and _health_ok(settings):
        pid = int(api_record["pid"])  # type: ignore[index]
        report["api"] = {"status": "already_running", "pid": pid, "url": settings.api_url}
        messages.append(
            f"[green]✓[/green] Core service already running (pid {pid}, {settings.api_url})"
        )
    else:
        if api_record and not process_alive(api_record):
            settings.api_pid_file.unlink(missing_ok=True)
        api_log = settings.logs_dir / "api.log"
        try:
            pid = spawn_detached("synapse.api.server", api_log, child_env, Path.cwd())
        except OSError as exc:
            err_console.print(f"[red]✗ Could not spawn API process: {exc}[/red]")
            raise typer.Exit(EXIT_ERROR) from exc
        write_pid_file(settings.api_pid_file, pid, "api")

        healthy = False
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _health_ok(settings):
                healthy = True
                break
            record = read_pid_file(settings.api_pid_file)
            if record and not process_alive(record):
                break  # process died during startup
            time.sleep(0.5)
        if not healthy:
            err_console.print(
                f"[red]✗ Core service failed to become healthy (pid {pid}) — see {api_log}[/red]"
            )
            raise typer.Exit(EXIT_ERROR)
        report["api"] = {"status": "started", "pid": pid, "url": settings.api_url}
        messages.append(f"[green]✓[/green] Core service started (pid {pid}, {settings.api_url})")

    # 3. Worker process (readiness = real log line emitted by the worker itself).
    worker_record = read_pid_file(settings.worker_pid_file)
    if process_alive(worker_record):
        wpid = int(worker_record["pid"])  # type: ignore[index]
        report["worker"] = {"status": "already_running", "pid": wpid}
        messages.append(f"[green]✓[/green] Background worker already running (pid {wpid})")
    else:
        if worker_record and not process_alive(worker_record):
            settings.worker_pid_file.unlink(missing_ok=True)
        worker_log = settings.logs_dir / "worker.log"
        offset = log_size(worker_log)
        try:
            wpid = spawn_detached("synapse.worker.main", worker_log, child_env, Path.cwd())
        except OSError as exc:
            err_console.print(f"[red]✗ Could not spawn worker process: {exc}[/red]")
            raise typer.Exit(EXIT_ERROR) from exc
        write_pid_file(settings.worker_pid_file, wpid, "worker")
        if wait_for_log_line(worker_log, "worker_started", timeout=12.0, start_offset=offset):
            report["worker"] = {"status": "ready", "pid": wpid}
            messages.append(f"[green]✓[/green] Background worker running (pid {wpid})")
        else:
            report["worker"] = {
                "status": "no_readiness_signal",
                "pid": wpid,
                "log": str(worker_log),
            }
            messages.append(
                f"[yellow]⚠[/yellow] Worker spawned (pid {wpid}) but has not reported ready "
                f"after 12s — check {worker_log}"
            )

    def render() -> None:
        for line in messages:
            console.print(line)

    _emit(state, report, render)


@app.command()
def stop(ctx: typer.Context) -> None:
    """Stop the worker gracefully, then the core service; verify each stop."""
    state = _state(ctx)
    settings = get_settings()
    report: dict[str, Any] = {}
    messages: list[str] = []

    # Worker: stop-file first (clean exit), hard terminate as fallback.
    worker_record = read_pid_file(settings.worker_pid_file)
    if process_alive(worker_record):
        wpid = int(worker_record["pid"])  # type: ignore[index]
        settings.worker_stop_file.touch()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and process_alive(worker_record):
            time.sleep(0.2)
        if process_alive(worker_record):
            stopped = terminate_process(worker_record)
            report["worker"] = {"status": "terminated" if stopped else "unstoppable", "pid": wpid}
            if stopped:
                messages.append(
                    f"[yellow]⚠[/yellow] Worker did not exit in time; terminated (pid {wpid})"
                )
            else:
                messages.append(f"[red]✗[/red] Worker (pid {wpid}) could not be stopped")
        else:
            report["worker"] = {"status": "stopped", "pid": wpid}
            messages.append(f"[green]✓[/green] Worker stopped (pid {wpid})")
    else:
        report["worker"] = {"status": "not_running"}
    settings.worker_stop_file.unlink(missing_ok=True)
    settings.worker_pid_file.unlink(missing_ok=True)

    # API: clean shutdown endpoint first, terminate as fallback.
    api_record = read_pid_file(settings.api_pid_file)
    api_pid = int(api_record["pid"]) if process_alive(api_record) else None
    if _health_ok(settings):
        try:
            with SynapseClient(settings, timeout=5.0) as client:
                client.shutdown()
        except (CoreNotRunning, ApiError):
            pass
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and _health_ok(settings):
            time.sleep(0.2)
        if _health_ok(settings):
            if api_record:
                terminate_process(api_record)
            still_up = _health_ok(settings)
            report["api"] = {"status": "unstoppable" if still_up else "terminated", "pid": api_pid}
            if still_up:
                messages.append("[red]✗[/red] Core service still responding after stop attempts")
            else:
                messages.append(f"[yellow]⚠[/yellow] Core service terminated (pid {api_pid})")
        else:
            report["api"] = {"status": "stopped", "pid": api_pid}
            messages.append("[green]✓[/green] Core service stopped")
    elif process_alive(api_record):
        terminate_process(api_record)
        report["api"] = {"status": "terminated", "pid": api_pid}
        messages.append(
            f"[yellow]⚠[/yellow] Core service had no health endpoint; terminated (pid {api_pid})"
        )
    else:
        report["api"] = {"status": "not_running"}
    settings.api_pid_file.unlink(missing_ok=True)

    if report["worker"]["status"] == "not_running" and report["api"]["status"] == "not_running":
        def render_idle() -> None:
            console.print("[dim]Core is not running.[/dim]")

        _emit(state, report, render_idle)
        return

    def render() -> None:
        for line in messages:
            console.print(line)

    _emit(state, report, render)


# --- status / doctor ------------------------------------------------------------


@app.command()
def status(ctx: typer.Context) -> None:
    """Report real service health, readiness, worker state, and queue depth."""
    state = _state(ctx)
    settings = get_settings()
    api_record = read_pid_file(settings.api_pid_file)
    worker_record = read_pid_file(settings.worker_pid_file)
    api_alive = process_alive(api_record)
    worker_alive = process_alive(worker_record)
    health = _health_ok(settings)

    if not api_alive and not health:
        report = {"running": False, "url": settings.api_url, "api": None, "worker": None}

        def render_down() -> None:
            console.print("[red]✗ SYNAPSE core is not running.[/red]")
            console.print("[dim]Start it with: synapse start[/dim]")

        _emit(state, report, render_down)
        raise typer.Exit(EXIT_NOT_RUNNING)

    report: dict[str, Any] = {
        "running": True,
        "url": settings.api_url,
        "api": {"alive": api_alive, "pid": api_record.get("pid") if api_record else None},
        "worker": {
            "alive": worker_alive,
            "pid": worker_record.get("pid") if worker_record else None,
        },
        "health": None,
        "ready": None,
        "system": None,
    }
    client_errors = False
    if health:
        try:
            with SynapseClient(settings, timeout=5.0) as client:
                try:
                    report["health"] = client.health()
                except CoreNotRunning:
                    client_errors = True
                try:
                    report["ready"] = client.ready()
                except CoreNotRunning:
                    client_errors = True
                try:
                    report["system"] = client.system_status()
                except (CoreNotRunning, ApiError):
                    client_errors = True
        except CoreNotRunning:
            client_errors = True

    def render_up() -> None:
        console.print(f"[bold]SYNAPSE ONE[/bold] — {settings.api_url}")
        api_line = (
            f"[green]✓ running[/green] (pid {report['api']['pid']})" if api_alive
            else "[yellow]process not tracked[/yellow]"
        )
        console.print(f"  Core:   {api_line}")
        if health and report["health"]:
            console.print("  Health: [green]✓ ok[/green]")
        else:
            console.print("  Health: [red]✗ unreachable[/red] (process alive but not answering)")
        ready = report["ready"]
        if ready and ready.get("status") == "ready":
            console.print("  Ready:  [green]✓ ready[/green] (database connected)")
        elif ready:
            ready_detail = ready.get("detail") or ready.get("database")
            console.print(f"  Ready:  [red]✗ {ready.get('status')}[/red] — {ready_detail}")
        else:
            console.print("  Ready:  [dim]n/a[/dim]")
        worker_line = (
            f"[green]✓ running[/green] (pid {report['worker']['pid']})" if worker_alive
            else "[red]✗ not running[/red]"
        )
        console.print(f"  Worker: {worker_line}")
        system = report["system"]
        if system:
            counts = system.get("jobs", {})
            interesting = {k: v for k, v in counts.items() if v}
            rendered = ", ".join(f"{k}={v}" for k, v in interesting.items()) or "no jobs"
            console.print(f"  Queue:  {system.get('queue_depth', 0)} queued · {rendered}")

    _emit(state, report, render_up)
    if not health or client_errors:
        raise typer.Exit(EXIT_ERROR)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Diagnose the local environment: Python, database, extensions, token, ports."""
    state = _state(ctx)
    settings = get_settings()
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # Python
    py = sys.version_info
    py_detail = f"{py.major}.{py.minor}.{py.micro}"
    add("python", "ok" if py >= (3, 11) else "fail", py_detail)

    # State directory writable
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.state_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("state_dir", "ok", str(settings.state_dir))
    except OSError as exc:
        add("state_dir", "fail", f"not writable: {exc}")

    # DB password configured (never print the value)
    add(
        "db_password",
        "ok" if settings.database_password_set else "fail",
        "set" if settings.database_password_set else "DB_PASSWORD missing (see .env.example)",
    )

    # TCP reachability
    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    try:
        with socket.create_connection((db_host, db_port), timeout=3.0):
            add("db_tcp", "ok", f"{db_host}:{db_port} accepting connections")
    except OSError:
        add(
            "db_tcp",
            "fail",
            f"{db_host}:{db_port} unreachable — is the container up? (docker compose up -d)",
        )
        db_ok = False
    else:
        db_ok = True

    # Database query + pgvector + migrations
    if db_ok:
        from synapse.db import check_database
        from synapse.db.migrate import alembic_config

        reachable, detail = check_database(settings.database_url)
        add(
            "database",
            "ok" if reachable else "fail",
            detail if reachable else redact(detail) or "connection failed",
        )
        if reachable:
            try:
                from sqlalchemy import text

                from synapse.db import session_scope

                with session_scope(settings.database_url) as session:
                    version = session.execute(
                        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                    ).scalar_one_or_none()
                    try:
                        applied = session.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one_or_none()
                    except Exception:  # noqa: BLE001 - table missing means no migrations yet
                        session.rollback()
                        applied = None
                add(
                    "pgvector",
                    "ok" if version else "fail",
                    f"vector {version}" if version else "extension not created — run synapse start",
                )
                try:
                    from alembic.script import ScriptDirectory

                    head = (
                        ScriptDirectory.from_config(alembic_config(settings.database_url))
                        .get_current_head()
                    )
                except Exception as exc:  # noqa: BLE001
                    head = f"error: {type(exc).__name__}"
                if applied is None:
                    add("migrations", "fail", "no migrations applied — run synapse start")
                elif applied == head:
                    add("migrations", "ok", f"at head ({head})")
                else:
                    add("migrations", "fail", f"applied={applied} head={head} — run synapse start")
            except Exception as exc:  # noqa: BLE001
                add("pgvector", "fail", redact(f"{type(exc).__name__}: {exc}") or "query failed")

    # Token
    if settings.token or settings.token_file.exists():
        add("client_token", "ok", f"present ({settings.token_file})")
    else:
        add("client_token", "warn", "will be generated on first synapse start")

    # API port / health
    health = _health_ok(settings)
    if health:
        add("api", "ok", f"healthy at {settings.api_url}")
        try:
            with SynapseClient(settings, timeout=5.0) as client:
                ready = client.ready()
            status_value = ready.get("status", "unknown")
            ready_detail = str(ready.get("detail") or status_value)
            add(
                "api_ready",
                "ok" if status_value == "ready" else "fail",
                status_value if status_value == "ready" else ready_detail,
            )
        except Exception:  # noqa: BLE001
            add("api_ready", "fail", "readiness probe failed")
    else:
        try:
            with socket.create_connection((settings.api_host, settings.api_port), timeout=1.0):
                add("api", "warn", f"port {settings.api_port} in use by another process")
        except OSError:
            add("api", "warn", "core not running (start it with: synapse start)")

    failures = sum(1 for c in checks if c["status"] == "fail")

    if state.json_output:
        typer.echo(json.dumps({"checks": checks, "failures": failures}, ensure_ascii=False))
    else:
        table = Table(title="SYNAPSE doctor", show_lines=False)
        table.add_column("STATUS", justify="center")
        table.add_column("CHECK")
        table.add_column("DETAIL")
        style_map = {"ok": "green", "warn": "yellow", "fail": "red"}
        symbol_map = {"ok": "✓", "warn": "⚠", "fail": "✗"}
        for check in checks:
            style = style_map[check["status"]]
            table.add_row(f"[{style}]{symbol_map[check['status']]}[/{style}]",
                          check["check"], check["detail"])
        console.print(table)
        if failures:
            console.print(f"[red]{failures} check(s) failed.[/red]")
        else:
            console.print("[green]All critical checks passed.[/green]")

    raise typer.Exit(EXIT_ERROR if failures else EXIT_OK)


# --- submit / jobs / job --------------------------------------------------------


def _wait_for_job(client: SynapseClient, job_id: str, state: State, timeout: float | None) -> Job:
    started = time.monotonic()
    last_event_id = 0
    while True:
        job = client.get_job(job_id)
        if not state.json_output:
            for event in job.events:
                if event.event_id > last_event_id:
                    last_event_id = event.event_id
                    console.print(
                        f"  [dim]→ {event.event_type}[/dim] "
                        f"[dim]{event.status or ''} ({event.component_id})[/dim]"
                    )
        if job.status in TERMINAL_STATES:
            return job
        if timeout is not None and time.monotonic() - started > timeout:
            err_console.print(
                f"[yellow]⚠ timeout after {timeout}s — job {job_id} "
                f"still {job.status.value}[/yellow]"
            )
            return job
        time.sleep(0.5)


def _exit_for_job(job: Job) -> None:
    if job.status == JobStatus.COMPLETED:
        raise typer.Exit(EXIT_OK)
    if job.status == JobStatus.FAILED:
        raise typer.Exit(EXIT_JOB_FAILED)
    if job.status == JobStatus.CANCELLED:
        raise typer.Exit(EXIT_JOB_CANCELLED)
    raise typer.Exit(EXIT_ERROR)


@app.command()
def submit(
    ctx: typer.Context,
    task_type: str = typer.Argument(
        ..., help="Registered task type (echo, text_stats, slow, ask, ingest_url, search, research)"
    ),
    data: str = typer.Option("{}", "--data", help="JSON payload, e.g. '{\"text\": \"hi\"}'"),
    data_file: Path | None = typer.Option(
        None,
        "--data-file",
        help="Read JSON payload from a file ('-' for stdin) — shell-quoting-proof",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Stream events until the job reaches a terminal state"
    ),
    project: str | None = typer.Option(None, "--project", help="Project scope on the job"),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait"),
) -> None:
    """Create a durable job on the core and optionally wait for its result."""
    state = _state(ctx)
    settings = get_settings()
    if data_file is not None:
        try:
            if str(data_file) == "-":
                raw = sys.stdin.read()
            else:
                raw = data_file.read_text(encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]✗ Could not read --data-file: {exc}[/red]")
            raise typer.Exit(EXIT_ERROR) from None
    else:
        raw = data
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]✗ payload is not valid JSON: {exc}[/red]")
        raise typer.Exit(EXIT_ERROR) from None
    if not isinstance(payload, dict):
        err_console.print("[red]✗ --data must be a JSON object[/red]")
        raise typer.Exit(EXIT_ERROR)

    with _client_errors():
        with SynapseClient(settings) as client:
            job = client.create_job(task_type, payload=payload, project_scope=project)
            if not wait:
                def render_created() -> None:
                    console.print(
                        f"[green]✓[/green] Job [bold]{job.id}[/bold] queued — task={job.task_type}"
                    )
                    console.print("[dim]Inspect it with: synapse job inspect " + job.id + "[/dim]")

                _emit(state, job.model_dump(mode="json"), render_created)
                return

            final = _wait_for_job(client, job.id, state, timeout)

    def render_final() -> None:
        color = {
            JobStatus.COMPLETED: "green",
            JobStatus.FAILED: "red",
            JobStatus.CANCELLED: "yellow",
        }.get(final.status, "white")
        console.print(
            f"[{color}]{final.status.value.upper()}[/{color}] job [bold]{final.id}[/bold]"
        )
        if final.error:
            console.print(f"[red]error:[/red] {final.error}")
        if final.result is not None:
            console.print(json.dumps(final.result, indent=2, ensure_ascii=False, default=str))

    if state.json_output:
        typer.echo(final.model_dump_json())
    else:
        render_final()
    _exit_for_job(final)


@app.command()
def jobs(
    ctx: typer.Context,
    status: JobStatus | None = typer.Option(None, "--status", help="Filter by status"),
    project: str | None = typer.Option(None, "--project", help="Filter by project scope"),
    limit: int = typer.Option(25, "--limit", min=1, max=200),
) -> None:
    """List recent jobs from the core."""
    state = _state(ctx)
    settings = get_settings()
    with _client_errors():
        with SynapseClient(settings) as client:
            rows = client.list_jobs(status=status, project_scope=project, limit=limit)

    def render() -> None:
        if not rows:
            console.print("[dim]No jobs found.[/dim]")
            return
        table = Table(title="Jobs")
        table.add_column("ID", style="cyan")
        table.add_column("STATUS")
        table.add_column("TASK")
        table.add_column("CREATED (UTC)")
        table.add_column("SCOPE")
        style_map = {
            "completed": "green", "failed": "red", "cancelled": "yellow",
            "running": "blue", "queued": "magenta",
        }
        for row in rows:
            style = style_map.get(row.status.value, "white")
            table.add_row(
                row.id,
                f"[{style}]{row.status.value}[/{style}]",
                row.task_type,
                row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                row.project_scope or "-",
            )
        console.print(table)

    _emit(state, [row.model_dump(mode="json") for row in rows], render)


@job_app.command("inspect")
def job_inspect(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id"),
) -> None:
    """Show a job's full state and its real event trail."""
    state = _state(ctx)
    settings = get_settings()
    with _client_errors():
        with SynapseClient(settings) as client:
            job = client.get_job(job_id)

    def render() -> None:
        console.print(f"[bold]Job[/bold] {job.id}")
        console.print(f"  status:    {job.status.value}")
        console.print(f"  task:      {job.task_type}")
        console.print(f"  attempts:  {job.attempts}/{job.max_attempts}")
        console.print(f"  created:   {job.created_at.isoformat()}")
        console.print(f"  started:   {job.started_at.isoformat() if job.started_at else '-'}")
        console.print(f"  finished:  {job.finished_at.isoformat() if job.finished_at else '-'}")
        console.print(f"  scope:     {job.project_scope or '-'}")
        console.print(f"  worker:    {job.worker_id or '-'}")
        if job.error:
            console.print(f"  [red]error:[/red] {job.error}")
        if job.result is not None:
            console.print("  result:")
            console.print(json.dumps(job.result, indent=4, ensure_ascii=False, default=str))
        if job.events:
            table = Table(title="Events")
            table.add_column("ID", justify="right")
            table.add_column("TIME (UTC)")
            table.add_column("EVENT")
            table.add_column("COMPONENT")
            table.add_column("STATUS")
            for event in job.events:
                table.add_row(
                    str(event.event_id),
                    event.timestamp.strftime("%H:%M:%S"),
                    event.event_type,
                    event.component_id,
                    event.status or "-",
                )
            console.print(table)

    _emit(state, job.model_dump(mode="json"), render)


@job_app.command("cancel")
def job_cancel(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Job id"),
) -> None:
    """Cancel a queued job, or flag a running job for cancellation."""
    state = _state(ctx)
    settings = get_settings()
    with _client_errors():
        with SynapseClient(settings) as client:
            job = client.cancel_job(job_id)

    def render() -> None:
        if job.status == JobStatus.CANCELLED:
            console.print(f"[green]✓[/green] Job [bold]{job.id}[/bold] cancelled")
        else:
            console.print(
                f"[green]✓[/green] Cancellation requested for [bold]{job.id}[/bold] "
                f"(status: {job.status.value}; the worker will stop it)"
            )

    _emit(state, job.model_dump(mode="json"), render)


# --- events ---------------------------------------------------------------------


@app.command(name="events")
def events_cmd(
    ctx: typer.Context,
    since: int = typer.Option(0, "--since", help="Start after this event id"),
    follow: bool = typer.Option(False, "--follow", help="Keep streaming until interrupted"),
) -> None:
    """Poll or follow the core's real event feed."""
    state = _state(ctx)
    settings = get_settings()
    last = since
    try:
        with _client_errors():
            with SynapseClient(settings) as client:
                while True:
                    rows = client.events_since(last, limit=200)
                    for row in rows:
                        last = row.event_id
                        if state.json_output:
                            typer.echo(row.model_dump_json())
                        else:
                            console.print(
                                f"[dim]{row.timestamp.strftime('%H:%M:%S')}[/dim] "
                                f"{row.event_type:<24} [cyan]{row.job_id[:8]}[/cyan] "
                                f"{row.status or ''}"
                            )
                    if not follow:
                        break
                    if not rows:
                        time.sleep(0.5)
    except KeyboardInterrupt:
        if not state.json_output:
            console.print("[dim]Stopped following.[/dim]")


# --- token / ask / ingest / dashboard --------------------------------------------


@app.command()
def token(ctx: typer.Context) -> None:
    """Print the local access token the dashboard gate expects (ADR 0006)."""
    state = _state(ctx)
    settings = get_settings()
    value = settings.token
    if not value:
        err_console.print("[red]✗ No access token exists yet.[/red]")
        err_console.print("[dim]Run `synapse start` first — it generates one on first boot.[/dim]")
        raise typer.Exit(EXIT_ERROR)

    report = {"token": value, "token_file": str(settings.token_file)}

    def render() -> None:
        console.print(value)

    _emit(state, report, render)


def _submit_and_wait(
    state: State,
    settings: Settings,
    task_type: str,
    payload: dict[str, Any],
    *,
    timeout: float | None,
    project: str | None,
) -> Job:
    """Queue one job and stream its events to a terminal state (ask/ingest)."""
    with _client_errors():
        with SynapseClient(settings) as client:
            created = client.create_job(task_type, payload=payload, project_scope=project)
            return _wait_for_job(client, created.id, state, timeout)


@app.command()
def ask(
    ctx: typer.Context,
    prompt: str = typer.Argument(..., help="What to ask (max 32000 characters)"),
    context: str | None = typer.Option(
        None, "--context", help="Untrusted context; fenced out of the system prompt"
    ),
    model: str | None = typer.Option(None, "--model", help="Model override (routed by the core)"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", min=1, help="Output token cap"),
    project: str | None = typer.Option(None, "--project", help="Job's project scope"),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait"),
) -> None:
    """Ask the workstation a question and stream the real answer (task «ask»)."""
    state = _state(ctx)
    settings = get_settings()
    if not prompt.strip():
        # Same rule the worker enforces (tasks.ask_task) — reject locally to
        # save the round-trip.
        err_console.print("[red]✗ prompt must be a non-empty string[/red]")
        raise typer.Exit(EXIT_ERROR)
    payload: dict[str, Any] = {"prompt": prompt}
    if context is not None:
        payload["context"] = context
    if model is not None:
        payload["model"] = model
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    final = _submit_and_wait(
        state, settings, "ask", payload, timeout=timeout, project=project
    )

    def render_final() -> None:
        if final.status is not JobStatus.COMPLETED:
            # Honest failure paths: no answer, no invented partial answer.
            console.print(
                f"[{'red' if final.status is JobStatus.FAILED else 'yellow'}]"
                f"{final.status.value.upper()}"
                f"[/{'red' if final.status is JobStatus.FAILED else 'yellow'}] "
                f"job [bold]{final.id}[/bold]"
            )
        if final.error:
            console.print(f"[red]error:[/red] {final.error}")
        result = final.result or {}
        text = result.get("text")
        if text:
            console.print(text)
        if result:
            usage = result.get("usage") or {}
            cost = usage.get("cost_usd")
            cost_text = "n/a (price unknown)" if cost is None else f"${cost:.6f}"
            console.print(
                f"[dim]{result.get('provider')}/{result.get('model')} · "
                f"in {usage.get('input_tokens')} out {usage.get('output_tokens')} tok · "
                f"{usage.get('latency_ms')} ms · {cost_text}[/dim]"
            )
            sources = result.get("sources") or []
            if sources:
                # Real provenance: which stored documents grounded this answer.
                titles = ", ".join(
                    f"{(s.get('title') or s.get('source') or '?')[:40]}" for s in sources
                )
                console.print(
                    f"[dim]grounded by {len(sources)} chunk(s) "
                    f"({sources[0].get('retriever')}): {titles}[/dim]"
                )
            else:
                console.print("[dim]Not grounded — no knowledge matched this prompt.[/dim]")

    if state.json_output:
        typer.echo(final.model_dump_json())
    else:
        render_final()
    _exit_for_job(final)


@app.command()
def ingest(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="http(s) URL to fetch through the SSRF gate"),
    max_chars: int | None = typer.Option(None, "--max-chars", min=1, help="Text size cap"),
    project: str | None = typer.Option(None, "--project", help="Job's project scope"),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait"),
) -> None:
    """Fetch a URL through the SSRF gate and store its text in knowledge (task «ingest_url»)."""
    state = _state(ctx)
    settings = get_settings()
    payload: dict[str, Any] = {"url": url}
    if max_chars is not None:
        payload["max_chars"] = max_chars

    final = _submit_and_wait(
        state, settings, "ingest_url", payload, timeout=timeout, project=project
    )

    def render_final() -> None:
        if final.status is not JobStatus.COMPLETED:
            console.print(
                f"[yellow]{final.status.value.upper()}[/yellow] job [bold]{final.id}[/bold]"
            )
        if final.error:
            console.print(f"[red]error:[/red] {final.error}")
        result = final.result or {}
        title = result.get("title") or result.get("final_url") or url
        if result:
            console.print(
                f"[green]✓[/green] [bold]{title}[/bold] — {result.get('chars')} chars "
                f"[dim]({result.get('content_type')}, HTTP {result.get('status_code')})[/dim]"
            )
            knowledge = result.get("knowledge")
            if knowledge:
                if knowledge.get("deduplicated"):
                    console.print(
                        f"[dim]Already known: document "
                        f"{str(knowledge.get('document_id'))[:8]} "
                        f"(content-identical, no new chunks)[/dim]"
                    )
                else:
                    console.print(
                        f"[dim]Stored in knowledge: document "
                        f"{str(knowledge.get('document_id'))[:8]} · "
                        f"{knowledge.get('chunks')} chunks[/dim]"
                    )
            else:
                console.print(
                    "[dim]Not stored (store disabled) — full text in the job result: "
                    f"synapse job inspect {final.id}[/dim]"
                )

    if state.json_output:
        typer.echo(final.model_dump_json())
    else:
        render_final()
    _exit_for_job(final)


@app.command()
def dashboard(ctx: typer.Context) -> None:
    """Open the live dashboard in a browser, starting the core first if needed."""
    state = _state(ctx)
    settings = get_settings()
    index_file = dashboard_dist_dir() / "index.html"
    if not index_file.exists():
        err_console.print(f"[red]✗ Dashboard assets are not built ({index_file} missing)[/red]")
        err_console.print("[dim]Build them once with: npm run build (in dashboard/)[/dim]")
        raise typer.Exit(EXIT_ERROR)

    if not _health_ok(settings):
        console.print("[dim]Core is not running — starting it first…[/dim]")
        start(ctx)

    url = f"{settings.api_url}/"
    report = {"url": url, "token_hint": "run `synapse token` to read the access token"}
    opened = webbrowser.open(url)

    def render() -> None:
        console.print(f"[green]✓[/green] Dashboard live at [bold]{url}[/bold]")
        console.print("[dim]Paste the access token at the gate — read it with: synapse token[/dim]")

    _emit(state, report, render)
    if not opened:
        err_console.print("[yellow]⚠ Could not launch a browser — open the URL manually.[/yellow]")


def _render_search_result(result: dict[str, Any]) -> None:
    """Render a search result dict (direct response or a queued job's result).

    Reads only dict keys, so both paths share one renderer. Absences render as
    "n/a"/absent — never as a fabricated 0. Facts may arrive as tuples (direct)
    or lists (JSON round-trip through the job queue); both iterate the same.
    """
    total_value = result.get("total")
    total = (
        "total n/a (this API reports no total)"
        if total_value is None
        else f"total {total_value}"
    )
    items = result.get("items") or []
    console.print(
        f"[bold]{result.get('connector')}[/bold] ({result.get('domain')}) — "
        f"{len(items)} shown · {total}"
    )
    requested = result.get("requested")
    page_size = result.get("page_size")
    if requested is not None and page_size is not None and requested > page_size:
        console.print(
            f"[yellow]·[/yellow] --limit {requested} trimmed to "
            f"{page_size} (connector page cap)"
        )
    rate = result.get("rate")
    if rate is None:
        console.print("[dim]· rate limit: not reported by this API[/dim]")
    else:
        remaining = "n/a" if rate.get("remaining") is None else str(rate["remaining"])
        ceiling = "n/a" if rate.get("limit") is None else str(rate["limit"])
        console.print(
            f"[dim]· rate limit: {remaining}/{ceiling} remaining "
            f"(reported via {rate.get('source')})[/dim]"
        )
    for index, item in enumerate(items, start=1):
        console.print(f"[cyan]{index}.[/cyan] {escape(str(item.get('title') or ''))}")
        meta = " · ".join(part for part in (item.get("date"), item.get("url")) if part)
        if meta:
            console.print(f"   [dim]{escape(meta)}[/dim]")
        if item.get("snippet"):
            console.print(f"   {escape(item['snippet'])}")
        facts = item.get("facts") or ()
        if facts:
            rendered = " · ".join(f"{key}={escape(value)}" for key, value in facts)
            console.print(f"   [dim]{rendered}[/dim]")
    if not items:
        console.print("[dim]No results matched — an empty result, not an error.[/dim]")


@app.command()
def search(
    ctx: typer.Context,
    connector: str | None = typer.Argument(
        None, help="Connector name — omit it to list every connector"
    ),
    query: str = typer.Argument(
        "", help="Search text (usgs takes key=value filters; frankfurter takes a currency code)"
    ),
    limit: int = typer.Option(10, "--limit", min=1, max=500, help="Max items to show"),
    queue: bool = typer.Option(
        False, "--queue", help="Queue a durable search job instead of calling the provider directly"
    ),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait for the job"),
) -> None:
    """Search a free public API: coding, knowledge, medical, earth, weather, finance.

    Runs directly against the verified provider through the SSRF gate — no core
    required. With --queue it runs as a durable job on the core instead. Failures
    print the real reason (rate_limited, blocked, …), never an empty-looking
    success.
    """
    state = _state(ctx)
    if connector is None and not queue:
        def render_catalog() -> None:
            table = Table(title="Connectors — free public APIs")
            table.add_column("NAME", style="cyan")
            table.add_column("DOMAIN")
            table.add_column("SOURCE")
            for entry in connector_catalog():
                table.add_row(entry["name"], entry["domain"], entry["description"])
            console.print(table)

        _emit(state, connector_catalog(), render_catalog)
        return

    settings = get_settings()
    if queue:
        # Same rule the worker enforces (tasks.search_task) — reject locally
        # to save the round-trip. There is no "search everything" job.
        if not connector or not query.strip():
            err_console.print(
                "[red]✗ --queue needs both a CONNECTOR and a QUERY argument[/red]"
            )
            raise typer.Exit(EXIT_ERROR)
        final = _submit_and_wait(
            state,
            settings,
            "search",
            {"connector": connector, "query": query, "limit": limit},
            timeout=timeout,
            project=None,
        )

        def render_queued() -> None:
            if final.status is not JobStatus.COMPLETED:
                # Honest failure paths: no results, no invented partial answer.
                color = "red" if final.status is JobStatus.FAILED else "yellow"
                console.print(
                    f"[{color}]{final.status.value.upper()}[/{color}] "
                    f"job [bold]{final.id}[/bold]"
                )
            if final.error:
                console.print(f"[red]error:[/red] {final.error}")
            if final.result:
                _render_search_result(final.result)

        if state.json_output:
            typer.echo(final.model_dump_json())
        else:
            render_queued()
        _exit_for_job(final)
        return

    try:
        with build_connector(connector, settings) as active:
            response = active.search(query, limit=limit)
    except ConnectorError as exc:
        err_console.print(f"[red]✗ {exc.reason}[/red]: {exc.detail}")
        raise typer.Exit(EXIT_ERROR) from None

    def render() -> None:
        _render_search_result(asdict(response))

    _emit(state, asdict(response), render)


def _render_research_result(result: dict[str, Any]) -> None:
    """Render a research job's result — the answer first, then real provenance.

    Reads only dict keys (the job result arrives JSON-round-tripped). Every
    value below came from the pipeline: sources are what actually entered the
    model's prompt, and a "total n/a" is a provider that reports no count.
    """
    answer = result.get("answer")
    if answer:
        console.print(answer)

    sources = result.get("sources") or []
    if sources:
        console.print("\n[bold]Sources[/bold]")
        for source in sources:
            index = source.get("index", "?")
            title = source.get("title") or source.get("url") or "untitled"
            console.print(f" [cyan][{index}][/cyan] {escape(str(title))}")
            if source.get("url"):
                console.print(f"     [dim]{escape(str(source['url']))}[/dim]")
            bits = [f"via {source.get('connector')}"]
            if source.get("via"):
                bits.append(str(source["via"]))
            if source.get("chars") is not None:
                bits.append(f"{source['chars']} chars")
            if source.get("status_code") is not None:
                bits.append(f"HTTP {source['status_code']}")
            console.print(f"     [dim]{escape(' · '.join(bits))}[/dim]")

    searches = result.get("searches") or []
    if searches:
        console.print("\n[bold]Searches[/bold]")
        for entry in searches:
            name = entry.get("connector")
            error = entry.get("error")
            if error:
                console.print(
                    f" [red]✗[/red] {name} — {error.get('reason')}: {error.get('detail')}"
                )
            else:
                total = entry.get("total")
                total_text = "total n/a" if total is None else f"total {total:,}"
                console.print(
                    f" [green]✓[/green] {name} — {entry.get('fetched')} result(s), {total_text}"
                )

    skipped = result.get("fetch_skipped") or []
    if skipped:
        console.print("\n[yellow]Not fetched[/yellow]")
        for entry in skipped:
            console.print(
                f" [dim]{escape(str(entry.get('url')))} — "
                f"{escape(str(entry.get('reason')))}[/dim]"
            )

    usage = result.get("usage") or {}
    cost = usage.get("cost_usd")
    cost_text = "n/a (price unknown)" if cost is None else f"${cost:.6f}"
    console.print(
        f"\n[dim]{result.get('provider')}/{result.get('model')} · "
        f"in {usage.get('input_tokens')} out {usage.get('output_tokens')} tok · "
        f"{usage.get('latency_ms')} ms · {cost_text}[/dim]"
    )


@app.command()
def research(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="The research question"),
    connectors: str | None = typer.Option(
        None,
        "--connectors",
        help=(
            "Comma-separated connector names "
            f"(default: {', '.join(DEFAULT_RESEARCH_CONNECTORS)})"
        ),
    ),
    max_sources: int = typer.Option(
        RESEARCH_DEFAULT_SOURCES,
        "--max-sources",
        min=1,
        max=RESEARCH_MAX_SOURCES,
        help="How many sources to fetch as evidence",
    ),
    model: str | None = typer.Option(None, "--model", help="Model override (routed by the core)"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", min=1, help="Output token cap"),
    project: str | None = typer.Option(None, "--project", help="Job's project scope"),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait"),
) -> None:
    """Research a question: search free APIs, fetch sources, cite an answer (task «research»)."""
    state = _state(ctx)
    settings = get_settings()

    cleaned = question.strip()
    if not cleaned:
        # Same rule the worker enforces (tasks.research_task) — reject locally.
        err_console.print("[red]✗ question must be a non-empty string[/red]")
        raise typer.Exit(EXIT_ERROR)
    if len(cleaned) > RESEARCH_MAX_QUESTION_CHARS:
        err_console.print(
            f"[red]✗ question exceeds {RESEARCH_MAX_QUESTION_CHARS} characters "
            f"(got {len(cleaned)})[/red]"
        )
        raise typer.Exit(EXIT_ERROR)

    payload: dict[str, Any] = {"question": cleaned, "max_sources": max_sources}
    if connectors is not None:
        names = [part.strip().lower() for part in connectors.split(",") if part.strip()]
        if not names:
            err_console.print("[red]✗ --connectors needs at least one name[/red]")
            raise typer.Exit(EXIT_ERROR)
        unknown = [name for name in names if name not in connector_names()]
        if unknown:
            err_console.print(f"[red]✗ unknown connector(s): {', '.join(unknown)}[/red]")
            err_console.print(f"available: {', '.join(connector_names())}")
            raise typer.Exit(EXIT_ERROR)
        payload["connectors"] = names
    if model is not None:
        payload["model"] = model
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    final = _submit_and_wait(
        state, settings, "research", payload, timeout=timeout, project=project
    )

    def render() -> None:
        if final.status is not JobStatus.COMPLETED:
            # Honest failure paths: no answer, no invented partial answer.
            color = "red" if final.status is JobStatus.FAILED else "yellow"
            console.print(
                f"[{color}]{final.status.value.upper()}[/{color}] job [bold]{final.id}[/bold]"
            )
        if final.error:
            console.print(f"[red]error:[/red] {final.error}")
        if final.result:
            _render_research_result(final.result)

    if state.json_output:
        typer.echo(final.model_dump_json())
    else:
        render()
    _exit_for_job(final)


def _mcp_client_config() -> dict[str, Any]:
    """The JSON an MCP host needs to launch ``synapse mcp`` as a stdio server.

    The token is never printed (spec 5): the launched server reads it from the
    same state file every other local client uses.
    """
    return {
        "mcpServers": {
            "synapse": {
                "command": "synapse",
                "args": ["mcp"],
            }
        }
    }


@app.command()
def mcp(
    ctx: typer.Context,
    print_config: bool = typer.Option(
        False,
        "--print-config",
        help="Print MCP host configuration JSON (for Claude/Cursor/etc.) and exit",
    ),
) -> None:
    """Serve the core to MCP clients over stdio — every MCP host becomes a thin client."""
    state = _state(ctx)
    settings = get_settings()

    if print_config:
        if state.json_output:
            typer.echo(json.dumps(_mcp_client_config()))
        else:
            console.print_json(json.dumps(_mcp_client_config()))
            console.print(
                "\n[dim]Add this to your MCP host's config. The server reads the "
                "local token automatically — it is never printed.[/dim]"
            )
        return

    # Stdio transport: stdout belongs to the protocol, so the console goes to
    # stderr. A missing token is caught before the first tool call, with the
    # real reason (the API would answer 401 anyway — fail fast, fail honest).
    if not settings.token:
        err_console.print(
            "[red]✗ no client token found — run `synapse start` once to create it[/red]"
        )
        raise typer.Exit(EXIT_NOT_RUNNING)

    from synapse.mcp import run_mcp_server

    raise typer.Exit(run_mcp_server(settings))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
