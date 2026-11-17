"""Shape of a document in MongoDB, and the one rule readers use to judge freshness."""

from datetime import datetime
from typing import Any

from bson import ObjectId

from app.domain.enums import DocumentStatus, StageName, StageState
from app.services.hashing import content_hash


def new_stage() -> dict[str, Any]:
    """Fresh per-stage tracking block. Used at creation and again on every PATCH."""
    return {
        "state": StageState.PENDING.value,
        "attempts": 0,        # incremented each time a worker starts this stage
        "last_error": None,   # message from the most recent failed attempt
    }


def new_document(
    *,
    user_id: str,
    title: str,
    content: str,
    client_doc_ref: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Build the full document for insert_one().

    The _id is generated here, before the insert, so the rate limiter
    can reserve a slot for this exact id first, and release it if the insert fails.
    """
    doc: dict[str, Any] = {
        "_id": ObjectId(),
        "user_id": user_id,
        "title": title,

        # Content and its identity. content_version is THE number every derived
        # field is compared against (see is_result_current).
        "content": content,
        "content_hash": content_hash(content),
        "content_version": 1,

        # Overall position (indexed) plus per-stage detail.
        "status": DocumentStatus.QUEUED.value,
        "failed_stage": None,  # None, or a StageName value once status == failed
        "stages": {
            StageName.PROCESSING.value: new_stage(),
            StageName.ENRICHING.value: new_stage(),
        },

        # Internal hand-off from stage 1 to stage 2, labelled with the version it
        # came from. Kept when enriching fails, so processing is not redone.
        "stage1_output": None,  # {"content_version": int, "summary": str}

        # Published output. Summary and tags are only ever written together, in
        # one $set, with one content_version label, so they cannot disagree.
        "result": None,  # {"content_version", "content_hash", "summary", "tags", "completed_at"}

        # Scheduling: the earliest moment any worker may pick this document up.
        # While a worker holds it, this is the lease expiry; while waiting to
        # retry, it is the backoff time; None once completed or failed.
        "next_attempt_at": now,
        # The id of the worker run that currently owns the document. Every worker
        # write includes it in its filter (fencing), so a run that lost ownership
        # cannot write.
        "lease_id": None,

        "created_at": now,
        "updated_at": now,
    }

    # Leave the field out (rather than storing None) when there is no ref: the
    # unique partial index only covers documents where the field exists, so any
    # number of documents can have no ref.
    if client_doc_ref is not None:
        doc["client_doc_ref"] = client_doc_ref

    return doc


def is_result_current(doc: dict[str, Any]) -> bool:
    """True only if the stored result was produced from the current content.

    A comparison of two stored numbers, not a stored boolean: PATCH bumps
    content_version, and the old result keeps its old label, so it reads
    as stale automatically, with nothing extra to keep in sync.
    """
    result = doc.get("result")
    return result is not None and result["content_version"] == doc["content_version"]
