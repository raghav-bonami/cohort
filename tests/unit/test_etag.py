import pytest

from app.api.etag import etag_for, parse_if_match
from app.core.errors import BadRequestError


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("*", None), ('"3"', 3), ("3", 3), (' "12" ', 12)],
)
def test_parse_if_match_accepts(value, expected):
    assert parse_if_match(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "abc", 'W/"3"', '"1", "2"', '"-1"', '"3', "\u0663"],  # last: Arabic-Indic 3
)
def test_parse_if_match_rejects(value):
    with pytest.raises(BadRequestError):
        parse_if_match(value)


def test_etag_is_the_quoted_content_version():
    assert etag_for({"content_version": 4}) == '"4"'
