from fastapi import Request , Depends
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.repositories.document_repo import DocumentRepository


def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


def get_redis(request: Request) -> Redis:
    return request.app.state.redis

def get_document_repo(db: AsyncDatabase = Depends(get_db)) -> DocumentRepository:
    """Routers depend on the repository, never on the raw database.

    Tests can override this one function to inject a different database.
    """
    return DocumentRepository(db)