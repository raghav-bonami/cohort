"""Shared fixtures. Integration tests use their own Mongo database and Redis DB,
never the ones the dev stack uses."""

import contextlib
import os

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pymongo.errors import ServerSelectionTimeoutError
from redis.exceptions import RedisError

from app.core.config import Settings
from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client
from app.main import create_app
from app.repositories.document_repo import DocumentRepository
from app.services.rate_limiter import ActiveDocumentLimiter
from app.services.result_cache import ResultCache
from app.worker.pipeline import Worker
from app.worker.stages import SimulatedStages

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")  # not 0: dev data
TEST_DB = "cohort_test"

# Pipeline settings for tests: instant stages, never fails.
FAST_PIPELINE = {
    "processing_min_seconds": 0,
    "processing_max_seconds": 0,
    "enriching_min_seconds": 0,
    "enriching_max_seconds": 0,
    "stage_failure_rate": 0.0,
}


@pytest.fixture
async def mongo_client():
    """A Mongo client with a clean test database; skips the test if Mongo is down."""
    client = create_mongo_client(MONGO_URL)
    try:
        await client.admin.command("ping")
    except ServerSelectionTimeoutError:
        await client.close()
        pytest.skip(f"MongoDB not reachable at {MONGO_URL}")  # reported, not silent

    await client.drop_database(TEST_DB)
    yield client
    await client.drop_database(TEST_DB)
    await client.close()


@pytest.fixture
async def redis_client():
    """A clean Redis test DB; skips the test if Redis is down."""
    client = create_redis_client(REDIS_TEST_URL)
    try:
        await client.ping()
    except RedisError:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {REDIS_TEST_URL}")  # reported, not silent

    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def db(mongo_client):
    """The test database with real indexes. Used to seed or inspect data directly."""
    database = mongo_client[TEST_DB]
    await ensure_indexes(database)
    return database


@pytest.fixture
def make_client(mongo_client, redis_client):
    """Factory: an HTTP client for the real app with chosen settings overrides.

    httpx's ASGITransport does not run lifespan on its own; LifespanManager does,
    so connections and indexes are set up exactly as in production.
    """

    @contextlib.asynccontextmanager
    async def _make(**overrides):
        params = {"mongo_url": MONGO_URL, "mongo_db": TEST_DB, "redis_url": REDIS_TEST_URL}
        app = create_app(Settings(**{**params, **overrides}))
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                yield http

    return _make


@pytest.fixture
async def client(make_client):
    """Default client. A high limit keeps the rate limiter out of tests about other rules;
    the rate-limit tests build their own client with the real limit."""
    async with make_client(max_active_per_user=1000) as http:
        yield http


@pytest.fixture
def worker(db, redis_client):
    """A real worker (instant, never failing) wired to the same Mongo and Redis as the API."""
    settings = Settings(**FAST_PIPELINE)
    repo = DocumentRepository(db)
    return Worker(
        repo,
        SimulatedStages(settings),
        settings,
        limiter=ActiveDocumentLimiter.from_settings(redis_client, repo, settings),
        cache=ResultCache.from_settings(redis_client, repo, settings),
    )
