"""Shared test fixtures: a real PostgreSQL test database (never SQLite).

Per project policy tests run against the actual engine so that JSONB,
row locking, SKIP LOCKED, and migration behaviour are genuinely exercised.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

import pytest
from sqlalchemy import create_engine, text

from synapse.config import Settings
from synapse.db import session_scope
from synapse.db.migrate import upgrade_head

TEST_DB_NAME = "synapseone_test"


def _base_url() -> str:
    password = os.environ.get("DB_PASSWORD", "")
    user = os.environ.get("DB_USER", "postgres")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Create (once) and migrate the dedicated test database."""
    if not os.environ.get("DB_PASSWORD"):
        pytest.fail("DB_PASSWORD is not set — copy .env.example to .env before running tests")
    admin = create_engine(_base_url() + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin.dispose()
    url = _base_url() + "/" + TEST_DB_NAME
    upgrade_head(url)
    return url


@pytest.fixture(autouse=True)
def _clean_job_tables(test_db_url: str):
    """Isolate tests: purge durable state after every test (sequences keep counting).

    ``model_usage`` is included because its FK is ``SET NULL``: deleting jobs
    alone would leave orphaned ledger rows that leak into the next test's
    "no calls recorded" assertions.
    """
    yield
    with session_scope(test_db_url) as session:
        session.execute(text("DELETE FROM model_usage"))
        session.execute(text("DELETE FROM job_events"))
        session.execute(text("DELETE FROM jobs"))


@pytest.fixture
def settings(test_db_url: str, tmp_path, monkeypatch) -> Settings:
    state = tmp_path / "state"
    monkeypatch.setenv("SYNAPSE_HOME", str(state))
    return Settings(
        api_host="127.0.0.1",
        api_port=8765,
        database_url=test_db_url,
        database_password_set=True,
        token="test-token-value",
        state_dir=state,
    )


@pytest.fixture
def _clean_knowledge_tables(settings):
    """Purge knowledge state around a DB-backed test (shared by several modules).

    ``_clean_job_tables`` only cleans the job tables; documents are
    content-addressed, so a leftover row would make a later "should create"
    assertion report a spurious dedup. Cleanup runs *before* the test too, so a
    failed prior test cannot cascade into confusing failures here.

    Uses ``settings.database_url`` (the dedicated test DB) rather than
    ``get_settings()``, which would read ``.env`` and wipe the *dev* database.

    Deliberately not autouse: pure-function tests (chunking, hashing) must not
    require a live database just to run.
    """
    from sqlalchemy import text

    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        session.execute(text("DELETE FROM chunks"))
        session.execute(text("DELETE FROM documents"))
    yield
    with session_scope(settings.database_url) as session:
        session.execute(text("DELETE FROM chunks"))
        session.execute(text("DELETE FROM documents"))


@pytest.fixture
def app(settings: Settings):
    from synapse.api.app import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.token}"}
