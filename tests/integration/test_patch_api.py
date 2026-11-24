"""PATCH /documents/{id} over HTTP: versioning, no-ops, If-Match, ownership."""

import asyncio
from datetime import UTC, datetime

import pytest
from bson import ObjectId

from app.db.mongo import DOCUMENTS
from app.services.hashing import content_hash

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


async def submit(client, content="original text") -> str:
    resp = await client.post(
        "/documents", json={"user_id": "alice", "title": "T", "content": content}, headers=ALICE
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def patch(client, doc_id, body, *, headers=ALICE, if_match=None):
    h = dict(headers)
    if if_match is not None:
        h["If-Match"] = if_match
    return await client.patch(f"/documents/{doc_id}", json=body, headers=h)


async def mark_completed(db, doc_id):
    """Pretend the worker finished version 1 (the worker itself is tested elsewhere)."""
    oid = ObjectId(doc_id)
    doc = await db[DOCUMENTS].find_one({"_id": oid})
    await db[DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "completed",
                "next_attempt_at": None,
                "stages.processing.state": "succeeded",
                "stages.enriching.state": "succeeded",
                "result": {
                    "content_version": 1,
                    "content_hash": doc["content_hash"],
                    "summary": "s",
                    "tags": ["t"],
                    "completed_at": datetime.now(UTC),
                },
            }
        },
    )


# ---- content changes -------------------------------------------------------


async def test_new_content_bumps_version_requeues_and_keeps_old_result_stale(client, db):
    doc_id = await submit(client)
    await mark_completed(db, doc_id)

    resp = await patch(client, doc_id, {"content": "new text"})
    assert resp.status_code == 200
    assert resp.headers["etag"] == '"2"'
    body = resp.json()
    assert body["content_version"] == 2
    assert body["status"] == "queued"
    assert body["stages"]["processing"] == {"state": "pending", "attempts": 0, "last_error": None}
    assert body["result"]["content_version"] == 1  # old result still shown...
    assert body["result_is_current"] is False  # ...and clearly marked stale

    stored = await db[DOCUMENTS].find_one({"_id": ObjectId(doc_id)})
    assert stored["content"] == "new text"
    assert stored["content_hash"] == content_hash("new text")
    assert stored["stage1_output"] is None and stored["lease_id"] is None


async def test_same_content_is_a_noop(client, db):
    doc_id = await submit(client, content="same text")
    await mark_completed(db, doc_id)

    resp = await patch(client, doc_id, {"content": "  same text  "})  # trimmed = same hash
    body = resp.json()
    assert resp.status_code == 200
    assert body["content_version"] == 1
    assert body["status"] == "completed"
    assert body["result_is_current"] is True


async def test_title_only_does_not_reprocess(client, db):
    doc_id = await submit(client)
    await mark_completed(db, doc_id)

    body = (await patch(client, doc_id, {"title": "Renamed"})).json()
    assert body["title"] == "Renamed"
    assert body["content_version"] == 1
    assert body["status"] == "completed"


async def test_patch_requeues_a_failed_document(client, db):
    doc_id = await submit(client)
    await db[DOCUMENTS].update_one(
        {"_id": ObjectId(doc_id)},
        {
            "$set": {
                "status": "failed",
                "failed_stage": "processing",
                "next_attempt_at": None,
                "stages.processing": {"state": "failed", "attempts": 3, "last_error": "boom"},
            }
        },
    )
    body = (await patch(client, doc_id, {"content": "fixed text"})).json()
    assert body["status"] == "queued"
    assert body["failed_stage"] is None
    assert body["stages"]["processing"]["attempts"] == 0


async def test_without_if_match_last_write_wins(client, db):
    doc_id = await submit(client)
    await patch(client, doc_id, {"content": "second"})
    body = (await patch(client, doc_id, {"content": "third"})).json()
    assert body["content_version"] == 3
    stored = await db[DOCUMENTS].find_one({"_id": ObjectId(doc_id)})
    assert stored["content"] == "third"


# ---- If-Match ---------------------------------------------------------------


async def test_get_returns_etag(client):
    doc_id = await submit(client)
    resp = await client.get(f"/documents/{doc_id}", headers=ALICE)
    assert resp.headers["etag"] == '"1"'


async def test_if_match_current_applies_and_stale_is_412(client):
    doc_id = await submit(client)

    ok = await patch(client, doc_id, {"content": "v2"}, if_match='"1"')
    assert ok.status_code == 200
    assert ok.headers["etag"] == '"2"'

    stale = await patch(client, doc_id, {"content": "v3"}, if_match='"1"')
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "precondition_failed"

    body = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert body["content_version"] == 2  # the stale PATCH changed nothing


async def test_concurrent_patches_with_same_if_match_exactly_one_wins(client):
    doc_id = await submit(client)
    responses = await asyncio.gather(
        *(patch(client, doc_id, {"content": f"edit {i}"}, if_match='"1"') for i in range(5))
    )
    assert sorted(r.status_code for r in responses) == [200, 412, 412, 412, 412]
    body = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert body["content_version"] == 2


@pytest.mark.parametrize("value", ["abc", 'W/"1"', '"1", "2"'])
async def test_malformed_if_match_is_400(client, value):
    doc_id = await submit(client)
    resp = await patch(client, doc_id, {"content": "x"}, if_match=value)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# ---- ownership and validation ---------------------------------------------


async def test_foreign_document_is_404_even_with_stale_if_match(client):
    doc_id = await submit(client)
    resp = await patch(client, doc_id, {"content": "x"}, headers=BOB, if_match='"99"')
    assert resp.status_code == 404  # never 412: that would reveal the document exists


async def test_patch_without_identity_is_401(client):
    doc_id = await submit(client)
    resp = await client.patch(f"/documents/{doc_id}", json={"content": "x"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "body", [{}, {"content": "   "}, {"user_id": "bob"}, {"client_doc_ref": "r"}]
)
async def test_invalid_patch_body_is_422(client, body):
    doc_id = await submit(client)
    resp = await patch(client, doc_id, body)
    assert resp.status_code == 422


async def test_title_and_content_in_one_patch(client):
    doc_id = await submit(client)
    body = (await patch(client, doc_id, {"title": "New title", "content": "new text"})).json()
    assert (body["title"], body["content_version"], body["status"]) == ("New title", 2, "queued")
