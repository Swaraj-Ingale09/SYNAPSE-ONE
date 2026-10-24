"""Database access helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=8)
def _engine_for(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


def get_engine(url: str) -> Engine:
    return _engine_for(url)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    factory = sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(url: str) -> tuple[bool, str]:
    """Return (reachable, detail) without leaking credentials."""
    try:
        with get_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "connected"
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash readiness
        name = type(exc).__name__
        return False, f"{name}: connection failed"
