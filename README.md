# Cohort Insights API

A FastAPI service where users submit text documents that go through a two-stage background pipeline: **processing** produces a summary, then **enriching** produces tags.

Documents can be edited while they are being processed, so the design centres on one question: *can a reader always tell which content produced the result they are looking at?*

The guarantees, each backed by tests:

- A reader can always tell which content version produced the result (`result_is_current`).
- Summary and tags can never come from different versions of the content.
- Concurrent workers never process the same stage twice, and a crashed worker's job is resumed.
- Repeated submissions with the same external reference are idempotent.
- The API keeps working when Redis is down; MongoDB is the source of truth.

```text
client ──HTTP──> api ──> MongoDB   documents: state + job queue
                  │
                  └────> Redis     rate limiter, content cache

worker(s) ──atomic claim / fenced writes──> MongoDB
```

**Stack:** Python 3.12 · FastAPI · MongoDB · Redis · pytest · Docker Compose (`api`, `worker`, `mongo`, `redis`).

---

## Quick start

Requirements: Docker only.

```bash
docker compose up --build                 # API on http://localhost:8000, interactive docs at /docs
docker compose run --rm tests             # full test suite inside Docker
docker compose up -d --scale worker=3     # optional: several worker processes
```

Submit a document and poll it:

```bash
curl -s -X POST localhost:8000/documents \
  -H 'X-User-Id: alice' -H 'Content-Type: application/json' \
  -d '{"user_id": "alice", "title": "Notes", "content": "MongoDB stores  reevery document."}'

curl -s localhost:8000/documents/<document_id> -H 'X-User-Id: alice'
```

Over roughly 15–35 seconds the status moves `queued → processing → enriching → completed`.

### Configuration

Every setting is an environment variable. A `.env` file is optional: `.env.example` documents all of them, and Compose sets the MongoDB and Redis URLs itself.

| Variable                               | Default | Meaning                                                                     |
| -------------------------------------- | ------- | --------------------------------------------------------------------------- |
| `WORKER_CONCURRENCY`                 | 2       | claim loops per worker process                                              |
| `LEASE_SECONDS`                      | 60      | how long a claim is exclusive; a crashed worker's job is retried after this |
| `MAX_ATTEMPTS`                       | 3       | attempts per stage before the document is marked `failed`                 |
| `RETRY_BASE_SECONDS`                 | 2       | retry backoff: 2 s, 4 s, ...                                                |
| `PROCESSING_MIN_SECONDS` / `_MAX_` | 10 / 20 | simulated processing duration                                               |
| `ENRICHING_MIN_SECONDS` / `_MAX_`  | 5 / 15  | simulated enriching duration                                                |
| `STAGE_FAILURE_RATE`                 | 0.1     | simulated failure chance per stage attempt                                  |
| `MAX_ACTIVE_PER_USER`                | 3       | rate limit: documents in progress per user                                  |
| `CACHE_TTL_SECONDS`                  | 86400   | content cache lifetime                                                      |

---

## API

| Method | Path                                                    | Success                             | Errors                            |
| ------ | ------------------------------------------------------- | ----------------------------------- | --------------------------------- |
| POST   | `/documents`                                          | 201 created · 200 idempotent retry | 401 403 409 422 429               |
| GET    | `/documents/{id}`                                     | 200 + `ETag`                      | 401 404                           |
| GET    | `/documents/by-ref/{client_doc_ref}`                  | 200 + `ETag`                      | 401 404                           |
| PATCH  | `/documents/{id}` (optional `If-Match`)             | 200 + `ETag`                      | 400 401 404 412 422 429           |
| GET    | `/users/{user_id}/documents?page=&page_size=&status=` | 200                                 | 401 404 422                       |
| GET    | `/health`                                             | 200                                 | 503 when MongoDB or Redis is down |

- **One error format** for everything, including framework 404/405s:
  `{"error": {"code", "message", "request_id", "details"}}`.
- **Request ids:** `X-Request-ID` is echoed if the client sends one and generated otherwise. It is returned on every response, included in error bodies, and bound to every log line.
- **GET `/documents/{id}`** returns `status`, `failed_stage`, per-stage
  `{state, attempts, last_error}`, `content_version`, `content_hash`, the `result` with the version it came from, and `result_is_current`. It does not return the content text or internal fields (lease, scheduling, stage-1 hand-off): responses are built field by field, never from the stored document as a whole.
- **Validation:** strings are trimmed; title ≤ 200, content ≤ 100,000, `user_id` ≤ 64 and `client_doc_ref` ≤ 128 characters; identifiers allow only `A–Z a–z 0–9 . _ : -`; unknown fields are rejected (422). Validation errors list the field and reason but never echo the submitted value.
- **Listing:** newest first; `page_size` ≤ 100 (default 20), `page` ≤ 10,000; the response includes `total` and `has_next`; `?status=` filters.
- **PATCH** accepts `title` and/or `content`. `user_id` and `client_doc_ref` cannot be changed.

---

## Assumptions

- **Identity.** Authentication is out of scope, so the caller identifies itself with an `X-User-Id` header on every request without it the API returns 401. In production will add the single dependency (`get_current_user`) using the Depends would verify a token instead, and nothing else would change.
- **Acting as someone else → 403.** If the `user_id` in a POST body differs from the header, the request is refused. raise the forbiddenError
- **Someone else's document → 404, not 403.** A missing id, a malformed id and another user's id all return an identical 404, so document ids cannot be probed. Listing another user's documents is also 404.
- **Content is trimmed before hashing**, so leading or trailing whitespace is not a new version.
- **`client_doc_ref` is optional** and globally unique when present.
- **PATCH on a `failed` document requeues it**: that is how a user retries.
- **Without `If-Match`, the last write wins.**
- **The stages are simulated, but their outputs are real.** The summary is the first 30 words of the content; tags are the five most frequent words of four or more letters in the summary, excluding common words, with ties broken alphabetically. The outputs are deterministic on purpose: the content cache is only correct if the same content always gives the same result.

---

## Schema & Staleness Design

### The document

| Field                                       | Purpose                                                                                                                                      |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `_id`                                     | ObjectId generated in the app before the insert, so a rate-limit slot can be reserved for it                                                 |
| `user_id`, `title`                      | owner and label                                                                                                                              |
| `client_doc_ref`                          | optional external id; the field is omitted (not null) when absent                                                                            |
| `content`, `content_hash`               | the text and its SHA-256                                                                                                                     |
| `content_version`                         | starts at 1, +1 on every content change. **The number everything else is compared against.**                                           |
| `status`                                  | `queued` / `processing` / `enriching` / `completed` / `failed`; stored so it can be indexed                                        |
| `failed_stage`                            | `null`, or the stage that ran out of attempts                                                                                              |
| `stages.processing`, `stages.enriching` | `{state, attempts, last_error}` for each stage                                                                                             |
| `stage1_output`                           | `{content_version, summary}`: the hand-off from stage 1 to stage 2                                                                         |
| `result`                                  | `{content_version, content_hash, summary, tags, completed_at}`                                                                             |
| `next_attempt_at`                         | when any worker may next claim the document: the lease expiry while claimed, the backoff time while waiting to retry, `null` once finished |
| `lease_id`                                | which worker run currently owns the document                                                                                                 |
| `created_at`, `updated_at`              | timestamps                                                                                                                                   |

### How a reader knows which content produced the result

- `content_version` is the document's current version; `result.content_version` is the version the result was computed from.
- GET returns both, plus `result_is_current = (result.content_version == content_version)`.
- This is a comparison of two stored numbers, not a stored flag. A flag would have to be updated correctly on every write path; a comparison cannot drift out of sync.
- After a PATCH the previous result stays visible, still labelled with its old version, so the reader sees the last good output clearly marked as stale rather than nothing at all.

### Why a mixed summary/tags result is impossible (not just unlikely)

1. **Written together.** Summary and tags live in one subdocument and are written in one `$set`.
   A single-document write in MongoDB is atomic: a reader sees all of it or none of it.
2. **Fenced writes.** Every worker write filters on `{_id, lease_id, content_version}`. If the content changed mid-run, the write matches nothing and is discarded.
3. **Tags only from the matching summary.** Enriching only uses a `stage1_output` whose `content_version` equals the document's current one.
4. **PATCH resets the hand-off atomically.** The same update that bumps the version also clears `stage1_output` and the lease, so no worker can continue from the old version's state.

### Walkthrough: a PATCH arrives while enriching is running

1. A worker has claimed the document at version 1 (lease `A`) and is computing tags from the v1 summary.
2. The PATCH applies one atomic update: new content and hash, `content_version` 2, both stages reset, `stage1_output` and `lease_id` cleared, status `queued`. The existing `result` is kept, still labelled v1.
3. A reader polling now sees the v1 result with `result_is_current: false`.
4. Any worker can claim version 2 immediately, with a new lease.
5. The v1 worker finishes and writes with the filter `{_id, lease_id: A, content_version: 1}`. It matches zero documents, so the output is discarded and logged as `stale_write_discarded`.
6. The v2 run publishes summary and tags together, labelled v2, and `result_is_current` becomes true.

Tested by `test_patch_during_enriching_never_publishes_mixed_versions` (and the same scenario during processing, and between a claim and the stage start).

### PATCH semantics

- **New content** (different hash): one atomic update bumps the version, resets the pipeline and requeues the document.
- **Same content** (same hash): no-op. The hash check is part of the update's filter, so even two identical concurrent PATCHes bump the version only once.
- **Title only:** renamed in place; no version bump and no reprocessing, because the title is not pipeline input.
- **Optimistic concurrency:** GET and PATCH responses carry `ETag: "<content_version>"`. A client that sends it back as `If-Match` gets 412 if someone else changed the content first. The check ispart of the update's filter, so check-and-write cannot be interleaved. Five concurrent PATCHes with the same `If-Match` produce exactly one 200.
- Simplification: the ETag tracks the content version only, so a title rename does not change it.

---

## Pipeline & worker

```text
          claim (atomic, takes a lease)
queued ───────────────► processing ──ok──► enriching ──ok──► completed
                          │   ▲                │   ▲
                    fail  │   │ retry after    │   │ retry after
                          ▼   │ 2 s, 4 s       ▼   │ backoff
                       (waiting)            (waiting)
                          │                    │
             3rd failure  ▼                    ▼  3rd failure
                       failed (failed_stage = "processing" | "enriching")
```

- **MongoDB is the job queue.** The job state already lives on the document, so the claim and the fenced writes act on the same record, there is one source of truth, and the pipeline keeps running when Redis is down.
- **Each stage is a separate job.** When processing succeeds the document is released and is due immediately; enriching is claimed as its own job, by any worker.
- **Atomic claim.** One `find_one_and_update` on `{status active, next_attempt_at <= now}`, oldest first, sets a new `lease_id` and pushes `next_attempt_at` to the lease expiry. When several workers race for the same document, exactly one wins.
- **The lease is the crash recovery.** Because `next_attempt_at` doubles as the lease expiry, a crashed worker's job simply becomes due again after 60 s and is claimed with a new lease; if the old worker comes back, its writes are fenced out. Trade-off: recovery can take up to the lease length. The lease must be longer than the longest stage, which the settings validation enforces.
- **Retries.** Three attempts per stage with exponential backoff (2 s, then 4 s), then `failed` with `failed_stage` set. An enriching retry reuses the saved summary, so processing is never redone. Attempts are counted when a stage *starts*, so a crash during the final attempt counts as a failure: a document that crashes the worker cannot be retried forever.
- **Shutdown.** SIGTERM lets the current stage finish before the process exits (Compose allows 30 s); a crashed worker is restarted by Compose.
- **Scaling.** `WORKER_CONCURRENCY` claim loops per process, and `docker compose up --scale worker=N` for more processes. Both are safe because every loop uses the same atomic claim.

---

## Crosswalk (`client_doc_ref`)

A client can attach its own external id to a submission. Repeated submissions with the same ref follow this policy:

| Situation                              | Response                                                               |
| -------------------------------------- | ---------------------------------------------------------------------- |
| New ref                                | 201, new document                                                      |
| Same ref, same user, same content      | 200 with the existing document (idempotent retry)                      |
| Same ref, same user, different content | 409 with `details.existing_document_id`; PATCH that document instead |
| Same ref, another user                 | 409 with no details                                                    |

- **Enforced by a unique partial index** that only covers documents that have the field, so any number of documents can have no ref.
- **Race-safe.** The service looks the ref up first, but that is only an optimisation; the unique index is the guarantee. If two requests both miss the lookup, one insert wins and the other gets
  `DuplicateKeyError`, re-reads the winner's document and applies the same policy. Five concurrent identical submits create exactly one document.
- **Why 409 instead of "treat it as a new version":** deliveries can arrive out of order, and an older delivery must never overwrite newer content. Rejecting is deterministic, and the 409 tells the client which document to PATCH.
- **The idempotency key is ref + content hash.** A different title on a retry is not a conflict.
- **Trade-off:** because refs are globally unique, a 409 for another user's ref reveals that the ref is in use (never who owns it or which document it is). Per-user uniqueness would remove that, at the cost of "one external id = one document".
- `GET /documents/by-ref/{ref}` is owner-scoped and served by the unique index; a test checks the query plan with `explain()`.

---

## Rate limiting

Each user may have at most **3 documents in progress** (`queued`, `processing` or `enriching`) at once. A 4th submission gets **429** (`rate_limited`) and nothing is stored.

### How it works

- Redis key `active:{user_id}` is a **set of document ids**, not a counter. It is a sorted set: the score is when the slot was reserved.
- One **Lua script** does the check and the add atomically: if this document already holds a slot, keep it; otherwise, if the user has fewer than 3, add it; otherwise refuse. Redis runs a script without interleaving other commands, so concurrent requests cannot all pass the check.
- The slot is reserved for the document's id **before** the insert. If the insert does not happen (duplicate `client_doc_ref`, MongoDB error, cancelled request), the slot is given back.
- The worker releases the slot when the document reaches `completed` or `failed`. If its final write was fenced out because a PATCH requeued the document, it keeps the slot, because the document is still in progress.

### Why a set and not INCR/DECR

A counter drifts: a retried request increments twice, and a crash between finishing and decrementing loses a decrement. With a set of ids, adding the same id twice or removing it twice changes nothing, and the count cannot go below zero. The same property means a **PATCH on a document that is already in progress does not count twice**: it re-acquires the slot it already holds.

### Order inside `POST /documents`

1. Crosswalk retry (same ref, same content) → 200, never touches the limiter, so a retry succeeds even when the user is at the limit.
2. Content cache hit → stored as `completed` immediately; needs no slot.
3. Rate limit → reserve a slot, or 429.
4. Insert.

### Self-healing

If a release is missed (for example the worker could not reach Redis), that slot would block the user until the key expires. So when a user is refused, the limiter checks reservations **older than 60 s** against MongoDB, removes those whose document is no longer in progress, and tries once more. Younger reservations are never removed, because their insert may still be in flight. This
runs only when a request is refused, so normal requests pay nothing for it.

---

## Content cache

Summary and tags depend only on the content, so content that has already been processed does not need to be processed again.

- Redis key `cache:result:{content_hash}` → `{"summary", "tags"}`, 24 h TTL. Written only when a result is actually published.
- **On submit** it is checked before the rate limiter. A hit stores the document as `completed` straight away, with the result labelled with *this* document's version and hash, and both stages shown as succeeded with 0 attempts ("done, but not run here").
- **On PATCH** the worker checks the cache when it claims the job and, on a hit, publishes the result with the same fenced write as a normal completion. This keeps one code path for everything that enters the queue. Trade-off: the document shows `queued` for about one poll interval (1 s), and the PATCH needs a free slot.
- **Keyed by content hash, not document id:** a PATCH changes the hash, so a document can never be served its old content's result, and nothing ever needs invalidating.
- Correct only because the stage outputs are deterministic; a real model with non-deterministic output would need different cache semantics.
- A corrupt cache entry is logged and treated as a miss.

---

## When Redis is down

Redis is an optimisation; MongoDB is the source of truth. The API keeps working, and every fallback is logged as a warning.

| Feature             | Fallback                                                                                                        | Log event                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| Rate limit: reserve | Count in-progress documents in MongoDB (`user_status_created` index)                                          | `rate_limit_fallback`       |
| Rate limit: release | Skipped; the self-healing step removes the stale slot later                                                     | `rate_limit_release_failed` |
| Cache: read         | Look up `result.content_hash` in MongoDB (`result_content_hash` index); if that fails too, process normally | `cache_redis_unavailable`   |
| Cache: write        | Skipped; costs only a future cache hit                                                                          | `cache_write_failed`        |

`/health` reports `degraded` with 503 so a load balancer or orchestrator can see it. During an outage the rate limit is **soft**: counting in MongoDB and then inserting is not atomic, so concurrent requests could briefly exceed it.

---

## Indexes

Each index exists for a named query; all are created idempotently at startup.

| Index                     | Keys                                               | Serves                                                   |
| ------------------------- | -------------------------------------------------- | -------------------------------------------------------- |
| `user_created`          | `user_id, created_at -1, _id -1`                 | listing without a status filter, already in sort order   |
| `user_status_created`   | `user_id, status, created_at -1, _id -1`         | listing with `?status=`; the rate-limit fallback count |
| `client_doc_ref_unique` | `client_doc_ref` (unique, partial: field exists) | crosswalk uniqueness and the by-ref lookup               |
| `result_content_hash`   | `result.content_hash`                            | content cache fallback when Redis is down                |
| `claim_queue`           | `status, next_attempt_at`                        | the worker's claim query                                 |

`_id` is the tie-breaker in both listing indexes, so documents created in the same millisecond still have a stable page order.

---

## At 100×

<!-- TODO(me): rewrite this section fully in my own words before submitting. -->

**A user with 500K documents.** Queries that start with `user_id` still use the indexes, so single reads and the first pages stay fast. What degrades is deep pagination: `skip` walks every skipped index entry, so page 10,000 costs far more than page 1. `count_documents` for `total` scans the user's whole index range on every request. A `status` filter narrows little, because it has only
five values. One very large user also becomes a hot spot for writes and for the worker queue.

**Shard key: `{user_id: 1, _id: 1}`, not `user_id` alone.** With `user_id` alone, all of one large user's documents share a single shard key value, so they form one chunk that cannot be split, and that shard becomes hot. The compound key keeps a user's listing on a single shard while still letting the chunk split. Two consequences: the by-ref lookup no longer contains the shard key, so
it becomes a scatter-gather query; and MongoDB only enforces unique indexes that include the shard key, so `client_doc_ref` uniqueness can no longer be enforced on the documents collection. The fix for both is a separate crosswalk collection, `doc_refs {_id: client_doc_ref, user_id, document_id}`, sharded on the hashed ref: uniqueness comes from `_id`, and the lookup targets one shard.

**The Redis rate limiter at 100× traffic.** Each check is O(1) on one key, so Redis Cluster spreads users across nodes and scales horizontally. What remains: a very active user's key is a hot key; every submit pays a network round-trip; and a failover can lose recent writes, so the sets drift from
reality. The self-healing step and a periodic rebuild from MongoDB repair drift; at the gateway level the limit could be enforced before requests reach the API; during failover a soft limit is acceptable, since MongoDB remains the source of truth.

**Pagination.** Replace `skip`/`limit` with keyset (cursor) pagination on `(created_at, _id)`: the client sends an opaque cursor and the query continues with `created_at < last` (ties broken by `_id`),
which costs the same on every page. Exact totals would be dropped or replaced with an estimate.

**The worker queue.** Claiming by polling MongoDB is simple and correct but adds load as workers multiply; at scale I would wake workers with change streams or move job dispatch to a dedicated queue, and add lease heartbeats so leases can be short and crash recovery fast.

---

## Testing

```bash
docker compose run --rm tests        # everything, inside Docker
python -m pytest                     # locally, with MongoDB and Redis running
python -m pytest --cov=app --cov-report=term-missing
```

- **143 tests, ~94% line coverage.** Unit tests cover validation, hashing, ETag parsing and the stage functions; integration tests run against **real MongoDB and Redis** — the databases are never mocked.
- **Isolated from dev data:** tests use the `cohort_test` database and Redis DB 15.
- **Deterministic:** zero stage delays, scripted stage failures, an injectable clock (time only moves when a test says so) and an injectable random source.
- **CI:** GitHub Actions runs lint and the tests through the same `docker compose` file.

Notable tests:

| Scenario                                                         | Proves                                                                |
| ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| 10 simultaneous claims on one document                           | exactly one worker wins                                               |
| 3 worker loops × 10 documents                                   | every stage runs exactly once                                         |
| PATCH mid-processing, mid-enriching, and between claim and start | stale output is never published; summary and tags share a version     |
| Expired lease                                                    | the job is reclaimed and the old worker's write is fenced out         |
| Enriching fails, then succeeds                                   | processing is not re-run                                              |
| Repeated failures                                                | retries stop at 3 with the correct `failed_stage`                   |
| 5 concurrent PATCHes with the same `If-Match`                  | exactly one 200, the rest 412                                         |
| 5 concurrent identical crosswalk submits                         | exactly one document                                                  |
| 10 concurrent submits                                            | exactly 3 accepted, 7 get 429                                         |
| Redis down                                                       | rate limit, cache, worker and `/health` all degrade without failing |

`app/worker/runner.py` (process wiring: signals, connections, task group) is not covered by unit tests; the logic it calls is, and running the stack in Compose exercises the wiring.

---

## What I would do with more time

- **Lease heartbeats**, so leases can be short and crash recovery fast even for long stages.
- **Jitter on retry backoff**, so failed jobs don't retry in lockstep.
- **Keyset pagination** and approximate totals (see At 100×).
- **Metrics:** queue depth, stage latency, failure and retry rates, cache hit rate.
- **A view of failed documents** (dead-letter style) for operators.
- **Crosswalk `source_version`:** let partners send a version so the newest delivery wins instead of a 409.
- **`Idempotency-Key` header** for retry-safe submissions without a `client_doc_ref`.
- **Close the release race:** a PATCH landing in the milliseconds between the worker's final write and its slot release can let a user briefly exceed the limit by one; a Lua release that only removes a slot reserved before the final write would close it.
- **`Retry-After` on 429** and cache stampede protection.
- **Real authentication** replacing the `X-User-Id` header.
