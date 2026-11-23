"""Business rules for documents.

Layering: routers translate HTTP <-> Python, the repository translates Python <-> Mongo,
and this service decides what is allowed. It raises AppError subclasses and never
touches HTTP objects, so it can be unit-tested without FastAPI.

Later phases add their rules here, in this order inside submit():
content cache (7) -> rate limit (7) -> insert -> crosswalk policy on duplicate (6).
PATCH (5) becomes a new method.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from pymongo.errors import DuplicateKeyError

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domain.documents import new_document
from app.domain.enums import DocumentStatus
from app.repositories.document_repo import DocumentRepository
from app.schemas.documents import DocumentCreate

log = structlog.get_logger()


class DocumentService:
    def __init__(self, repo: DocumentRepository) -> None:
        self._repo = repo

    async def submit(self, *, caller_id: str, data: DocumentCreate) -> dict[str, Any]:
        """Store a new document as queued and return it."""
        # The spec puts user_id in the body; the header says who is calling.
        # Submitting on someone else's behalf is refused with 403 (not 404):
        # this reveals nothing about any document, it is about the caller's own identity.
        if data.user_id != caller_id:
            raise ForbiddenError("user_id in the body does not match X-User-Id")

        doc = new_document(
            user_id=data.user_id,
            title=data.title,
            content=data.content,
            client_doc_ref=data.client_doc_ref,
            now=datetime.now(UTC),
        )
        try:
            await self._repo.insert(doc)
        except DuplicateKeyError as exc:
            if not _is_client_doc_ref_conflict(exc):
                raise  # any other duplicate is a bug; let the 500 handler log it
            # Interim rule. Phase 6 replaces this with the full crosswalk policy
            # (same ref + same content = idempotent 200, different = 409 with id).
            raise ConflictError("client_doc_ref is already in use") from exc

        log.info(
            "document_submitted",
            document_id=str(doc["_id"]),
            user_id=doc["user_id"],
            has_client_doc_ref="client_doc_ref" in doc,
        )
        return doc

    async def get(self, *, caller_id: str, document_id: str) -> dict[str, Any]:
        """Return the caller's document, or raise NotFoundError.

        Missing, someone else's and malformed ids all end here as the same 404,
        because the repository matches _id and user_id in one query.
        """
        doc = await self._repo.get_owned(document_id, caller_id)
        if doc is None:
            raise NotFoundError("Document not found")
        return doc

    async def list_for_user(
        self,
        *,
        caller_id: str,
        user_id: str,
        status: DocumentStatus | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (one page of documents, total count) for the caller's own listing."""
        # Asking for another user's listing gets the same 404 regardless of whether
        # that user exists, so the endpoint cannot be used to discover user ids.
        if user_id != caller_id:
            raise NotFoundError("User not found")

        items = await self._repo.list_for_user(
            user_id, status=status, page=page, page_size=page_size
        )
        total = await self._repo.count_for_user(user_id, status=status)
        return items, total


def _is_client_doc_ref_conflict(exc: DuplicateKeyError) -> bool:
    """True if the duplicate was on client_doc_ref (the only user-controlled unique key)."""
    key_pattern = (exc.details or {}).get("keyPattern", {})
    return "client_doc_ref" in key_pattern
