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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
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
from synapse.config import Settings, get_settings
from synapse.contracts import TERMINAL_STATES, Job, JobStatus
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
    task_type: str = typer.Argument(..., help="Registered task type (echo, text_stats, slow)"),
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
