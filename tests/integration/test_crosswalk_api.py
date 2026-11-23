"""Crosswalk policy for client_doc_ref (PLAN A6), over HTTP against real MongoDB."""

import asyncio

from app.db.mongo import DOCUMENTS

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}
REF = "crm-001"


async def post(client, *, user="alice", content="quarterly report body", ref=REF, title="T"):
    body = {"user_id": user, "title": title, "content": content}
    if ref is not None:
        body["client_doc_ref"] = ref
    return await client.post("/documents", json=body, headers={"X-User-Id": user})


# ---- the policy -------------------------------------------------------------

async def test_retry_with_same_content_returns_existing_document(client, db):
    first = await post(client)
    second = await post(client)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["document_id"] == first.json()["document_id"]
    assert second.headers["location"] == first.headers["location"]
    assert await db[DOCUMENTS].count_documents({"client_doc_ref": REF}) == 1


async def test_retry_ignores_whitespace_and_title(client):
    first = await post(client, content="quarterly report body")
    second = await post(client, content="  quarterly report body  ", title="Other title")
    assert second.status_code == 200
    assert second.json()["document_id"] == first.json()["document_id"]


async def test_same_ref_different_content_is_409_pointing_at_existing(client):
    first = await post(client, content="version one")
    resp = await post(client, content="version two")

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"] == {"existing_document_id": first.json()["document_id"]}


async def test_same_ref_other_user_is_409_without_leaking_anything(client):
    alice_doc = (await post(client)).json()["document_id"]
    resp = await post(client, user="bob")  # same ref, same content, different owner

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert "details" not in error
    assert alice_doc not in resp.text


async def test_retry_after_patch_conflicts(client):
    doc_id = (await post(client, content="original")).json()["document_id"]
    await client.patch(f"/documents/{doc_id}", json={"content": "edited"}, headers=ALICE)

    # The partner re-sends the original delivery: it no longer matches the document.
    resp = await post(client, content="original")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["existing_document_id"] == doc_id


async def test_documents_without_ref_never_collide(client):
    a = await post(client, ref=None)
    b = await post(client, ref=None)
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["document_id"] != b.json()["document_id"]


# ---- races --------------------------------------------------------------------

async def test_concurrent_identical_submits_create_exactly_one_document(client, db):
    responses = await asyncio.gather(*(post(client) for _ in range(5)))

    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["document_id"] for r in responses}) == 1
    assert await db[DOCUMENTS].count_documents({"client_doc_ref": REF}) == 1


async def test_concurrent_conflicting_submits_one_wins_rest_409(client, db):
    responses = await asyncio.gather(*(post(client, content=f"version {i}") for i in range(5)))

    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
    assert await db[DOCUMENTS].count_documents({"client_doc_ref": REF}) == 1


# ---- GET /documents/by-ref/{ref} ---------------------------------------------

async def test_by_ref_returns_own_document_with_etag(client):
    doc_id = (await post(client)).json()["document_id"]
    resp = await client.get(f"/documents/by-ref/{REF}", headers=ALICE)

    assert resp.status_code == 200
    assert resp.json()["document_id"] == doc_id
    assert resp.json()["client_doc_ref"] == REF
    assert resp.headers["etag"] == '"1"'


async def test_by_ref_foreign_and_unknown_are_the_same_404(client):
    await post(client)  # alice owns REF
    responses = [
        await client.get(f"/documents/by-ref/{REF}", headers=BOB),  # someone else's
        await client.get("/documents/by-ref/no-such-ref", headers=ALICE),  # doesn't exist
    ]
    assert [r.status_code for r in responses] == [404, 404]
    bodies = [{k: v for k, v in r.json()["error"].items() if k != "request_id"} for r in responses]
    assert bodies[0] == bodies[1]


async def test_by_ref_requires_identity(client):
    resp = await client.get(f"/documents/by-ref/{REF}")
    assert resp.status_code == 401