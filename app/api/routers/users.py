"""User-scoped endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DocumentServiceDep
from app.domain.enums import DocumentStatus
from app.schemas.documents import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    DocumentListItem,
    DocumentListResponse,
)
from app.schemas.errors import error_responses

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/{user_id}/documents",
    response_model=DocumentListResponse,
    responses=error_responses(401, 404, 422),
)
async def list_user_documents(
    user_id: str,
    caller: CurrentUser,
    service: DocumentServiceDep,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status: DocumentStatus | None = None,  # unknown value -> 422 listing the allowed ones
) -> DocumentListResponse:
    """List the caller's own documents, newest first."""
    items, total = await service.list_for_user(
        caller_id=caller, user_id=user_id, status=status, page=page, page_size=page_size
    )
    return DocumentListResponse(
        items=[DocumentListItem.from_doc(doc) for doc in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=page * page_size < total,
    )