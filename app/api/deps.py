"""FastAPI dependencies: how routes get the caller's identity, the database and services.

Routes declare what they need (CurrentUser, DocumentServiceDep); FastAPI builds it per
request. Tests can swap any of these with app.dependency_overrides.
"""

from typing import Annotated

from fastapi import Depends, Header, Request
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.core.errors import UnauthorizedError
from app.repositories.document_repo import DocumentRepository
from app.schemas.documents import UserId
from app.services.document_service import DocumentService


def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_document_repo(db: AsyncDatabase = Depends(get_db)) -> DocumentRepository:
    """Routers depend on the repository, never on the raw database."""
    return DocumentRepository(db)


def get_document_service(
    repo: DocumentRepository = Depends(get_document_repo),
) -> DocumentService:
    return DocumentService(repo)


async def get_current_user(
    x_user_id: Annotated[UserId | None, Header(alias="X-User-Id")] = None,
) -> str:
    """Identify the caller.

    Assumption (README): there is no real authentication in scope, so the caller
    states its identity in the X-User-Id header. In production this dependency
    would verify a token instead; nothing else in the app would change.
    Missing header -> 401. A malformed value fails the UserId rules -> 422.
    """
    if x_user_id is None:
        raise UnauthorizedError("Missing X-User-Id header")
    return x_user_id


# Short aliases so route signatures stay readable.
CurrentUser = Annotated[str, Depends(get_current_user)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
