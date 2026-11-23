"""Every MongoDB query on the documents collection lives here.

Routers and services never touch Mongo directly. Two
benefits: each query sits next to the index it relies on, and the ownership
rule (filter by user_id) is enforced in one place.

Will covers the API's reads and writes. Later add their own methods
"""

from typing import Any

from dataclasses import dataclass
from datetime import datetime

from bson import ObjectId

from pymongo import DESCENDING , ASCENDING , ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db.ids import parse_object_id
from app.db.mongo import DOCUMENTS
from app.domain.enums import ACTIVE_STATUSES, DocumentStatus, StageName, StageState

# "Newest first", with _id as tie-breaker. Matches both listing indexes exactly,
# so Mongo reads the index in order and never sorts in memory.
NEWEST_FIRST = [("created_at", DESCENDING), ("_id", DESCENDING)]

# Status values a worker may pick up (same set that counts towards the rate limit).
_ACTIVE_VALUES = [s.value for s in ACTIVE_STATUSES]

@dataclass(frozen=True)
class Fence:
    """One worker's claim on one version of one document.

    Every worker write filters on all three fields. If any of them changed since the
    claim - a PATCH bumped content_version, or the lease expired and another worker
    re-claimed (new lease_id) - the write matches 0 documents and is discarded.
    That is what stops an old run from ever publishing over newer content.
    """

    document_id: ObjectId
    lease_id: str
    content_version: int

    def as_filter(self) -> dict[str, Any]:
        return {
            "_id": self.document_id,
            "lease_id": self.lease_id,
            "content_version": self.content_version,
        }
    
class DocumentRepository:
    def __init__(self, db: AsyncDatabase) -> None:
        self._col = db[DOCUMENTS]

    # ---- writes ---------------------------------------------------------

    async def insert(self, doc: dict[str, Any]) -> None:
        """Insert a new document.

        DuplicateKeyError (same client_doc_ref) is deliberately NOT caught here.
        What a duplicate ref means (idempotent retry vs 409) is business policy,
        so the service layer decides it.
        """
        await self._col.insert_one(doc)

    # ---- owner-scoped reads (used by the API) ----------------------------

    async def get_owned(self, document_id: str, user_id: str) -> dict[str, Any] | None:
        """Fetch a document only if it belongs to user_id.

        _id and user_id are matched in ONE query, so "does not exist",
        "belongs to someone else" and "malformed id" all return None, and
        all become the same 404. Nothing distinguishes them, not even timing.
        """
        oid = parse_object_id(document_id)
        if oid is None:
            return None
        return await self._col.find_one({"_id": oid, "user_id": user_id})

    async def get_owned_by_ref(self, client_doc_ref: str, user_id: str) -> dict[str, Any] | None:
        """GET /documents/by-ref/{ref}. Uses the client_doc_ref_unique index.

        The equality on client_doc_ref implies the field exists, which is what
        lets Mongo use the partial index (proven by the explain() test).
        """
        return await self._col.find_one({"client_doc_ref": client_doc_ref, "user_id": user_id})

    async def list_for_user(
        self,
        user_id: str,
        *,
        status: DocumentStatus | None,
        page: int,
        page_size: int,
    ) -> list[dict[str, Any]]:
        """One page of a user's documents, newest first.

        skip/limit is fine at this scale. Its cost grows with the page number;
        the README's "At 100x" section explains the keyset-cursor replacement.
        """
        query = self._user_query(user_id, status)
        cursor = (
            self._col.find(query).sort(NEWEST_FIRST).skip((page - 1) * page_size).limit(page_size)
        )
        return await cursor.to_list(length=page_size)

    async def count_for_user(self, user_id: str, *, status: DocumentStatus | None) -> int:
        """Total for pagination metadata. Served by the same indexes as the list."""
        return await self._col.count_documents(self._user_query(user_id, status))

    # ---- internal reads (never returned to a caller as-is) ---------------

    async def get_by_ref_any_owner(self, client_doc_ref: str) -> dict[str, Any] | None:
        """Look up a ref regardless of owner.

        Only for applying the crosswalk policy after a DuplicateKeyError (Phase 6),
        e.g. "same ref, different user, so 409". Never expose its result directly.
        """
        return await self._col.find_one({"client_doc_ref": client_doc_ref})

    async def count_active(self, user_id: str) -> int:
        """Rate-limit fallback when Redis is down. Served by user_status_created."""
        return await self._col.count_documents(
            {"user_id": user_id, "status": {"$in": _ACTIVE_VALUES}}
        )

    async def find_result_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Content-cache fallback when Redis is down (PLAN A8).

        Returns any stored result whose content_hash matches, even from a
        document that has since been PATCHed to different content. That is
        still correct: summary and tags are a function of the content alone,
        and the result carries the hash of the content that produced it.
        """
        doc = await self._col.find_one(
            {"result.content_hash": content_hash},
            projection={"_id": 0, "result": 1},
        )
        return doc["result"] if doc else None

        # ---- worker: claim and fenced stage transitions ------------------------

    async def claim_next(
        self, *, now: datetime, lease_id: str, lease_until: datetime
    ) -> dict[str, Any] | None:
        """Atomically take the oldest due job, or return None if nothing is due.

        "Due" = active status and next_attempt_at <= now. That covers new documents,
        retries whose backoff has passed, and jobs whose previous lease expired
        (crashed worker), all with one condition, served by the claim_queue index.

        find_one_and_update is a single atomic operation on a single document: when
        several workers race for the same job, exactly one update applies. Setting
        next_attempt_at to the lease expiry is what makes the job invisible to the
        others until then.
        """
        return await self._col.find_one_and_update(
            {"status": {"$in": _ACTIVE_VALUES}, "next_attempt_at": {"$lte": now}},
            {"$set": {"lease_id": lease_id, "next_attempt_at": lease_until, "updated_at": now}},
            sort=[("next_attempt_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def start_stage(self, fence: Fence, stage: StageName, *, now: datetime) -> bool:
        """Mark the stage running and count the attempt. Status moves queued -> processing."""
        return await self._fenced(
            fence,
            {
                "$set": {
                    "status": stage.value,
                    f"stages.{stage.value}.state": StageState.RUNNING.value,
                    "updated_at": now,
                },
                "$inc": {f"stages.{stage.value}.attempts": 1},
            },
        )

    async def complete_processing(self, fence: Fence, *, summary: str, now: datetime) -> bool:
        """Stage 1 done: keep the summary, labelled with its version, and hand over to stage 2.

        stage1_output is kept even if enriching later fails, so processing is never redone.
        The lease is released and the job is due immediately: enriching is claimed as
        its own job, by this worker or any other.
        """
        return await self._fenced(
            fence,
            {
                "$set": {
                    "stage1_output": {"content_version": fence.content_version, "summary": summary},
                    "stages.processing.state": StageState.SUCCEEDED.value,
                    "stages.processing.last_error": None,
                    "status": DocumentStatus.ENRICHING.value,
                    "lease_id": None,
                    "next_attempt_at": now,
                    "updated_at": now,
                }
            },
        )

    async def complete_enriching(
        self,
        fence: Fence,
        *,
        content_hash: str,
        summary: str,
        tags: list[str],
        now: datetime,
    ) -> bool:
        """Stage 2 done: publish summary + tags together.

        One $set on one document is atomic in MongoDB, so a reader sees the whole new
        result or none of it - never new tags next to an old summary. The result is
        labelled with the version and hash of the content it was computed from.
        """
        return await self._fenced(
            fence,
            {
                "$set": {
                    "result": {
                        "content_version": fence.content_version,
                        "content_hash": content_hash,
                        "summary": summary,
                        "tags": tags,
                        "completed_at": now,
                    },
                    "stages.enriching.state": StageState.SUCCEEDED.value,
                    "stages.enriching.last_error": None,
                    "status": DocumentStatus.COMPLETED.value,
                    "failed_stage": None,
                    "lease_id": None,
                    "next_attempt_at": None,  # terminal: never claimable again
                    "updated_at": now,
                }
            },
        )

    async def schedule_retry(
        self,
        fence: Fence,
        stage: StageName,
        *,
        error: str,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        """Attempt failed, retries left: release the job and make it due again at retry_at.

        Status stays processing/enriching; the stage shows state=pending, its attempt
        count and the last error, so a caller can see it is waiting to retry.
        """
        return await self._fenced(
            fence,
            {
                "$set": {
                    f"stages.{stage.value}.state": StageState.PENDING.value,
                    f"stages.{stage.value}.last_error": error,
                    "lease_id": None,
                    "next_attempt_at": retry_at,
                    "updated_at": now,
                }
            },
        )

    async def fail_stage(
        self, fence: Fence, stage: StageName, *, error: str, now: datetime
    ) -> bool:
        """Out of attempts: terminal failure, recording exactly which stage failed."""
        return await self._fenced(
            fence,
            {
                "$set": {
                    f"stages.{stage.value}.state": StageState.FAILED.value,
                    f"stages.{stage.value}.last_error": error,
                    "status": DocumentStatus.FAILED.value,
                    "failed_stage": stage.value,
                    "lease_id": None,
                    "next_attempt_at": None,
                    "updated_at": now,
                }
            },
        )

    async def _fenced(self, fence: Fence, update: dict[str, Any]) -> bool:
        """Apply an update only if this worker still owns this version of the document.

        Returns False when the fence rejected the write (0 documents matched).
        """
        result = await self._col.update_one(fence.as_filter(), update)
        return result.matched_count == 1

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _user_query(user_id: str, status: DocumentStatus | None) -> dict[str, Any]:
        query: dict[str, Any] = {"user_id": user_id}
        if status is not None:
            query["status"] = status.value
        return query
