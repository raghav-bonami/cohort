from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.routers import documents, health, users
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_middleware
from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)

    app.state.mongo = create_mongo_client(settings.mongo_url)
    app.state.db = app.state.mongo[settings.mongo_db]
    app.state.redis = create_redis_client(settings.redis_url)

    # Fail fast if Mongo is unreachable: it is the source of truth and enforces
    # client_doc_ref uniqueness. Redis is not checked: the app runs degraded without it
    await ensure_indexes(app.state.db)

    log.info("startup", app=settings.app_name)

    yield

    await app.state.redis.aclose()
    await app.state.mongo.close()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory.
    Settings are attached to the app instead of being read globally, so tests can
    build an app pointed at a test database without touching environment variables.
    """
    app = FastAPI(title="Cohort Insights API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or get_settings()

    register_error_handlers(app)
    app.middleware("http")(request_id_middleware)

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(users.router)
    return app


app = create_app()
