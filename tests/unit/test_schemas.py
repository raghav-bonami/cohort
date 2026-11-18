from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.documents import new_document
from app.schemas.documents import DocumentCreate, DocumentResponse


def body(**overrides):
    return {"user_id": "alice", "title": "T", "content": "C", **overrides}


def test_strings_are_trimmed():
    data = DocumentCreate(**body(title="  T  ", content="  text  ", client_doc_ref=" ref-1 "))
    assert (data.title, data.content, data.client_doc_ref) == ("T", "text", "ref-1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": "   "},                 # whitespace only = empty
        {"title": ""},
        {"content": "x" * 100_001},          # over MAX_CONTENT_CHARS
        {"client_doc_ref": "a/b"},           # "/" would break the by-ref path
        {"client_doc_ref": "a b"},
        {"user_id": "alice!"},
        {"status": "completed"},             # unknown field (extra="forbid")
    ],
)
def test_invalid_input_is_rejected(overrides):
    with pytest.raises(ValidationError):
        DocumentCreate(**body(**overrides))


def test_response_never_exposes_internal_fields():
    doc = new_document(
        user_id="alice", title="T", content="secret text", client_doc_ref=None,
        now=datetime.now(UTC),
    )
    dumped = DocumentResponse.from_doc(doc).model_dump()
    for internal in ("content", "lease_id", "next_attempt_at", "stage1_output", "user_id"):
        assert internal not in dumped
    assert dumped["result_is_current"] is False
    assert dumped["client_doc_ref"] is None