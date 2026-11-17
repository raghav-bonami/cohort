"""Every MongoDB query on the documents collection lives here.

Routers and services never touch Mongo directly. Two
benefits: each query sits next to the index it relies on, and the ownership
rule (filter by user_id) is enforced in one place.

Will covers the API's reads and writes. Later add their own methods
"""

from typing import Any

from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.db.ids import parse_object_id
from app.db.mongo import DOCUMENTS
from app.domain.enums import ACTIVE_STATUSES, DocumentStatus

# "Newest first", with _id as tie-breaker. Matches both listing indexes exactly,
# so Mongo reads the index in order and never sorts in memory.
NEWEST_FIRST = [("created_at", DESCENDING), ("_id", DESCENDING)]


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
            self._col.find(query)
            .sort(NEWEST_FIRST)
            .skip((page - 1) * page_size)
            .limit(page_size)
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
        """Rate-limit fallback when Redis is down (PLAN A7).

        Mongo is the source of truth for status. Served by user_status_created.
        """
        return await self._col.count_documents(
            {"user_id": user_id, "status": {"$in": [s.value for s in ACTIVE_STATUSES]}}
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

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _user_query(user_id: str, status: DocumentStatus | None) -> dict[str, Any]:
        query: dict[str, Any] = {"user_id": user_id}
        if status is not None:
            query["status"] = status.value
        return query
