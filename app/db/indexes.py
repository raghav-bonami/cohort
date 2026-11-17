"""Index definitions. Each one exists for a named query."""

import structlog
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.db.mongo import DOCUMENTS

log = structlog.get_logger()

DOCUMENT_INDEXES: list[IndexModel] = [
    # GET /users/{id}/documents without a status filter: equality on user_id,
    # then already in "newest first" order. _id breaks ties between documents
    # created in the same millisecond, so page boundaries are stable.
    IndexModel(
        [("user_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="user_created",
    ),
    # Same listing WITH ?status=..., and the rate-limit fallback
    # count_documents({user_id, status: {$in: active}}) when Redis is down.
    IndexModel(
        [
            ("user_id", ASCENDING),
            ("status", ASCENDING),
            ("created_at", DESCENDING),
            ("_id", DESCENDING),
        ],
        name="user_status_created",
    ),
    # Crosswalk: by-ref lookup, and uniqueness of client_doc_ref when present.
    # Partial: only documents that HAVE the field are indexed, so documents
    # without a ref never collide with each other.
    IndexModel(
        [("client_doc_ref", ASCENDING)],
        name="client_doc_ref_unique",
        unique=True,
        partialFilterExpression={"client_doc_ref": {"$exists": True}},
    ),
    # Content cache fallback when Redis is down: "has any document already
    # produced a result for this exact content?"
    IndexModel([("result.content_hash", ASCENDING)], name="result_content_hash"),
    # Worker claim query (Phase 4): status in active AND next_attempt_at <= now,
    # oldest first.
    IndexModel(
        [("status", ASCENDING), ("next_attempt_at", ASCENDING)],
        name="claim_queue",
    ),
]


async def ensure_indexes(db: AsyncDatabase) -> None:
    """Create all indexes. Safe to run on every startup.

    MongoDB treats an identical existing index as a no-op. If a definition is
    changed but keeps the same name, Mongo raises IndexOptionsConflict, which is
    deliberate: a silent mismatch between code and database would be worse.
    """
    names = await db[DOCUMENTS].create_indexes(DOCUMENT_INDEXES)
    log.info("indexes_ensured", collection=DOCUMENTS, indexes=names)
