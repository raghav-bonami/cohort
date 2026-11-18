import structlog
from fastapi import APIRouter, Depends, Response
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.deps import get_db, get_redis

router = APIRouter(tags=["health"])
log = structlog.get_logger()


@router.get("/health")
async def health(
    response: Response,
    db: AsyncDatabase = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    checks: dict[str, dict] = {}

    try:
        await db.command("ping")
        checks["mongo"] = {"status": "ok"}
    except PyMongoError as exc:
        log.warning("health_check_failed", dependency="mongo", error=str(exc))
        checks["mongo"] = {"status": "down", "error": type(exc).__name__}

    try:
        await redis.ping()
        checks["redis"] = {"status": "ok"}
    except RedisError as exc:
        log.warning("health_check_failed", dependency="redis", error=str(exc))
        checks["redis"] = {"status": "down", "error": type(exc).__name__}

    healthy = all(c["status"] == "ok" for c in checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}
