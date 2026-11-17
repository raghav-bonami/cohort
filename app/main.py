from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.routers import health
from app.core.config import get_settings
from app.core.logging import configure_logging, request_id_middleware

from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client
from app.db.indexes import ensure_indexes

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    app.state.mongo = create_mongo_client(settings.mongo_url)
    app.state.db = app.state.mongo[settings.mongo_db]
    app.state.redis = create_redis_client(settings.redis_url)

    # Fail fast if Mongo is unreachable at startup: it is the source of truth,
    # and running without its indexes would mean silent collection scans and
    # no uniqueness on client_doc_ref. Compose only starts the api once Mongo
    # is healthy, so this only fails when something is genuinely wrong.
    # Redis is NOT checked here: the app is designed to run degraded without it.
    await ensure_indexes(app.state.db)

    log.info("startup", app=settings.app_name)

    yield

    await app.state.redis.aclose()
    await app.state.mongo.close()
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="Cohort Insights API", lifespan=lifespan)
    app.middleware("http")(request_id_middleware)
    app.include_router(health.router)
    return app


app = create_app()
