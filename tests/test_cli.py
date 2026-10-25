"""CLI behaviour tests: help, exit codes, JSON mode, honest failure paths (spec 10.1, 16.3)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from synapse.cli.main import EXIT_NOT_RUNNING, app

runner = CliRunner()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch) -> None:
    """Point the CLI at an empty state home and a closed port (no core running)."""
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SYNAPSE_API_PORT", "59871")
    monkeypatch.delenv("SYNAPSE_TOKEN", raising=False)


def test_help_lists_real_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("start", "stop", "status", "doctor", "submit", "jobs", "events",
                    "token", "ask", "ingest", "dashboard"):
        assert command in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_status_without_core_exits_not_running(isolated_env) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == EXIT_NOT_RUNNING
    assert "not running" in result.output


def test_status_json_without_core(isolated_env) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == EXIT_NOT_RUNNING


def test_job_inspect_without_core_exits_not_running(isolated_env) -> None:
    result = runner.invoke(app, ["job", "inspect", "some-id"])
    assert result.exit_code == EXIT_NOT_RUNNING
    assert "synapse start" in result.output


def test_submit_rejects_invalid_json_before_contacting_core(isolated_env) -> None:
    result = runner.invoke(app, ["submit", "echo", "--data", "{not json"])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_submit_rejects_non_object_payload(isolated_env) -> None:
    result = runner.invoke(app, ["submit", "echo", "--data", "[1, 2]"])
    assert result.exit_code == 1
    assert "JSON object" in result.output


def test_job_group_help() -> None:
    result = runner.invoke(app, ["job", "--help"])
    assert result.exit_code == 0
    assert "inspect" in result.output
    assert "cancel" in result.output


# --- token / dashboard / ask (ADR 0006) -------------------------------------------


def test_token_prints_the_real_token(tmp_path, monkeypatch) -> None:
    """``token`` reads the same file ``get_settings`` hands the API client."""
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SYNAPSE_API_PORT", "59871")
    monkeypatch.delenv("SYNAPSE_TOKEN", raising=False)
    token_file = tmp_path / "state" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("tok-local-123", encoding="utf-8")

    result = runner.invoke(app, ["token"])

    assert result.exit_code == 0
    assert "tok-local-123" in result.output


def test_token_without_token_file_is_an_honest_failure(isolated_env) -> None:
    """Before the first `synapse start` no token exists — say so, don't invent one."""
    result = runner.invoke(app, ["token"])

    assert result.exit_code == 1
    assert "synapse start" in result.output


def test_dashboard_missing_build_exits_with_the_build_hint(
    isolated_env, tmp_path, monkeypatch
) -> None:
    """A missing dashboard build must produce a clear instruction, not a blank UI."""
    from synapse.cli import main as cli_main

    monkeypatch.setattr(cli_main, "dashboard_dist_dir", lambda: tmp_path / "no-dist")
    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 1
    assert "npm run build" in result.output


def test_ask_rejects_empty_prompt_before_contacting_core(isolated_env) -> None:
    """Same rule the worker enforces — reject locally, with no core needed."""
    result = runner.invoke(app, ["ask", "   "])

    assert result.exit_code == 1
    assert "non-empty" in result.output
