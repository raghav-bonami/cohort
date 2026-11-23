"""Business rules for documents.

Layering: routers translate HTTP <-> Python, the repository translates Python <-> Mongo,
and this service decides what is allowed. It raises AppError subclasses and never
touches HTTP objects, so it can be unit-tested without FastAPI.

Later phases add their rules here:
- submit(): content cache (7) -> rate limit (7) -> insert -> crosswalk policy (6)
- update(): content cache and rate limit (7) when new content re-enters the pipeline
"""

from app.core.clock import Clock, utc_now
from typing import Any

import structlog
from pymongo.errors import DuplicateKeyError

from app.core.errors import ConflictError, ForbiddenError, NotFoundError , PreconditionFailedError
from app.domain.documents import new_document , content_change_fields
from app.domain.enums import DocumentStatus
from app.repositories.document_repo import DocumentRepository
from app.schemas.documents import DocumentCreate , DocumentUpdate

log = structlog.get_logger()


class DocumentService:
    def __init__(self, repo: DocumentRepository, clock: Clock = utc_now) -> None:
        self._repo = repo
        self._clock = clock

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
            now=self._clock(),
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

    async def update(
        self,
        *,
        caller_id: str,
        document_id: str,
        data: DocumentUpdate,
        expected_version: int | None,
    ) -> dict[str, Any]:
        """Apply a PATCH and return the document as it is afterwards.

        - New content (different hash): one atomic update bumps content_version,
          resets the pipeline and re-queues. The old result stays, labelled with its
          old version, so it reads as stale until the new version completes.
        - Same content (same hash): nothing to recompute; no version bump.
        - Title only: renamed in place; no version bump, no reprocessing.
        - expected_version (from If-Match): applied only if still current, else 412.
          Without it, the last write wins.
        """
        # Ownership first: a foreign or missing document is 404 even with a bad
        # If-Match, so 412 can never reveal that someone else's document exists.
        current = await self.get(caller_id=caller_id, document_id=document_id)
        _check_precondition(expected_version, current)
        now = self._clock()

        if data.content is not None:
            changes = content_change_fields(content=data.content, now=now)
            if changes["content_hash"] != current["content_hash"]:
                if data.title is not None:
                    changes["title"] = data.title
                updated = await self._repo.replace_content(
                    current["_id"], caller_id, changes=changes, expected_version=expected_version
                )
                if updated is not None:
                    log.info(
                        "document_content_replaced",
                        document_id=document_id,
                        content_version=updated["content_version"],
                        previous_status=current["status"],
                    )
                    # Phase 7: check the content cache, and reserve a rate-limit slot
                    # if the document was terminal (completed/failed) before this PATCH.
                    return updated

                # 0 matched: a concurrent PATCH got in between our read and our write.
                # Re-read to find out which condition failed.
                current = await self.get(caller_id=caller_id, document_id=document_id)
                _check_precondition(expected_version, current)
                # Not a version conflict, so the hash matched: someone already stored
                # this exact content. Nothing to reprocess; fall through to the title.

        if data.title is not None and data.title != current["title"]:
            updated = await self._repo.update_title(
                current["_id"],
                caller_id,
                title=data.title,
                now=now,
                expected_version=expected_version,
            )
            if updated is None:
                # Only possible with If-Match: the version moved since our check.
                latest = await self.get(caller_id=caller_id, document_id=document_id)
                _check_precondition(expected_version, latest)
                updated = latest  # unreachable in practice; kept so the type is total
            log.info("document_title_updated", document_id=document_id)
            return updated

        log.info("document_patch_noop", document_id=document_id)
        return current

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

def _check_precondition(expected_version: int | None, doc: dict[str, Any]) -> None:
    """412 if the client's If-Match version is no longer the current one."""
    if expected_version is not None and doc["content_version"] != expected_version:
        raise PreconditionFailedError(
            f"If-Match is {expected_version} but the current content_version is "
            f"{doc['content_version']}"
        )