## Schema & Staleness Design

### The document
(paste the field list from app/domain/documents.py, one line per field)

### How a reader knows which content produced the result
- `content_version` starts at 1 and every PATCH increments it.
- `result` carries its own `content_version` and `content_hash`.
- `GET` returns both numbers plus `result_is_current = (result.content_version == content_version)`.
  It's a comparison of two stored values, not a flag someone must remember to update.

### Why a mixed summary/tags result is impossible (not just unlikely)
1. **Written together.** Summary and tags live in one subdocument and are written in one `$set`.
   A single-document write in MongoDB is atomic: a reader sees all of it or none of it.
2. **Fenced writes.** Every worker write filters on `{_id, content_version, lease_id}`.
   If a PATCH bumped the version mid-run, the write matches nothing and is discarded.
3. **Tags only from the matching summary.** Stage 2 only uses `stage1_output` whose
   `content_version` equals the document's current one.

### Walkthrough: PATCH arrives while enriching is running
(step by step: worker holds v2 → PATCH sets v3, clears lease_id, resets stages →
worker finishes, tries to write with content_version=2 → 0 matched → discarded and logged →
reader meanwhile sees result v2 with result_is_current=false → new run publishes v3)

## Rate limiting

Each user may have at most **3 documents in progress** (`queued`, `processing` or
`enriching`) at once. A 4th submission gets **429** (`rate_limited`) and nothing is stored.
The limit is configurable with `MAX_ACTIVE_PER_USER`.

### How it works
- Redis key `active:{user_id}` is a **set of document ids**, not a counter. It's a sorted set:
  the score is when the slot was reserved.
- One **Lua script** does the check and the add atomically: if this document already holds a
  slot, keep it; else if the user has fewer than 3, add it; else refuse. Redis runs a script
  without interleaving other commands, so concurrent requests can't all pass the check.
- The slot is reserved for the document's id **before** the insert (the id is generated in
  the app). If the insert doesn't happen (duplicate `client_doc_ref`, Mongo error,
  cancelled request), the slot is given back.
- The worker releases the slot when the document reaches `completed` or `failed`. If its
  final write was fenced out (a PATCH requeued the document), it keeps the slot, because the
  document is still active.

### Why a set and not INCR/DECR
A counter drifts: a retried request increments twice, and a crash between work and decrement
loses a decrement. With a set of ids, adding an id twice or removing it twice changes
nothing, and the count can't go below zero. The same property means a **PATCH on a document
that is already in progress doesn't count twice**: it re-acquires the slot it already holds.

### Ordering in `POST /documents`
1. Crosswalk retry (same `client_doc_ref`, same content) → 200, never touches the limiter,
   so a retry succeeds even when the user is at the limit.
2. Content cache hit → stored as `completed` immediately, needs no slot.
3. Rate limit → reserve a slot or 429.
4. Insert.

### Self-healing
If a release is missed (for example the worker couldn't reach Redis), that slot would block
the user until the key expires. So when a user is refused, the limiter checks reservations
**older than 60 s** against Mongo and removes those whose document is no longer active, then
tries once more. Younger reservations are never removed: their insert may still be in flight.
This runs only on the refusal path, so normal requests pay nothing for it.

## Content cache

Summary and tags depend only on the content, so content that has already been processed
doesn't need processing again.

- Redis key `cache:result:{content_hash}` → `{"summary", "tags"}`, 24 h TTL
  (`CACHE_TTL_SECONDS`). Written only when a result is actually published.
- **On submit:** checked before the rate limiter. A hit stores the document as `completed`
  straight away, with the result labelled with *this* document's version and hash, and stages
  shown as succeeded with 0 attempts ("done, not run here").
- **On PATCH:** the worker checks the cache when it claims the job and, on a hit, publishes
  the result with the same fenced write as a normal completion. This keeps one code path for
  everything that enters the queue. Trade-off: the document shows `queued` for about one poll
  interval, and the PATCH needs a free slot.
- **Why keyed by content hash, not document id:** a PATCH changes the hash, so a document
  can never be served its old content's result, and nothing ever needs invalidating.
- This is only correct because the stage outputs are deterministic. A real model with
  non-deterministic output would need the cache semantics revisited.
- A corrupt cache entry is logged and treated as a miss.

## When Redis is down

Redis is an optimisation; MongoDB is the source of truth. The API keeps working, and every
fallback is logged as a warning.

| Feature | Fallback | Log event |
|---|---|---|
| Rate limit: reserve | Count active documents in Mongo (`user_status_created` index) | `rate_limit_fallback` |
| Rate limit: release | Skip; the reconciler removes the stale slot later | `rate_limit_release_failed` |
| Cache: read | Look up `result.content_hash` in Mongo (`result_content_hash` index); if that fails too, process normally | `cache_redis_unavailable` |
| Cache: write | Skip; costs only a future cache hit | `cache_write_failed` |

During an outage the limit is **soft**: counting in Mongo then inserting isn't atomic, so
concurrent requests could briefly exceed it.

## Known limits (rate limit and cache)

- A PATCH that lands in the milliseconds between the worker's final write and its release
  can leave a requeued document without a slot, so the user could briefly have one document
  more than the limit. A fix: release with a Lua script that only removes a slot reserved
  before the terminal write.
- 429 responses have no `Retry-After` header; when a slot frees up depends on processing time.
- No stampede protection: two identical new documents submitted at the same moment are both
  processed. The second completes from the cache only if the first finished before the worker
  claimed it.

Tests: [tests/integration/test_rate_limit.py](tests/integration/test_rate_limit.py) and
[tests/integration/test_cache.py](tests/integration/test_cache.py), including concurrency and
Redis-down cases.
