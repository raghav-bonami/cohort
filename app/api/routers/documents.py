"""Document endpoints. Thin by design: parse input, call the service, shape the output."""

from fastapi import APIRouter, Response

from app.api.deps import CurrentUser, DocumentServiceDep
from app.schemas.documents import DocumentCreate, DocumentResponse, SubmitResponse
from app.schemas.errors import error_responses

router = APIRouter(prefix="/documents", tags=["documents"])


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
    caller: CurrentUser,
    service: DocumentServiceDep,
) -> DocumentResponse:
    """Poll a document: stage progress, and the result with the version it came from."""
    doc = await service.get(caller_id=caller, document_id=document_id)
    return DocumentResponse.from_doc(doc)