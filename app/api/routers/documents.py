"""Document endpoints. Thin by design: parse input, call the service, shape the output."""

from fastapi import APIRouter, Response, Header

from typing import Annotated

from app.api.deps import CurrentUser, DocumentServiceDep
from app.schemas.documents import DocumentCreate, DocumentResponse, DocumentUpdate, SubmitResponse
from app.schemas.errors import error_responses
from app.api.etag import etag_for, parse_if_match

router = APIRouter(prefix="/documents", tags=["documents"])

IfMatch = Annotated[str | None, Header(alias="If-Match", description='Optional. The Etag (content_version) you last saw, e.g. "3"'
                                       "The PATCH applies only if it still current; otherwise 412.")]

@router.post(
    "",
    status_code=201,
    response_model=SubmitResponse,
    responses=error_responses(401, 403, 409, 422),
)
async def submit_document(
    body: DocumentCreate,
    response: Response,
    caller: CurrentUser,
    service: DocumentServiceDep,
) -> SubmitResponse:
    """Submit a document for processing. Returns 201 with its id and status (queued)."""
    doc = await service.submit(caller_id=caller, data=body)
    # Standard for 201: tell the client where the new resource lives (its poll URL).
    response.headers["Location"] = f"/documents/{doc['_id']}"
    return SubmitResponse.from_doc(doc)


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    responses=error_responses(401, 404),
)
async def get_document(
    document_id: str,  # plain str on purpose: a malformed id must give 404, not 422
    response: Response,
    caller: CurrentUser,
    service: DocumentServiceDep,
) -> DocumentResponse:
    """Poll a document: stage progress, and the result with the version it came from."""
    doc = await service.get(caller_id=caller, document_id=document_id)
    response.headers["Etag"] = etag_for(doc)
    return DocumentResponse.from_doc(doc)

@router.patch(
    "/{document_id}",
    response_model=DocumentResponse,
    responses=error_responses(400, 401, 404, 412, 422),
)
async def update_document(
    document_id: str,
    body: DocumentUpdate,
    response: Response,
    caller: CurrentUser,
    service: DocumentServiceDep,
    if_match: IfMatch = None,
) -> DocumentResponse:
    """Change title and/or content. New content creates a new content_version and is
    reprocessed; the previous result stays visible, marked result_is_current=false."""
    doc = await service.update(
        caller_id=caller,
        document_id=document_id,
        data=body,
        expected_version=parse_if_match(if_match),
    )
    response.headers["ETag"] = etag_for(doc)
    return DocumentResponse.from_doc(doc)
