"""Worker against real MongoDB: no sleeping, failures on demand, time moved by hand."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.mongo import DOCUMENTS
from app.domain.documents import is_result_current, new_document, new_stage
from app.domain.enums import StageName
from app.repositories.document_repo import DocumentRepository, Fence
from app.services.hashing import content_hash
from app.worker.pipeline import Worker
from app.worker.stages import SimulatedStages, StageError, make_summary, make_tags

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SETTINGS = Settings(
    lease_seconds=60, max_attempts=3, retry_base_seconds=2, worker_poll_seconds=0.05,
    processing_min_seconds=0, processing_max_seconds=0,
    enriching_min_seconds=0, enriching_max_seconds=0,
)


class FakeClock:
    """Time only moves when a test says so."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ScriptedStages:
    """Stages double: fails the first N calls, counts calls, can run a hook mid-stage."""

    def __init__(self, *, fail_processing=0, fail_enriching=0, during_processing=None):
        self.fail = {"processing": fail_processing, "enriching": fail_enriching}
        self.calls = {"processing": 0, "enriching": 0}
        self.during_processing = during_processing

    async def processing(self, content: str) -> str:
        self.calls["processing"] += 1
        if self.during_processing is not None:
            await self.during_processing()
        if self.calls["processing"] <= self.fail["processing"]:
            raise StageError("scripted processing failure")
        return make_summary(content)

    async def enriching(self, summary: str) -> list[str]:
        self.calls["enriching"] += 1
        if self.calls["enriching"] <= self.fail["enriching"]:
            raise StageError("scripted enriching failure")
        return make_tags(summary)


async def insert_doc(db, content="mongo redis fastapi worker lease fencing retries"):
    doc = new_document(user_id="u1", title="t", content=content, client_doc_ref=None, now=T0)
    await db[DOCUMENTS].insert_one(doc)
    return doc["_id"]


def make_worker(db, stages, clock) -> Worker:
    return Worker(DocumentRepository(db), stages, SETTINGS, clock=clock)


async def load(db, oid):
    return await db[DOCUMENTS].find_one({"_id": oid})


# ---- happy path ------------------------------------------------------------

async def test_document_goes_through_both_stages(db):
    clock, stages = FakeClock(), ScriptedStages()
    worker = make_worker(db, stages, clock)
    oid = await insert_doc(db)

    assert await worker.run_once() is True  # processing
    doc = await load(db, oid)
    assert doc["status"] == "enriching"
    assert doc["stages"]["processing"] == {"state": "succeeded", "attempts": 1, "last_error": None}
    assert doc["stage1_output"]["content_version"] == 1
    assert doc["result"] is None and doc["lease_id"] is None

    assert await worker.run_once() is True  # enriching
    doc = await load(db, oid)
    assert doc["status"] == "completed"
    assert doc["result"]["summary"] == doc["stage1_output"]["summary"]
    assert doc["result"]["tags"] == make_tags(doc["result"]["summary"])
    assert doc["result"]["content_version"] == 1
    assert doc["result"]["content_hash"] == doc["content_hash"]
    assert is_result_current(doc)
    assert doc["next_attempt_at"] is None and doc["lease_id"] is None

    assert await worker.run_once() is False  # nothing left to do


async def test_real_simulated_stages_complete_with_zero_delay(db):
    settings = SETTINGS.model_copy(update={"stage_failure_rate": 0.0})
    worker = Worker(DocumentRepository(db), SimulatedStages(settings), settings, clock=FakeClock())
    oid = await insert_doc(db)
    await worker.run_once()
    await worker.run_once()
    assert (await load(db, oid))["status"] == "completed"


# ---- failures and retries --------------------------------------------------

async def test_enriching_failure_retries_only_enriching(db):
    clock, stages = FakeClock(), ScriptedStages(fail_enriching=1)
    worker = make_worker(db, stages, clock)
    oid = await insert_doc(db)

    await worker.run_once()  # processing succeeds
    await worker.run_once()  # enriching fails (attempt 1)
    doc = await load(db, oid)
    assert doc["status"] == "enriching"
    assert doc["stages"]["enriching"] == {
        "state": "pending", "attempts": 1, "last_error": "scripted enriching failure",
    }
    assert doc["next_attempt_at"] == T0 + timedelta(seconds=2)  # backoff 2 * 2**0

    assert await worker.run_once() is False  # not due yet
    clock.advance(2)
    assert await worker.run_once() is True

    doc = await load(db, oid)
    assert doc["status"] == "completed"
    assert stages.calls == {"processing": 1, "enriching": 2}  # processing never re-ran
    assert doc["stages"]["processing"]["attempts"] == 1


async def test_retries_stop_at_max_attempts_and_record_failed_stage(db):
    clock, stages = FakeClock(), ScriptedStages(fail_processing=99)
    worker = make_worker(db, stages, clock)
    oid = await insert_doc(db)

    for expected_delay in (2, 4):  # attempts 1 and 2 fail -> retry with backoff
        await worker.run_once()
        doc = await load(db, oid)
        assert doc["status"] == "processing"
        assert doc["next_attempt_at"] == clock.now + timedelta(seconds=expected_delay)
        clock.advance(expected_delay)

    await worker.run_once()  # attempt 3 fails -> terminal
    doc = await load(db, oid)
    assert doc["status"] == "failed"
    assert doc["failed_stage"] == "processing"
    assert doc["stages"]["processing"] == {
        "state": "failed", "attempts": 3, "last_error": "scripted processing failure",
    }
    assert doc["stages"]["enriching"]["state"] == "pending"
    assert doc["next_attempt_at"] is None

    clock.advance(3600)
    assert await worker.run_once() is False  # never picked up again
    assert stages.calls["processing"] == 3


# ---- races -----------------------------------------------------------------

async def test_concurrent_claims_have_exactly_one_winner(db):
    oid = await insert_doc(db)
    repo = DocumentRepository(db)
    claims = await asyncio.gather(
        *(
            repo.claim_next(now=T0, lease_id=f"lease-{i}", lease_until=T0 + timedelta(seconds=60))
            for i in range(10)
        )
    )
    winners = [c for c in claims if c is not None]
    assert len(winners) == 1
    assert (await load(db, oid))["lease_id"] == winners[0]["lease_id"]


async def test_patch_during_processing_discards_the_stale_output(db):
    clock = FakeClock()
    oid = await insert_doc(db, content="original content about mongo")
    new_content = "replacement content about redis"

    async def simulate_patch():
        # What PATCH (Phase 5) will do, applied while the worker is mid-stage.
        await db[DOCUMENTS].update_one(
            {"_id": oid},
            {
                "$set": {
                    "content": new_content,
                    "content_hash": content_hash(new_content),
                    "status": "queued",
                    "lease_id": None,
                    "next_attempt_at": clock.now,
                    "stage1_output": None,
                    "stages.processing": new_stage(),
                    "stages.enriching": new_stage(),
                },
                "$inc": {"content_version": 1},
            },
        )

    stages = ScriptedStages(during_processing=simulate_patch)
    worker = make_worker(db, stages, clock)

    await worker.run_once()  # ran on v1; PATCH landed mid-run; the v1 write is fenced out
    doc = await load(db, oid)
    assert doc["content_version"] == 2
    assert doc["stage1_output"] is None  # the v1 summary never landed
    assert doc["status"] == "queued"

    stages.during_processing = None
    await worker.run_once()
    await worker.run_once()
    doc = await load(db, oid)
    assert doc["status"] == "completed"
    assert doc["result"]["content_version"] == 2
    assert doc["result"]["content_hash"] == content_hash(new_content)
    assert doc["result"]["summary"] == make_summary(new_content)


# ---- crash recovery --------------------------------------------------------

async def test_expired_lease_is_reclaimed_and_old_holder_is_fenced_out(db):
    clock = FakeClock()
    oid = await insert_doc(db)
    repo = DocumentRepository(db)

    # Worker A claims and starts, then "crashes" (never finishes).
    claimed = await repo.claim_next(
        now=T0, lease_id="worker-a", lease_until=T0 + timedelta(seconds=60)
    )
    fence_a = Fence(oid, "worker-a", claimed["content_version"])
    assert await repo.start_stage(fence_a, StageName.PROCESSING, now=T0)

    worker_b = make_worker(db, ScriptedStages(), clock)
    assert await worker_b.run_once() is False  # lease still valid: nobody else may take it
    clock.advance(61)
    assert await worker_b.run_once() is True  # lease expired: B takes over

    doc = await load(db, oid)
    assert doc["status"] == "enriching"
    assert doc["stages"]["processing"]["attempts"] == 2  # A's attempt + B's

    # A wakes up and tries to publish its stale output: rejected.
    assert await repo.complete_processing(fence_a, summary="stale", now=clock.now) is False
    assert (await load(db, oid))["stage1_output"]["summary"] != "stale"


async def test_crash_on_final_attempt_fails_instead_of_retrying_forever(db):
    clock = FakeClock()
    oid = await insert_doc(db)
    await db[DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "processing",
                "stages.processing.state": "running",
                "stages.processing.attempts": 3,  # final attempt already started
                "lease_id": "dead-worker",
                "next_attempt_at": T0,  # its lease has expired
            }
        },
    )
    stages = ScriptedStages()
    await make_worker(db, stages, clock).run_once()

    doc = await load(db, oid)
    assert doc["status"] == "failed"
    assert doc["failed_stage"] == "processing"
    assert doc["stages"]["processing"]["last_error"] == "lease expired during the final attempt"
    assert stages.calls["processing"] == 0  # no 4th attempt


# ---- shutdown --------------------------------------------------------------

async def test_run_forever_stops_when_signalled(db):
    stop = asyncio.Event()
    task = asyncio.create_task(make_worker(db, ScriptedStages(), FakeClock()).run_forever(stop))
    stop.set()
    await asyncio.wait_for(task, timeout=5)  # exits promptly, no hanging loop