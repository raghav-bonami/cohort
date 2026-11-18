from datetime import UTC, datetime

from app.domain.documents import is_result_current, new_document

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make(**overrides):
    args = {"user_id": "u1", "title": "t", "content": "c", "client_doc_ref": None, "now": NOW}
    return new_document(**{**args, **overrides})


def test_new_document_starts_queued_at_version_1():
    doc = make()
    assert doc["status"] == "queued"
    assert doc["content_version"] == 1
    assert doc["stages"]["processing"]["state"] == "pending"
    assert doc["stages"]["enriching"]["state"] == "pending"
    assert doc["result"] is None
    assert doc["next_attempt_at"] == NOW


def test_missing_ref_is_omitted_not_null():
    # Required for the partial unique index to allow many ref-less documents.
    assert "client_doc_ref" not in make(client_doc_ref=None)
    assert make(client_doc_ref="ext-1")["client_doc_ref"] == "ext-1"


def test_result_is_not_current_when_absent():
    assert is_result_current(make()) is False


def test_result_is_current_only_when_versions_match():
    doc = make()
    doc["result"] = {"content_version": 1, "summary": "s", "tags": ["a"]}
    assert is_result_current(doc) is True

    doc["content_version"] = 2  # what a PATCH does
    assert is_result_current(doc) is False
