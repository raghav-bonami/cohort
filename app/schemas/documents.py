"""Request and response models for the documents API.

Validation goes beyond Pydantic defaults on purpose:
- every string is trimmed, so "   " counts as empty and is rejected;
- every string has a maximum length, so one request cannot store megabytes;
- identifier fields allow only a safe character set;
- unknown fields are rejected instead of silently ignored.

Limits are constants, not settings: they are part of the API contract and appear in
the OpenAPI schema, so they should not change per environment.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.domain.documents import is_result_current
from app.domain.enums import DocumentStatus, StageName, StageState

MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 100_000
MAX_USER_ID_CHARS = 64
MAX_REF_CHARS = 128

# Pagination bounds. page_size caps response size; MAX_PAGE caps how deep skip()
# can go, because skip walks every skipped index entry (see README "At 100x").
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 10_000

# Letters, digits and . _ : - only. No "/" (a ref must fit in the
# /documents/by-ref/{ref} path as one segment), no spaces, no control characters.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9._:-]+$"

UserId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_USER_ID_CHARS,
        pattern=IDENTIFIER_PATTERN,
    ),
]
ClientDocRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_REF_CHARS,
        pattern=IDENTIFIER_PATTERN,
    ),
]
Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_CHARS)
]
# Trimmed before hashing: "text" and "  text  " are the same content and share a
# cache entry. Inner whitespace is kept; it is part of the content.
Content = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CONTENT_CHARS)
]


# ---- requests --------------------------------------------------------------


class DocumentCreate(BaseModel):
    """Body of POST /documents."""

    # extra="forbid": a client sending {"status": "completed"} or a misspelled
    # field gets a 422, instead of the field being dropped without notice.
    model_config = ConfigDict(extra="forbid")

    user_id: UserId
    title: Title
    content: Content
    client_doc_ref: ClientDocRef | None = None


class DocumentUpdate(BaseModel):
    """Body of PATCH /documents/{id}. Send at least one field.

    user_id and client_doc_ref are not here on purpose: ownership and the external
    reference are identity, not editable data. extra="forbid" turns an attempt to
    change them into a 422 instead of a silent ignore.
    """

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    content: Content | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "DocumentUpdate":
        if self.title is None and self.content is None:
            raise ValueError("provide at least one of: title, content")
        return self


# ---- responses -------------------------------------------------------------
# Every response model is built field by field from the stored document
# (never **doc), so internal fields - lease_id, next_attempt_at, stage1_output,
# the full content - can never leak into a response by accident.


class SubmitResponse(BaseModel):
    """Body of POST /documents (201)."""

    document_id: str
    status: DocumentStatus
    content_version: int

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "SubmitResponse":
        return cls(
            document_id=str(doc["_id"]),
            status=doc["status"],
            content_version=doc["content_version"],
        )


class StageView(BaseModel):
    state: StageState
    attempts: int
    last_error: str | None


class StagesView(BaseModel):
    processing: StageView
    enriching: StageView


class ResultView(BaseModel):
    """Summary and tags always arrive together with the version that produced them."""

    content_version: int
    content_hash: str
    summary: str
    tags: list[str]
    completed_at: datetime


class DocumentResponse(BaseModel):
    """Body of GET /documents/{id}.

    Staleness is answered by comparison, not a flag: content_version is the
    document's current version, result.content_version is the version the result
    came from, and result_is_current is simply whether they are equal.
    """

    document_id: str
    title: str
    client_doc_ref: str | None
    status: DocumentStatus
    failed_stage: StageName | None
    content_version: int
    content_hash: str
    stages: StagesView
    result: ResultView | None
    result_is_current: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "DocumentResponse":
        return cls(
            document_id=str(doc["_id"]),
            title=doc["title"],
            client_doc_ref=doc.get("client_doc_ref"),  # field is absent when no ref
            status=doc["status"],
            failed_stage=doc["failed_stage"],
            content_version=doc["content_version"],
            content_hash=doc["content_hash"],
            stages=doc["stages"],
            result=doc["result"],
            result_is_current=is_result_current(doc),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
        )


class DocumentListItem(BaseModel):
    """One row of the listing: enough to show status, without stage detail or result."""

    document_id: str
    title: str
    client_doc_ref: str | None
    status: DocumentStatus
    failed_stage: StageName | None
    content_version: int
    result_is_current: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "DocumentListItem":
        return cls(
            document_id=str(doc["_id"]),
            title=doc["title"],
            client_doc_ref=doc.get("client_doc_ref"),
            status=doc["status"],
            failed_stage=doc["failed_stage"],
            content_version=doc["content_version"],
            result_is_current=is_result_current(doc),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
        )


class DocumentListResponse(BaseModel):
    """Body of GET /users/{user_id}/documents."""

    items: list[DocumentListItem]
    page: int
    page_size: int
    total: int
    has_next: bool
