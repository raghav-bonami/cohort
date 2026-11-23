from bson import ObjectId

from app.db.ids import parse_object_id


def test_valid_id_parses():
    oid = ObjectId()
    assert parse_object_id(str(oid)) == oid


def test_uppercase_hex_is_accepted():
    oid = ObjectId()
    assert parse_object_id(str(oid).upper()) == oid


def test_malformed_ids_return_none():
    for bad in ["", "abc", "z" * 24, "a" * 23, "a" * 25, "a" * 12]:
        assert parse_object_id(bad) is None, bad


def test_trailing_newline_is_rejected():
    # The reason fullmatch() is used instead of ^...$
    assert parse_object_id(str(ObjectId()) + "\n") is None
