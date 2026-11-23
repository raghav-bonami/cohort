"""ETag / If-Match: optimistic concurrency for PATCH.

The ETag is the document's content_version, quoted as HTTP requires: "3".
A client reads a document, keeps the ETag, and sends it back as If-Match when it
PATCHes. If someone else changed the content in between, the versions differ and the
PATCH is refused with 412 instead of silently overwriting their change.

Simplification (README): the ETag tracks the content version only, because content is
what PATCH conflicts are about. A title rename does not change it.
"""

import re
from typing import Any

from app.core.errors import BadRequestError

# A version number, either bare (3) or quoted ("3"). [0-9], not \d: \d also matches
# non-ASCII digits. Weak ETags (W/"3") and lists ("1", "2") are not supported.
_IF_MATCH = re.compile(r'([0-9]{1,9})|"([0-9]{1,9})"')


def etag_for(doc: dict[str, Any]) -> str:
    return f'"{doc["content_version"]}"'


def parse_if_match(value: str | None) -> int | None:
    """Expected content_version from If-Match, or None when there is no precondition."""
    if value is None:
        return None
    value = value.strip()
    if value == "*":
        # "*" means "any current representation": the document only has to exist,
        # which the ownership lookup already enforces (404 otherwise).
        return None
    match = _IF_MATCH.fullmatch(value)
    if match is None:
        raise BadRequestError('If-Match must be a content_version, e.g. "3"')
    return int(match.group(1) or match.group(2))
