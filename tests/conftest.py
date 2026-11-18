"""Shared fixtures. Integration tests use a separate database, never 'cohort'."""

import os

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
TEST_DB = "cohort_test"


@pytest.fixture
async def db():
    """A clean database with real indexes, dropped after each test."""
    client = create_mongo_client(MONGO_URL)
    try:
        await client.admin.command("ping")
    except ServerSelectionTimeoutError:
        await client.close()
        # Skip loudly rather than fail confusingly: pytest reports skips.
        pytest.skip(f"MongoDB not reachable at {MONGO_URL}")

    await client.drop_database(TEST_DB)
    database = client[TEST_DB]
    await ensure_indexes(database)

    yield database

    await client.drop_database(TEST_DB)
    await client.close()
