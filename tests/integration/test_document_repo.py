"""Real MongoDB: ownership, uniqueness, ordering, and proof of index use."""

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.db.mongo import DOCUMENTS
from app.domain.documents import new_document
from app.domain.enums import DocumentStatus
from app.repositories.document_repo import DocumentRepository

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make(user_id="u1", ref=None, minutes=0, content="c"):
    return new_document(
        user_id=user_id, title="t", content=content,
        client_doc_ref=ref, now=T0 + timedelta(minutes=minutes),
    )


def index_names_used(explain: dict) -> set[str]:
    """Collect every index used by the winning plan (handles nested plan shapes)."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "IXSCAN" in str(node.get("stage", "")):
                found.add(node.get("indexName"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(explain["queryPlanner"]["winningPlan"])
    return found


# ---- ownership -----------------------------------------------------------

async def test_owner_can_read(db):
    repo = DocumentRepository(db)
    doc = make()
    await repo.insert(doc)
    assert (await repo.get_owned(str(doc["_id"]), "u1"))["_id"] == doc["_id"]


async def test_other_user_missing_and_malformed_all_look_the_same(db):
    repo = DocumentRepository(db)
    doc = make(user_id="owner")
    await repo.insert(doc)

    assert await repo.get_owned(str(doc["_id"]), "intruder") is None  # someone else's
    assert await repo.get_owned(str(ObjectId()), "owner") is None     # does not exist
    assert await repo.get_owned("not-an-id", "owner") is None         # malformed


# ---- crosswalk uniqueness ------------------------------------------------

async def test_duplicate_ref_is_rejected_by_the_index(db):
    repo = DocumentRepository(db)
    await repo.insert(make(ref="ext-1"))
    with pytest.raises(DuplicateKeyError):
        await repo.insert(make(ref="ext-1", content="different"))


async def test_many_documents_without_ref_are_allowed(db):
    repo = DocumentRepository(db)
    for _ in range(3):
        await repo.insert(make(ref=None))  # partial index ignores them
    assert await db[DOCUMENTS].count_documents({}) == 3


async def test_by_ref_is_owner_scoped(db):
    repo = DocumentRepository(db)
    await repo.insert(make(user_id="owner", ref="ext-1"))
    assert await repo.get_owned_by_ref("ext-1", "owner") is not None
    assert await repo.get_owned_by_ref("ext-1", "intruder") is None


async def test_by_ref_uses_the_index_not_a_collection_scan(db):
    await DocumentRepository(db).insert(make(ref="ext-1"))
    explain = await db[DOCUMENTS].find({"client_doc_ref": "ext-1", "user_id": "u1"}).explain()
    assert "client_doc_ref_unique" in index_names_used(explain)


# ---- listing -------------------------------------------------------------

async def test_list_is_newest_first_and_paginated(db):
    repo = DocumentRepository(db)
    docs = [make(minutes=i) for i in range(5)]
    for d in docs:
        await repo.insert(d)

    page1 = await repo.list_for_user("u1", status=None, page=1, page_size=2)
    page3 = await repo.list_for_user("u1", status=None, page=3, page_size=2)

    assert [d["_id"] for d in page1] == [docs[4]["_id"], docs[3]["_id"]]
    assert [d["_id"] for d in page3] == [docs[0]["_id"]]
    assert await repo.count_for_user("u1", status=None) == 5


async def test_list_filters_by_status_and_owner(db):
    repo = DocumentRepository(db)
    done = make()
    done["status"] = DocumentStatus.COMPLETED.value
    await repo.insert(done)
    await repo.insert(make())                     # queued
    await repo.insert(make(user_id="someone-else"))

    completed = await repo.list_for_user("u1", status=DocumentStatus.COMPLETED, page=1, page_size=10)
    assert [d["_id"] for d in completed] == [done["_id"]]


async def test_count_active_ignores_terminal_documents(db):
    repo = DocumentRepository(db)
    for status in ["queued", "processing", "enriching", "completed", "failed"]:
        d = make()
        d["status"] = status
        await repo.insert(d)
    assert await repo.count_active("u1") == 3
