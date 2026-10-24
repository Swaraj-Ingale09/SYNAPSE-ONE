"""Runtime configuration for SYNAPSE ONE.

All values come from environment variables (optionally loaded from a `.env`
file at the repository root). Secrets are never hard-coded; see `.env.example`
for placeholder names only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_HOST = "127.0.0.1"  # spec 15.1: loopback by default
DEFAULT_API_PORT = 8765


def synapse_home() -> Path:
    """Directory for runtime state: token file, pid files, logs."""
    override = os.environ.get("SYNAPSE_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "synapse"


@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    database_url: str
    database_password_set: bool
    token: str | None
    state_dir: Path
    worker_poll_interval: float = 0.5
    event_poll_interval: float = 0.5
    job_lease_seconds: int = 30
    job_max_attempts: int = 3
    max_payload_bytes: int = 256 * 1024

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "token"

    @property
    def worker_stop_file(self) -> Path:
        return self.state_dir / "worker.stop"

    @property
    def api_pid_file(self) -> Path:
        return self.state_dir / "api.pid"

    @property
    def worker_pid_file(self) -> Path:
        return self.state_dir / "worker.pid"


def get_settings() -> Settings:
    """Build settings from the current environment (read fresh, never cached)."""
    home = synapse_home()
    api_host = os.environ.get("SYNAPSE_API_HOST", DEFAULT_API_HOST)
    api_port = int(os.environ.get("SYNAPSE_API_PORT", str(DEFAULT_API_PORT)))

    password = os.environ.get("DB_PASSWORD", "")
    url_override = (os.environ.get("SYNAPSE_DATABASE_URL") or "").strip()
    if url_override:
        database_url = url_override
    else:
        user = os.environ.get("DB_USER", "postgres")
        host = os.environ.get("DB_HOST", "localhost")
        port = os.environ.get("DB_PORT", "5432")
        dbname = os.environ.get("DB_NAME", "synapseone")
        database_url = (
            f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{dbname}"
        )

    token = (os.environ.get("SYNAPSE_TOKEN") or "").strip() or None
    if token is None:
        token_path = home / "token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip() or None

    return Settings(
        api_host=api_host,
        api_port=api_port,
        database_url=database_url,
        database_password_set=bool(password),
        token=token,
        state_dir=home,
    )
