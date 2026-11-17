from pymongo import AsyncMongoClient

# Single place for the collection name, so repositories, indexes and tests agree.
DOCUMENTS = "documents"

def create_mongo_client(url: str) -> AsyncMongoClient:
    # Short server-selection timeout: fail fast instead of hanging when Mongo is down.
    # tz_aware: datetimes come back timezone-aware, so comparisons with
    # datetime.now(UTC) work (needed for next_attempt_at in the worker)
    return AsyncMongoClient(url, serverSelectionTimeoutMS=2000, tz_aware=True)
