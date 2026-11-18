"""End-to-end over HTTP against real MongoDB."""

from datetime import UTC, datetime

import pytest
from bson import ObjectId

from app.db.mongo import DOCUMENTS

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


def payload(**overrides):
    return {"user_id": "alice", "title": "Q3 report", "content": "Some content", **overrides}


async def submit(client, headers=ALICE, **overrides) -> str:
    resp = await client.post("/documents", json=payload(**overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


# ---- POST /documents --------------------------------------------------------

async def test_submit_returns_201_id_status_and_location(client):
    resp = await client.post("/documents", json=payload(), headers=ALICE)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["content_version"] == 1
    assert resp.headers["location"] == f"/documents/{body['document_id']}"


async def test_submit_stores_trimmed_values(client, db):
    doc_id = await submit(client, title="  Padded  ", content="  text  ")
    stored = await db[DOCUMENTS].find_one({"_id": ObjectId(doc_id)})
    assert (stored["title"], stored["content"]) == ("Padded", "text")


async def test_validation_error_format_does_not_echo_input(client):
    resp = await client.post("/documents", json=payload(content="   "), headers=ALICE)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "body.content"
    assert "input" not in error["details"][0]


async def test_submit_without_header_is_401(client):
    resp = await client.post("/documents", json=payload())
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_submit_on_behalf_of_someone_else_is_403(client):
    resp = await client.post("/documents", json=payload(user_id="alice"), headers=BOB)
    assert resp.status_code == 403


async def test_duplicate_client_doc_ref_is_409(client):
    await submit(client, client_doc_ref="ext-1")
    resp = await client.post(
        "/documents", json=payload(client_doc_ref="ext-1", content="other"), headers=ALICE
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


# ---- GET /documents/{id} ----------------------------------------------------

async def test_get_returns_status_shape_without_internals(client):
    doc_id = await submit(client)
    resp = await client.get(f"/documents/{doc_id}", headers=ALICE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["stages"]["processing"] == {"state": "pending", "attempts": 0, "last_error": None}
    assert body["stages"]["enriching"]["state"] == "pending"
    assert body["result"] is None
    assert body["result_is_current"] is False
    assert "content" not in body and "lease_id" not in body


async def test_foreign_missing_and_malformed_ids_are_indistinguishable(client):
    doc_id = await submit(client)
    responses = [
        await client.get(f"/documents/{doc_id}", headers=BOB),        # someone else's
        await client.get(f"/documents/{ObjectId()}", headers=ALICE),  # does not exist
        await client.get("/documents/not-an-id", headers=ALICE),      # malformed
    ]
    assert [r.status_code for r in responses] == [404, 404, 404]
    # Identical bodies apart from request_id, so nothing reveals which case it was.
    bodies = [{k: v for k, v in r.json()["error"].items() if k != "request_id"} for r in responses]
    assert bodies[0] == bodies[1] == bodies[2]


async def test_result_is_current_is_a_version_comparison(client, db):
    doc_id = await submit(client)
    oid = ObjectId(doc_id)
    result = {
        "content_version": 1, "content_hash": "h", "summary": "s", "tags": ["a"],
        "completed_at": datetime.now(UTC),
    }
    await db[DOCUMENTS].update_one({"_id": oid}, {"$set": {"status": "completed", "result": result}})
    body = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert body["result_is_current"] is True

    # Simulate what PATCH will do: the content moves on, the result keeps its label.
    await db[DOCUMENTS].update_one({"_id": oid}, {"$inc": {"content_version": 1}})
    body = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert body["content_version"] == 2
    assert body["result"]["content_version"] == 1
    assert body["result_is_current"] is False


# ---- GET /users/{user_id}/documents -----------------------------------------

async def test_list_is_newest_first_and_paginated(client):
    # Submitted in quick succession: several can share a created_at millisecond.
    # The _id tie-breaker in the sort keeps the order deterministic anyway.
    ids = [await submit(client, title=f"doc {i}") for i in range(5)]

    first = (await client.get("/users/alice/documents",
                              params={"page": 1, "page_size": 2}, headers=ALICE)).json()
    last = (await client.get("/users/alice/documents",
                             params={"page": 3, "page_size": 2}, headers=ALICE)).json()

    assert [i["document_id"] for i in first["items"]] == [ids[4], ids[3]]
    assert (first["total"], first["has_next"]) == (5, True)
    assert [i["document_id"] for i in last["items"]] == [ids[0]]
    assert last["has_next"] is False


async def test_list_filters_by_status(client, db):
    done_id = await submit(client)
    await submit(client)
    await db[DOCUMENTS].update_one({"_id": ObjectId(done_id)}, {"$set": {"status": "completed"}})

    body = (await client.get("/users/alice/documents",
                             params={"status": "completed"}, headers=ALICE)).json()
    assert [i["document_id"] for i in body["items"]] == [done_id]


async def test_list_shows_only_own_documents(client):
    mine = await submit(client)
    await submit(client, headers=BOB, user_id="bob")
    body = (await client.get("/users/alice/documents", headers=ALICE)).json()
    assert [i["document_id"] for i in body["items"]] == [mine]


async def test_listing_another_user_is_404(client):
    resp = await client.get("/users/bob/documents", headers=ALICE)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "params", [{"page_size": 101}, {"page": 0}, {"status": "not-a-status"}]
)
async def test_list_rejects_bad_query_params(client, params):
    resp = await client.get("/users/alice/documents", params=params, headers=ALICE)
    assert resp.status_code == 422


# ---- framework errors use the same format -----------------------------------

async def test_unknown_route_uses_error_format(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"