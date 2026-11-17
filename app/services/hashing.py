"""Content fingerprinting.

The hash is the cache key and is stored next to the result, so a reader
can see exactly which content produced it.
"""

import hashlib


def content_hash(content: str) -> str:
    """Return the sha256 hex digest of the content exactly as stored.

    No normalisation happens here on purpose: validation trims the text
    once before storage, and the hash must describe the stored bytes exactly.
    If this function changed text, two different stored contents could share
    a hash, and the cache could hand back the wrong result.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
