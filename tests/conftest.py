"""Shared fixtures. Integration tests use their own database, never 'cohort'."""

import os

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pymongo.errors import ServerSelectionTimeoutError

from app.core.config import Settings
from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client
from app.main import create_app

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TEST_DB = "cohort_test"


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
async def db(mongo_client):
    """The test database with real indexes. Used to seed or inspect data directly."""
    database = mongo_client[TEST_DB]
    await ensure_indexes(database)
    return database


@pytest.fixture
async def client(mongo_client):
    """HTTP client for the real app, with startup/shutdown (lifespan) actually run.

    httpx's ASGITransport does not run lifespan on its own; LifespanManager does,
    so connections and indexes are set up exactly as in production.
    """
    settings = Settings(mongo_url=MONGO_URL, mongo_db=TEST_DB, redis_url=REDIS_URL)
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http