"""Rate limiting (PLAN A7): at most 3 active documents per user."""

import asyncio
import time

import pytest
from bson import ObjectId

from app.db.mongo import DOCUMENTS

ALICE = {"X-User-Id": "alice"}
DEAD_REDIS_URL = "redis://localhost:6390/0"  # nothing listens here: simulates Redis down


@pytest.fixture
async def api(make_client):
    """The real limit (3), unlike the default test client."""
    async with make_client(max_active_per_user=3) as http:
        yield http


async def submit(http, content, **extra):
    body = {"user_id": "alice", "title": "T", "content": content, **extra}
    return await http.post("/documents", json=body, headers=ALICE)


async def drain(worker):
    while await worker.run_once():
        pass


async def test_fourth_active_document_is_429_and_not_stored(api, db):
    for i in range(3):
        assert (await submit(api, f"doc {i}")).status_code == 201

    resp = await submit(api, "doc 3")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert resp.json()["error"]["details"] == {"limit": 3}
    assert await db[DOCUMENTS].count_documents({}) == 3


async def test_completing_documents_frees_their_slots(api, worker, redis_client):
    for i in range(3):
        await submit(api, f"doc {i}")
    await drain(worker)  # all three complete; the worker releases each slot

    assert await redis_client.zcard("active:alice") == 0
    assert (await submit(api, "doc 3")).status_code == 201


async def test_concurrent_submits_admit_exactly_the_limit(api, db):
    responses = await asyncio.gather(*(submit(api, f"doc {i}") for i in range(10)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] * 3 + [429] * 7  # the Lua check-and-add is atomic
    assert await db[DOCUMENTS].count_documents({}) == 3


async def test_patch_of_active_document_does_not_count_twice(api):
    ids = [(await submit(api, f"doc {i}")).json()["document_id"] for i in range(3)]
    resp = await api.patch(f"/documents/{ids[0]}", json={"content": "edited"}, headers=ALICE)
    assert resp.status_code == 200  # it already holds a slot


async def test_patch_that_requeues_a_completed_document_needs_a_slot(api, worker):
    done_id = (await submit(api, "first")).json()["document_id"]
    await drain(worker)  # completed, slot released
    for i in range(3):
        await submit(api, f"doc {i}")  # at the limit again

    resp = await api.patch(f"/documents/{done_id}", json={"content": "edited"}, headers=ALICE)
    assert resp.status_code == 429
    body = (await api.get(f"/documents/{done_id}", headers=ALICE)).json()
    assert (body["status"], body["content_version"]) == ("completed", 1)  # nothing changed


async def test_idempotent_retry_succeeds_even_at_the_limit(api):
    first = await submit(api, "doc 0", client_doc_ref="crm-1")
    await submit(api, "doc 1")
    await submit(api, "doc 2")

    retry = await submit(api, "doc 0", client_doc_ref="crm-1")
    assert retry.status_code == 200
    assert retry.json()["document_id"] == first.json()["document_id"]


async def test_duplicate_ref_race_gives_the_slot_back(api, redis_client):
    # Five identical submits race: one inserts, the others lose on the unique index
    # after reserving a slot. Every loser must release its slot.
    await asyncio.gather(*(submit(api, "same", client_doc_ref="crm-9") for _ in range(5)))
    assert await redis_client.zcard("active:alice") == 1


async def test_abandoned_slots_are_reconciled_from_mongo(api, redis_client):
    # Three old reservations for documents that are not active (e.g. a release was
    # missed while Redis was unreachable).
    await redis_client.zadd("active:alice", {str(ObjectId()): 0 for _ in range(3)})

    resp = await submit(api, "doc")
    assert resp.status_code == 201
    assert await redis_client.zrange("active:alice", 0, -1) == [resp.json()["document_id"]]


async def test_fresh_reservations_are_never_reconciled(api, redis_client):
    # Reserved just now: their inserts may still be in flight, so they must count.
    now = time.time()
    await redis_client.zadd("active:alice", {str(ObjectId()): now for _ in range(3)})
    assert (await submit(api, "doc")).status_code == 429


async def test_limit_still_enforced_when_redis_is_down(make_client, db):
    # Slow-ish on Windows: each Redis call waits for the 1s connect timeout.
    async with make_client(redis_url=DEAD_REDIS_URL, max_active_per_user=3) as http:
        for i in range(3):
            assert (await submit(http, f"doc {i}")).status_code == 201  # Mongo count
        assert (await submit(http, "doc 3")).status_code == 429
    assert await db[DOCUMENTS].count_documents({}) == 3


async def test_reconcile_never_drops_slots_of_active_documents(api, redis_client):
    ids = [(await submit(api, f"doc {i}")).json()["document_id"] for i in range(3)]
    # Make their reservations look old: reconcile will check them against Mongo...
    await redis_client.zadd("active:alice", {doc_id: 0 for doc_id in ids})

    assert (await submit(api, "doc 3")).status_code == 429
    # ...and keep every one, because all three documents really are still active.
    assert sorted(await redis_client.zrange("active:alice", 0, -1)) == sorted(ids)
