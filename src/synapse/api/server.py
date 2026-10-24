"""API process entry point: ``python -m synapse.api.server``.

The CLI launches this as a detached background process and reads its log.
"""

from __future__ import annotations

import logging

import uvicorn

from synapse.api.app import create_app
from synapse.config import get_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    # Lets POST /v1/admin/shutdown request a clean uvicorn exit.
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
