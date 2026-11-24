"""Content cache (PLAN A8): identical content completes without reprocessing."""

from datetime import UTC, datetime

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.db.mongo import DOCUMENTS
from app.db.redis import create_redis_client
from app.domain.documents import new_document
from app.services.hashing import content_hash
from app.services.result_cache import ResultCache
from app.worker.stages import make_summary, make_tags

ALICE = {"X-User-Id": "alice"}
DEAD_REDIS_URL = "redis://localhost:6390/0"  # nothing listens here: simulates Redis down
TEXT = "MongoDB stores every document and the worker processes each document."


async def submit(http, content=TEXT, **extra):
    body = {"user_id": "alice", "title": "T", "content": content, **extra}
    return await http.post("/documents", json=body, headers=ALICE)


async def drain(worker):
    while await worker.run_once():
        pass


async def test_known_content_completes_immediately(client, worker, redis_client):
    await submit(client)
    await drain(worker)  # first run computes and caches
    assert await redis_client.ttl(ResultCache.key(content_hash(TEXT))) > 0

    resp = await submit(client, title="Same text again")
    assert resp.status_code == 201
    assert resp.json()["status"] == "completed"  # no queue at all

    body = (await client.get(f"/documents/{resp.json()['document_id']}", headers=ALICE)).json()
    assert body["result"]["summary"] == make_summary(TEXT)
    assert body["result"]["tags"] == make_tags(make_summary(TEXT))
    assert body["result"]["content_version"] == 1  # labelled with THIS document's version
    assert body["result_is_current"] is True
    assert body["stages"]["processing"]["attempts"] == 0  # not run here


async def test_cache_hit_needs_no_rate_limit_slot(make_client, worker):
    async with make_client(max_active_per_user=3) as http:
        await submit(http)
        await drain(worker)
        for i in range(3):
            await submit(http, content=f"other {i}")  # at the limit

        resp = await submit(http)  # cached content
        assert resp.status_code == 201
        assert resp.json()["status"] == "completed"


async def test_patch_to_known_content_is_completed_by_worker_from_cache(client, worker):
    await submit(client, content=TEXT)
    other_id = (await submit(client, content="something else entirely")).json()["document_id"]
    await drain(worker)

    resp = await client.patch(f"/documents/{other_id}", json={"content": TEXT}, headers=ALICE)
    assert resp.json()["status"] == "queued"
    assert await worker.run_once() is True  # one claim, no stages run

    body = (await client.get(f"/documents/{other_id}", headers=ALICE)).json()
    assert body["status"] == "completed"
    assert body["content_version"] == 2
    assert body["result"]["content_version"] == 2
    assert body["result"]["summary"] == make_summary(TEXT)
    assert body["stages"]["processing"]["attempts"] == 0


async def test_patched_content_never_gets_the_old_result(client, worker):
    doc_id = (await submit(client, content=TEXT)).json()["document_id"]
    await drain(worker)

    body = (
        await client.patch(f"/documents/{doc_id}", json={"content": "brand new"}, headers=ALICE)
    ).json()
    assert body["status"] == "queued"  # the new hash is not cached
    assert body["result_is_current"] is False  # old result shown only as stale


async def test_corrupt_cache_entry_is_a_miss(client, redis_client):
    await redis_client.set(ResultCache.key(content_hash(TEXT)), "not json")
    resp = await submit(client)
    assert resp.json()["status"] == "queued"


@pytest.mark.parametrize("seed_mongo", [True, False])
async def test_redis_down_falls_back_to_mongo(make_client, db, seed_mongo):
    if seed_mongo:
        # A document that already completed with this content (in Mongo only).
        doc = new_document(
            user_id="bob", title="t", content=TEXT, client_doc_ref=None, now=datetime.now(UTC)
        )
        doc.update(
            status="completed",
            next_attempt_at=None,
            result={
                "content_version": 1,
                "content_hash": doc["content_hash"],
                "summary": make_summary(TEXT),
                "tags": make_tags(make_summary(TEXT)),
                "completed_at": datetime.now(UTC),
            },
        )
        await db[DOCUMENTS].insert_one(doc)

    async with make_client(redis_url=DEAD_REDIS_URL) as http:
        resp = await submit(http)
    assert resp.status_code == 201
    assert resp.json()["status"] == ("completed" if seed_mongo else "queued")


async def test_cache_gives_up_quietly_when_redis_and_mongo_are_both_down():
    class MongoDown:
        async def find_result_by_hash(self, content_hash):
            raise ServerSelectionTimeoutError("simulated outage")

    dead = create_redis_client(DEAD_REDIS_URL)
    cache = ResultCache(dead, MongoDown(), ttl_seconds=60)
    try:
        assert await cache.get(content_hash(TEXT)) is None  # miss: process normally
        await cache.put(content_hash(TEXT), summary="s", tags=[])  # logged, never raised
    finally:
        await dead.aclose()
