"""Per-user limit on documents in progress (PLAN A7).

Redis key  active:{user_id}  is a sorted set:
  member = document id, score = when the slot was reserved (epoch seconds)

Why a set of ids rather than a counter: adding an id that is already there and
removing one that is already gone are both no-ops. Retries, a PATCH on an already-active
document, or a double release can never make the count drift. A counter (INCR/DECR) can.
Why sorted: the reservation time lets the reconciler tell an abandoned slot from one
whose insert is still in flight.

Mongo stays the source of truth: when Redis is unavailable, the limit is enforced by
counting active documents there.
"""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.clock import Clock, utc_now
from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.repositories.document_repo import DocumentRepository

log = structlog.get_logger()

# Runs atomically inside Redis: no other command can interleave, so concurrent
# requests can never all pass the "fewer than limit" check.
# KEYS[1] active:{user_id}
# ARGV[1] document id   ARGV[2] limit   ARGV[3] now (epoch s)   ARGV[4] key TTL (s)
# Returns 1 = slot held (newly or already), 0 = at the limit.
_ACQUIRE_LUA = """
local held = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not held and redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


class ActiveDocumentLimiter:
    def __init__(
        self,
        redis: Redis,
        repo: DocumentRepository,
        *,
        limit: int,
        key_ttl_seconds: int,
        grace_seconds: float,
        clock: Clock = utc_now,
    ) -> None:
        self._redis = redis
        self._repo = repo
        self._limit = limit
        self._key_ttl = key_ttl_seconds
        self._grace = grace_seconds
        self._clock = clock
        self._acquire = redis.register_script(_ACQUIRE_LUA)  # EVALSHA, cached by Redis

    @classmethod
    def from_settings(
        cls, redis: Redis, repo: DocumentRepository, settings: Settings
    ) -> "ActiveDocumentLimiter":
        return cls(
            redis,
            repo,
            limit=settings.max_active_per_user,
            key_ttl_seconds=settings.rate_limit_key_ttl_seconds,
            grace_seconds=settings.rate_limit_grace_seconds,
        )

    @staticmethod
    def key(user_id: str) -> str:
        return f"active:{user_id}"

    async def acquire(self, user_id: str, document_id: str) -> None:
        """Hold a slot for this document, or raise RateLimitedError (429).

        Holding is idempotent: a document that already has a slot keeps it.
        """
        try:
            allowed = await self._try_acquire(user_id, document_id)
            if not allowed and await self._drop_stale(user_id):
                allowed = await self._try_acquire(user_id, document_id)
        except RedisError as exc:
            # Redis is an optimisation here, not the source of truth: count in Mongo.
            # Not atomic, so during an outage concurrent requests could exceed the
            # limit slightly (documented as a soft limit).
            log.warning("rate_limit_fallback", user_id=user_id, error=str(exc))
            active = await self._repo.count_active(user_id, exclude_id=document_id)
            allowed = active < self._limit

        if not allowed:
            raise RateLimitedError(
                f"You already have {self._limit} documents in progress; "
                "submit again when one of them has completed or failed",
                details={"limit": self._limit},
            )

    async def release(self, user_id: str, document_id: str) -> None:
        """Give the slot back. Never raises: a missed release is repaired by _drop_stale."""
        try:
            await self._redis.zrem(self.key(user_id), document_id)
        except RedisError as exc:
            log.warning(
                "rate_limit_release_failed",
                user_id=user_id,
                document_id=document_id,
                error=str(exc),
            )

    async def _try_acquire(self, user_id: str, document_id: str) -> bool:
        now = self._clock().timestamp()
        result = await self._acquire(
            keys=[self.key(user_id)],
            args=[document_id, self._limit, now, self._key_ttl],
        )
        return result == 1

    async def _drop_stale(self, user_id: str) -> bool:
        """Remove slots whose document is no longer active. True if any were removed.

        Runs only when a user is refused, so normal requests pay nothing for it. Only
        reservations older than the grace period are candidates: a younger one may
        belong to a request that has not inserted its document yet.
        """
        key = self.key(user_id)
        cutoff = self._clock().timestamp() - self._grace
        candidates = await self._redis.zrangebyscore(key, "-inf", cutoff)
        if not candidates:
            return False
        still_active = await self._repo.active_ids_among(user_id, candidates)
        stale = [m for m in candidates if m not in still_active]
        if not stale:
            return False
        await self._redis.zrem(key, *stale)
        log.warning("rate_limit_slots_reconciled", user_id=user_id, removed=len(stale))
        return True
