"""Turning user-supplied ids into ObjectIds without leaking 500s."""

import re

from bson import ObjectId

# Exactly 24 hex characters. fullmatch() is used instead of ^...$ because "$"
# also matches just before a trailing newline, so "<24 hex>\n" would slip through.
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{24}")


def parse_object_id(value: str) -> ObjectId | None:
    """Return an ObjectId, or None if the value cannot be one.

    Callers treat None as "not found" (404), so a malformed id is
    indistinguishable from a missing one, and never becomes a 500.
    """
    if not _OBJECT_ID.fullmatch(value):
        return None
    return ObjectId(value)
