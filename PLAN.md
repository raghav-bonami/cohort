# Cohort Insights API: Implementation Plan

This is the plan for the take-home assignment. The interviewer says they're mostly grading the design: staleness, crosswalk, race conditions, and the "At 100×" section. So the order is: decide the design first, then write code, then write tests that prove the design works. Each decision comes with a short reason, because you'll have to defend all of it live.

---

## Part A: Design decisions (settle these before coding)

### A1. Stack

| Concern | Choice | Why |
|---|---|---|
| API | FastAPI + Pydantic v2 + `pydantic-settings` | Required; settings come from environment variables |
| Mongo driver | `pymongo` async (`AsyncMongoClient`) | Motor is deprecated; PyMongo's native async client replaces it |
| Redis | `redis.asyncio` | Used for rate limiting and the content cache |
| Worker | Separate container; MongoDB is the queue (atomic claim with a lease) | Mongo already holds the state, so there's no second source of truth. It still works if Redis is down, and `--scale worker=3` is an easy way to show race handling |
| Logging | `structlog`, JSON output | Structured logs |
| Tests | `pytest`, `pytest-asyncio`, `httpx.AsyncClient` | Integration tests run against real Mongo and Redis in compose |
| Tooling | `uv` or `pip` + `ruff` | Keeps the codebase clean |

### A2. Identity and ownership

- There's no real auth, so the caller identifies itself with an **`X-User-Id` header** (FastAPI dependency `get_current_user`). Document this as an assumption.
- `POST` accepts `user_id` in the body, as the spec says. If the header is present and doesn't match, return 403.
- Every read query filters on **`{_id, user_id}` together in one query**. A missing document and someone else's document both return **404**, and since it's the same query either way, even the timing doesn't reveal which one it was.

### A3. Document schema (the core of the assignment)

```text
documents {
  _id: ObjectId
  user_id, title, client_doc_ref (optional)
  content, content_hash (sha256), content_version (int, starts at 1, +1 on each PATCH)
  status: queued | processing | enriching | completed | failed      # stored so it can be indexed
  failed_stage: null | "processing" | "enriching"
  stages: {
    processing: { state: pending|running|succeeded|failed, attempts, error, next_attempt_at }
    enriching:  { same shape }
  }
  stage1_output: { content_version, summary }        # internal: partial progress is kept
  result: { content_version, content_hash, summary, tags, completed_at }   # published only when both stages finish
  lease: { id, expires_at } | null                    # which worker owns the current stage
  created_at, updated_at
}
```

### A4. Staleness mechanism: why mixing is impossible, not just unlikely

Three guarantees work together:

1. **Summary and tags live in one subdocument (`result`) with one `content_version`.** They're written in a single `$set`, and MongoDB writes to a single document are atomic. A reader therefore can't see new tags next to an old summary, because they're never written separately.
2. **Every worker write is fenced.** The filter is `{_id, content_version: v, "lease.id": my_lease}`. If a PATCH has bumped the version to v+1 mid-run, the write matches 0 documents and the worker discards its output and logs it. Old content can't publish a result over new content.
3. **Enriching only reads `stage1_output` when `stage1_output.content_version == content_version`.** Tags can therefore only come from a summary of the same version.

A reader works it out with a comparison, not a guessed boolean: `result_is_current = (result.content_version == content_version)`. The `GET` response returns both numbers so the caller can check for themselves.

```json
{
  "document_id": "...",
  "status": "enriching",
  "content_version": 3,
  "failed_stage": null,
  "stages": {
    "processing": { "state": "succeeded", "attempts": 1 },
    "enriching":  { "state": "running",   "attempts": 1 }
  },
  "result": { "content_version": 2, "summary": "...", "tags": ["..."] },
  "result_is_current": false
}
```

### A5. PATCH semantics

- A single atomic update sets the new content and `content_hash`, does `$inc content_version`, resets both stages to `pending`, sets status to `queued`, and clears the lease. The old `result` stays, labelled with its old version, so it's visibly stale.
- **Optimistic concurrency (bonus):** the client may send `If-Match: <content_version>`. The update filter then includes `content_version: expected`; if it matches nothing, return **412**. Without the header, the last write wins (documented).
- If the new content has the same hash, it's a no-op: return 200 without reprocessing.
- PATCH re-enters the pipeline, so it goes through the rate limiter, but a document that is already active doesn't count twice (see A7).

### A6. Crosswalk policy for `client_doc_ref` (choose it and write it down)

- Unique index on `client_doc_ref`, **partial**: only where it's a string. Many documents can have no ref.
- Repeat submissions:
  - **Same ref, same user, same content hash:** idempotent retry. Return **200** with the existing document; nothing new is created.
  - **Same ref, different content:** **409 Conflict**, with the existing `document_id` in the body. To change content, the partner must PATCH.
  - **Same ref, different user:** **409**, without revealing the owner.
- Why reject instead of "new version": deliveries can arrive out of order, so treating a repeat as a new version could let older content overwrite newer content. Rejecting is deterministic. Under "more time", mention accepting a partner-supplied `source_version` so that the highest version wins.
- Race: two identical submissions arriving at the same time both try to insert. The unique index makes one fail with `DuplicateKeyError`; catch it, re-read the existing document, and apply the policy above. Test this explicitly; it's a bonus item.
- `GET /documents/by-ref/{ref}` filters on `{client_doc_ref, user_id}`, so it uses the index and is still owner-scoped. Show that it uses the index with `explain()` in a test or in the README.

### A7. Redis use case 1: rate limiting

- Store a **Redis Set** per user, `active:{user_id}`, holding the IDs of active documents. Don't use a plain counter.
- A Lua script atomically does: "if the doc is already in the set, allow; else if the set has fewer than 3 members, add it; otherwise return 429."
- When a document reaches completed or failed, the worker does `SREM`. A Set is idempotent: removing twice is harmless and no counter drifts below zero. That's the argument for it over INCR/DECR.
- The key gets a TTL as a safety net. A reconciler can rebuild the set from Mongo.
- **If Redis is down:** fall back to `count_documents({user_id, status ∈ active})` on Mongo, using the `(user_id, status)` index. Log a warning. Mongo is the source of truth anyway.

### A8. Redis use case 2: content cache

- The key is `cache:result:{content_hash}` and holds `{summary, tags}` as JSON, with a 24h TTL. It's written only when a pipeline completes.
- On submit or PATCH, check the cache **before** the rate limiter. On a hit, write `result` with the current `content_version`, set status to `completed`, and skip the queue.
- The key is the content hash, never the `document_id`, so PATCH can't leak stale entries.
- **If Redis is down:** fall back to a Mongo lookup on `result.content_hash` (that's the "content hash index" they ask for). If that also fails, just process normally. Never `except: pass`; log every fallback.

### A9. Worker pipeline and race handling

- **Claim:** `find_one_and_update({status:"queued", next_attempt_at<=now} OR expired lease, ...)` atomically sets `running` plus a new lease. Only one worker can win.
- Running `processing` sleeps 10–20s and fails about 10% of the time; `enriching` sleeps 5–15s, also ~10%. Delays and failure rate come from env variables so tests can run with 0s delays and a forced failure rate.
- **Retry with backoff (bonus):** the stage's `attempts` counter goes up and `next_attempt_at = now + base·2^n`. After max attempts, set `status=failed` and `failed_stage=<stage>`.
- A failure in `enriching` retries only enriching; `stage1_output` is kept.
- If a worker crashes, its lease expires and another worker reclaims the job.
- Each stage transition is a single fenced update (A4, guarantee 2).

### A10. Indexes (list these in the README)

| Index | Serves |
|---|---|
| `{user_id:1, created_at:-1, _id:-1}` | Listing, newest first |
| `{user_id:1, status:1, created_at:-1}` | Listing with status filter; rate-limit fallback |
| `{client_doc_ref:1}` unique, partial | by-ref lookup and uniqueness |
| `{"result.content_hash":1}` | Cache fallback |
| `{status:1, next_attempt_at:1}` | Worker claim query |

---

## Part B: Build phases, in order

### Phase 0: Environment and repo setup (Day 1, about 1 hour)

1. Install or check: Python 3.11+, Git, Docker Desktop (WSL2 backend on Windows), and optionally the GitHub CLI and MongoDB Compass for browsing data.
2. Use this folder (`C:\Users\bonami\Projects\cohort`), kept separate from `rag-pipeline`, then `git init`.
3. Set your git identity for this repo (`git config user.name` / `user.email`) so commits show the right author.
4. Create the GitHub repo, add the remote, and add `.gitignore` (Python, `.env`, `.venv`).
5. Create a venv and `pyproject.toml` with the dependencies.
6. You don't need to install MongoDB or Redis locally; both run in Docker.

### Phase 1: Skeleton and infrastructure (Day 1)

1. Folder layout:

   ```text
   app/
     main.py                 # app factory + lifespan (connect Mongo/Redis, create indexes)
     core/  config.py  logging.py  errors.py
     db/    mongo.py  redis.py  indexes.py
     api/   deps.py  routers/{documents,users,health}.py
     schemas/                # Pydantic request/response models
     repositories/document_repo.py
     services/{document_service,rate_limiter,content_cache,hashing}.py
     worker/{runner,stages,claim}.py
   tests/{unit,integration}/  conftest.py
   Dockerfile  docker-compose.yml  .env.example  README.md  Makefile
   ```

2. Settings class using `pydantic-settings`, plus `.env.example` with every variable documented.
3. JSON logging and a request-ID middleware.
4. `GET /health`, which pings Mongo and Redis and returns 200 or 503 with details for each.
5. `Dockerfile` and `docker-compose.yml` with services `api`, `worker`, `mongo`, `redis`, healthchecks, and `depends_on: condition: service_healthy`.
6. Done when `docker compose up --build` works and `/health` is green. **Commit.**

### Phase 2: Data layer (Day 1–2)

1. Domain enums for overall status and stage state.
2. Repository functions covering every query the app needs; the routers never touch Mongo directly.
3. Create indexes at startup (idempotent).
4. ObjectId handling: validate path IDs, and treat a malformed ID as 404, not 500.
5. Write the "Schema & Staleness Design" README section **now**. Writing it first catches design holes early. **Commit.**

### Phase 3: Core endpoints (Day 2)

1. `POST /documents`: validation (trimmed, non-empty, max lengths, `client_doc_ref` pattern), hashing, insert, **201**.
2. `GET /documents/{id}`: owner-scoped, returns the A4 response shape.
3. `GET /users/{user_id}/documents`: path `user_id` must equal the header user (else 404), plus `page`, `page_size` (bounded, e.g. ≤100), optional `status` filter, newest first.
4. A global exception handler that returns a consistent error body. **Commit.**

### Phase 4: Worker pipeline (Day 2–3)

1. Claim and lease logic, the two stage functions, fenced writes, retries with backoff, and status transitions.
2. Release the rate-limit slot when a document reaches a terminal state, and write the cache on completion.
3. Clean shutdown on SIGTERM.
4. Manual check: submit a document, poll it, watch it go queued → processing → enriching → completed. **Commit.**

### Phase 5: PATCH and staleness (Day 3)

1. Atomic version bump, `If-Match` → 412, no-op when the hash is the same.
2. Check that a PATCH during an in-flight run causes the old run's writes to be rejected by the fence. **Commit.**

### Phase 6: Crosswalk (Day 3)

1. Implement the A6 policy, handle `DuplicateKeyError`, and add the by-ref endpoint. **Commit.**

### Phase 7: Redis features (Day 3–4)

1. Lua script for the rate limiter, Mongo fallback, and 429 with a clear message (optionally a `Retry-After` header).
2. Content cache with TTL and Mongo fallback.
3. Test with Redis stopped (`docker compose stop redis`): the API should degrade, not crash. **Commit.**

### Phase 8: Tests (Day 4)

Run them in compose against real Mongo and Redis, with 0s delays and an injectable random source so outcomes are deterministic.

- **Staleness:** PATCH mid-run, then assert `result_is_current=false` and that the stale run's write is rejected by the fence. Also assert summary and tags always share the same version.
- **Optimistic concurrency:** two PATCHes with the same `If-Match`; one gets 200 and the other 412.
- **Crosswalk:** same-ref retry is idempotent; different content gets 409; a concurrent duplicate submit creates exactly one document; by-ref uses the index.
- **Ownership:** another user's document and a nonexistent one both give 404.
- **Rate limit:** the 4th active document gets 429; once one completes, the next submit works; PATCH on an already-active document doesn't double count.
- **Cache:** identical content completes immediately; after a PATCH, the old hash's result never shows up.
- **Pipeline:** a forced enrich failure doesn't rerun processing; retries stop at the max; `failed_stage` is correct.
- **Concurrency:** two workers claiming at once results in one winner.
- **Redis down:** fallbacks work.

Optionally add a GitHub Actions workflow that runs these. **Commit.**

### Phase 9: README and submission (Day 5)

1. Sections: overview, quick start (one command), API table with status codes, design decisions, assumptions, crosswalk policy, indexes, Redis degradation, **Schema & Staleness Design**, **At 100×**, testing, and what you'd do with more time.
2. Final check on a fresh clone: `docker compose up` works with nothing else installed.
3. Push, make sure the repo is accessible, and email the link.

---

## Part C: Outline for "At 100×" (write it in your own words)

1. **A user with 500K documents:** the B-tree indexes still work for queries that start with `user_id`. What breaks is deep `skip`, which walks every skipped index entry, and `count_documents` for totals, which scans the user's whole range. The status filter has few distinct values, so it narrows little. One "whale" user also becomes a hot spot.
2. **Shard key:** `{user_id: 1, _id: 1}` (or `created_at` instead of `_id`), **not `user_id` alone.** With `user_id` alone, all of a whale's documents share one key value, so they land in a jumbo chunk that can't be split and that shard gets hot. The compound key keeps listing queries targeted to one shard and still allows splitting. Two consequences:
   - The by-ref lookup becomes scatter-gather across shards.
   - Mongo only enforces unique indexes that include the shard key, so `client_doc_ref` uniqueness can't be enforced on the main collection anymore.

   The fix for both is a separate crosswalk collection `doc_refs {_id: client_doc_ref, user_id, document_id}`, sharded by hashed ref.
3. **Redis rate limiter at 100× QPS:** each check is O(1) on one key, so Redis Cluster spreads users across nodes. The remaining risks:
   - A whale user's key becomes hot.
   - Each call is a network round-trip.
   - Failover can lose recent writes, so the sets drift from reality.

   Changes: periodically rebuild the sets from Mongo, possibly enforce limits at the gateway, and accept a soft limit during failover.
4. **Pagination:** replace skip/limit with keyset (cursor) pagination on `(created_at, _id)`: `?cursor=<opaque>` with a filter `created_at < last`. Drop exact totals or make them approximate.

---

## Part D: Debrief checklist

Be ready to answer each of these without notes:

- Why summary and tags are one subdocument, and why the version-fenced write rules out a mixed result.
- What happens, step by step, when a PATCH lands while enriching is running.
- Why the rate limiter uses a Set instead of a counter, and what happens when Redis is down.
- Why 409 for a repeated ref with different content, and the alternative you'd consider.
- Why 404 rather than 403 for another user's document.
- Why `{user_id, _id}` as the shard key and not `user_id` alone.
