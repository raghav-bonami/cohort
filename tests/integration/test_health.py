"""Health check, request ids, and the last-resort 500 handler."""

import httpx
from asgi_lifespan import LifespanManager

from app.api.deps import get_document_service
from app.main import create_app

DEAD_REDIS_URL = "redis://localhost:6390/0"  # nothing listens here: simulates Redis down


async def test_health_is_ok_when_both_dependencies_are_up(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"mongo": {"status": "ok"}, "redis": {"status": "ok"}},
    }


async def test_health_reports_degraded_when_redis_is_down(make_client):
    async with make_client(redis_url=DEAD_REDIS_URL) as http:
        resp = await http.get("/health")
    assert resp.status_code == 503  # a load balancer can act on this
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["mongo"] == {"status": "ok"}
    assert body["checks"]["redis"]["status"] == "down"


async def test_request_id_is_echoed_or_generated(client):
    given = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert given.headers["x-request-id"] == "trace-123"  # caller's id kept for tracing

    generated = await client.get("/health")
    assert len(generated.headers["x-request-id"]) == 32  # uuid4 hex


async def test_error_body_carries_the_request_id(client):
    resp = await client.get(
        "/documents/not-an-id", headers={"X-User-Id": "alice", "X-Request-ID": "trace-456"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["request_id"] == "trace-456"


async def test_unexpected_error_is_500_without_internals(mongo_client, test_settings):
    class BrokenService:
        async def get(self, **_):
            raise RuntimeError("cannot reach mongodb://secret-host:27017")

    app = create_app(test_settings)
    app.dependency_overrides[get_document_service] = lambda: BrokenService()
    async with LifespanManager(app) as manager:
        # raise_app_exceptions=False: look at the response the client would get,
        # instead of having the test client re-raise the server-side exception.
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            resp = await http.get(f"/documents/{'a' * 24}", headers={"X-User-Id": "alice"})

    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "Internal server error"
    assert "secret-host" not in resp.text  # internals stay in the logs
