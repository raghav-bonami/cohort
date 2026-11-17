import asyncio

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging

log = structlog.get_logger()


async def run() -> None:
    configure_logging(get_settings().log_level)
    log.info("worker_started")
    while True:
        await asyncio.sleep(30)
        log.info("worker_idle")


if __name__ == "__main__":
    asyncio.run(run())
