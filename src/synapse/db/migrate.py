"""Programmatic Alembic migrations.

Revision scripts live inside the package (db/migrations) so that migrations
are available whether the package is installed editable or as a wheel.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation: literal percent signs must be doubled.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_head(database_url: str) -> None:
    """Apply all pending migrations. Idempotent."""
    command.upgrade(alembic_config(database_url), "head")
