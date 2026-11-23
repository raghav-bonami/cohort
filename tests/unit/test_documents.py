from datetime import UTC, datetime

from app.domain.documents import content_change_fields, is_result_current, new_document

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_content_change_resets_pipeline_but_keeps_result():
    now = datetime.now(UTC)
    fields = content_change_fields(content="new", now=now)
    assert "result" not in fields  # old result stays, visibly stale
    assert "content_version" not in fields  # bumped with $inc, not set
    assert fields["status"] == "queued"
    assert fields["stage1_output"] is None and fields["lease_id"] is None
    assert fields["stages"]["processing"] == {"state": "pending", "attempts": 0, "last_error": None}
    assert fields["next_attempt_at"] == now


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
