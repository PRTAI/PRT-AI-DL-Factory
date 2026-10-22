"""Run one process; concurrency is controlled by BRIDGE_MAX_CONCURRENCY."""

import uvicorn

from .app import create_app
from .settings import Settings


def main():
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
