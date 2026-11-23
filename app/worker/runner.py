"""Worker process entry point:  python -m app.worker.runner

Runs `worker_concurrency` claim loops in one process. Scale further with more
processes (docker compose up --scale worker=3). Every loop in every process uses the
same atomic claim, so adding workers never causes double processing.
"""

import asyncio
import signal
import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client
from app.repositories.document_repo import DocumentRepository
from app.worker.pipeline import Worker
from app.worker.stages import SimulatedStages

log = structlog.get_logger()

def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM (docker stop) and SIGINT (Ctrl+C) ask the loops to finish and exit."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops don't support this. Containers run on Linux, where
            # it works; locally on Windows, Ctrl+C still stops the process.
            log.info("signal_handler_unavailable", signal=sig.name)



async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("worker_started")

    client = create_mongo_client(settings.mongo_url)
    db = client[settings.mongo_db]
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        # The claim query needs the claim_queue index; creating it is idempotent.
        await ensure_indexes(db)
        worker = Worker(DocumentRepository(db), SimulatedStages(settings), settings)
        log.info("worker_started", concurrency=settings.worker_concurrency)

        # TaskGroup: if one loop crashes on a bug, the others are cancelled and the
        # process exits non-zero; Docker restarts it. Failing loudly beats a
        # half-alive worker. Any job it held is re-claimed when its lease expires.
        async with asyncio.TaskGroup() as tasks:
            for slot in range(settings.worker_concurrency):
                tasks.create_task(worker.run_forever(stop), name=f"worker-slot-{slot}")
    finally:
        await client.close()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
