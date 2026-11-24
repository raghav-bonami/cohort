"""Content cache (PLAN A8): content that was already processed is not processed again.

Redis key  cache:result:{content_hash}  ->  {"summary": ..., "tags": [...]}  (24h TTL)

Keyed by the hash of the content, never by document id: a PATCH changes the hash, so
it can never be served the old content's result. Correct only because summary and tags
are a pure function of the content (app/worker/stages.py).

If Redis is unavailable, the same question is asked of Mongo through the
result_content_hash index. If that fails too, the document is simply processed.
"""

import structlog
from pydantic import BaseModel, ValidationError
from pymongo.errors import PyMongoError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.repositories.document_repo import DocumentRepository

log = structlog.get_logger()


class CachedResult(BaseModel):
    summary: str
    tags: list[str]


class ResultCache:
    def __init__(self, redis: Redis, repo: DocumentRepository, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._repo = repo
        self._ttl = ttl_seconds

    @classmethod
    def from_settings(
        cls, redis: Redis, repo: DocumentRepository, settings: Settings
    ) -> "ResultCache":
        return cls(redis, repo, ttl_seconds=settings.cache_ttl_seconds)

    @staticmethod
    def key(content_hash: str) -> str:
        return f"cache:result:{content_hash}"

    async def get(self, content_hash: str) -> CachedResult | None:
        """The known result for this content, or None (miss, or nothing reachable)."""
        try:
            raw = await self._redis.get(self.key(content_hash))
        except RedisError as exc:
            log.warning("cache_redis_unavailable", error=str(exc))
            return await self._get_from_mongo(content_hash)

        if raw is None:
            return None
        try:
            return CachedResult.model_validate_json(raw)
        except ValidationError:
            # Never trust cache contents blindly; a bad entry is just a miss.
            log.warning("cache_entry_invalid", content_hash=content_hash)
            return None

    async def put(self, content_hash: str, *, summary: str, tags: list[str]) -> None:
        """Remember a completed result. Never raises: a failed write only costs a future hit."""
        payload = CachedResult(summary=summary, tags=tags).model_dump_json()
        try:
            await self._redis.set(self.key(content_hash), payload, ex=self._ttl)
        except RedisError as exc:
            log.warning("cache_write_failed", content_hash=content_hash, error=str(exc))

    async def _get_from_mongo(self, content_hash: str) -> CachedResult | None:
        try:
            result = await self._repo.find_result_by_hash(content_hash)
        except PyMongoError as exc:
            log.warning("cache_fallback_failed", error=str(exc))
            return None  # no cache at all: the document is processed normally
        if result is None:
            return None
        log.info("cache_hit_mongo_fallback", content_hash=content_hash)
        return CachedResult(summary=result["summary"], tags=result["tags"])
