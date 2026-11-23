"""Business rules for documents.

Layering: routers translate HTTP <-> Python, the repository translates Python <-> Mongo,
and this service decides what is allowed. It raises AppError subclasses and never
touches HTTP objects, so it can be unit-tested without FastAPI.

Later phases add their rules here:
- submit(): content cache (7) -> rate limit (7) -> insert -> crosswalk policy (6)
- update(): content cache and rate limit (7) when new content re-enters the pipeline
"""

from typing import Any

import structlog
from pymongo.errors import DuplicateKeyError

from app.core.clock import Clock, utc_now
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, PreconditionFailedError
from app.domain.documents import content_change_fields, new_document
from app.domain.enums import DocumentStatus
from app.repositories.document_repo import DocumentRepository
from app.schemas.documents import DocumentCreate, DocumentUpdate

log = structlog.get_logger()


class DocumentService:
    def __init__(self, repo: DocumentRepository, clock: Clock = utc_now) -> None:
        self._repo = repo
        self._clock = clock

    async def submit(self, *, caller_id: str, data: DocumentCreate) -> tuple[dict[str, Any], bool]:
        """Create a document, or replay an earlier identical submission.

        Returns (document, created). created=False means an idempotent retry: the
        same client_doc_ref with the same content was already submitted, and the
        existing document is returned instead of creating a second one (PLAN A6).
        """
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
        ref = data.client_doc_ref

        # Fast path for retries: answer from the existing document without inserting.
        # Phase 7 relies on this: a retry must get its 200 even when the user is at
        # the rate limit, so this check comes before the limiter.
        if ref is not None:
            existing = await self._repo.get_by_ref_any_owner(ref)
            if existing is not None:
                return _apply_crosswalk_policy(existing, caller_id, doc["content_hash"]), False

        # Phase 7: content cache, then rate limit, go here
        try:
            await self._repo.insert(doc)
        except DuplicateKeyError as exc:
            if not _is_client_doc_ref_conflict(exc):
                raise  # any other duplicate is a bug; let the 500 handler log it
            # Lost a race: another request inserted this ref between our lookup and
            # our insert. The unique index guarantees only one of us got in, so the
            # winner's document now exists; apply the same policy to it.
            # (Phase 7: release the rate-limit slot reserved for `doc` here.)
            existing = await self._repo.get_by_ref_any_owner(ref)
            if existing is None:
                # The index said the ref exists, but we can't read it (nothing deletes
                # documents today, so this should be impossible). Don't guess: log and 409.
                log.warning("crosswalk_duplicate_not_found", client_doc_ref=ref)
                raise ConflictError("client_doc_ref is already in use") from exc
            return _apply_crosswalk_policy(existing, caller_id, doc["content_hash"]), False

        log.info(
            "document_submitted",
            document_id=str(doc["_id"]),
            user_id=doc["user_id"],
            has_client_doc_ref=ref is not None,
        )
        return doc, True

    async def get(self, *, caller_id: str, document_id: str) -> dict[str, Any]:
        """Return the caller's document, or raise NotFoundError.

        Missing, someone else's and malformed ids all end here as the same 404,
        because the repository matches _id and user_id in one query.
        """
        doc = await self._repo.get_owned(document_id, caller_id)
        if doc is None:
            raise NotFoundError("Document not found")
        return doc

    async def get_by_ref(self, *, caller_id: str, client_doc_ref: str) -> dict[str, Any]:
        """Return the caller's document with this external ref, or raise NotFoundError.

        Same 404 for "no such ref" and "someone else's ref": the lookup matches
        client_doc_ref and user_id together, exactly like get().
        """
        doc = await self._repo.get_owned_by_ref(client_doc_ref, caller_id)
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


def _apply_crosswalk_policy(
    existing: dict[str, Any], caller_id: str, content_hash: str
) -> dict[str, Any]:
    """Decide what a repeated client_doc_ref means (PLAN A6).

    Returns the existing document for an idempotent retry; raises ConflictError otherwise.
    """
    document_id = str(existing["_id"])

    if existing["user_id"] != caller_id:
        # Never reveal anything about another user's document: no id, no status.
        log.info("crosswalk_conflict", reason="other_owner")
        raise ConflictError("client_doc_ref is already in use")

    if existing["content_hash"] != content_hash:
        # Same ref, different content. Not treated as a new version: deliveries can
        # arrive out of order, and an old one must not overwrite newer content.
        # The caller owns the document, so pointing at it is safe and actionable.
        log.info("crosswalk_conflict", reason="different_content", document_id=document_id)
        raise ConflictError(
            "client_doc_ref already refers to a document with different content; "
            "PATCH that document to change its content",
            details={"existing_document_id": document_id},
        )

    log.info("crosswalk_replay", document_id=document_id)
    return existing


def _check_precondition(expected_version: int | None, doc: dict[str, Any]) -> None:
    """412 if the client's If-Match version is no longer the current one."""
    if expected_version is not None and doc["content_version"] != expected_version:
        raise PreconditionFailedError(
            f"If-Match is {expected_version} but the current content_version is "
            f"{doc['content_version']}"
        )
