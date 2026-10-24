"""Security helpers: secret redaction and token persistence (spec 15.2)."""

from __future__ import annotations

import pytest

from synapse.config import Settings
from synapse.security import ensure_token, redact


def test_redact_removes_bearer_tokens() -> None:
    out = redact("request failed: Authorization: Bearer abc123secret")
    assert "abc123secret" not in out
    assert "<redacted>" in out


def test_redact_removes_password_assignments() -> None:
    out = redact("connecting with password=hunter2 now")
    assert "hunter2" not in out


def test_redact_removes_provider_key_shapes() -> None:
    out = redact("key is sk-abcdefghijklmnop and ghp_abcdefghij0123456789")
    assert "sk-abcdefghij" not in out
    assert "ghp_abcdefghij" not in out


def test_redact_removes_url_credentials() -> None:
    out = redact("dsn postgresql+psycopg2://postgres:s3cr3t@localhost:5432/db")
    assert "s3cr3t" not in out


def test_redact_removes_configured_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "verysecretvalue")
    out = redact("auth failed for password verysecretvalue")
    assert "verysecretvalue" not in out
    assert "<redacted:DB_PASSWORD>" in out


def test_redact_passes_through_clean_text() -> None:
    assert redact("job completed in 42ms") == "job completed in 42ms"
    assert redact(None) is None


def test_ensure_token_generates_once_and_persists(tmp_path) -> None:
    settings = Settings(
        api_host="127.0.0.1",
        api_port=8765,
        database_url="postgresql+psycopg2://x:x@localhost/x",
        database_password_set=True,
        token=None,
        state_dir=tmp_path / "state",
    )
    first = ensure_token(settings)
    assert settings.token_file.exists()
    assert first == ensure_token(settings)  # stable across calls
    assert len(first) >= 32


def test_ensure_token_respects_preconfigured_token(tmp_path) -> None:
    settings = Settings(
        api_host="127.0.0.1",
        api_port=8765,
        database_url="postgresql+psycopg2://x:x@localhost/x",
        database_password_set=True,
        token="pinned-token",
        state_dir=tmp_path / "state",
    )
    assert ensure_token(settings) == "pinned-token"
    assert not settings.token_file.exists()  # nothing written when pinned
