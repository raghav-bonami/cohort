"""The pipeline worker: claim one due job, run one stage of it, record the outcome.

Each stage is its own job. After processing succeeds the document is released and
enriching is claimed as a separate job, so both stages share one code path:

    claim (atomic) -> start (fenced) -> run stage -> complete / retry / fail (fenced)
"""

import asyncio
import contextlib
import uuid
from datetime import timedelta
from typing import Any

import structlog
from pymongo.errors import PyMongoError

from app.core.clock import Clock, utc_now
from app.core.config import Settings
from app.domain.enums import DocumentStatus, StageName
from app.repositories.document_repo import DocumentRepository, Fence
from app.services.rate_limiter import ActiveDocumentLimiter
from app.services.result_cache import ResultCache
from app.worker.stages import StageError, Stages

log = structlog.get_logger()


class Worker:
    def __init__(
        self,
        repo: DocumentRepository,
        stages: Stages,
        settings: Settings,
        clock: Clock = utc_now,  # injectable: tests move time forward instead of sleeping
        *,
        limiter: ActiveDocumentLimiter | None = None,  # None: no slot release (tests)
        cache: ResultCache | None = None,  # None: no cache read/write (tests)
    ) -> None:
        self._repo = repo
        self._stages = stages
        self._settings = settings
        self._clock = clock
        self._limiter = limiter
        self._cache = cache

    async def run_once(self) -> bool:
        """Process at most one stage of one job. Returns False if nothing was due."""
        now = self._clock()
        lease_id = (
            uuid.uuid4().hex
        )  # unique per claim, so a re-claim always fences out the old holder
        doc = await self._repo.claim_next(
            now=now,
            lease_id=lease_id,
            lease_until=now + timedelta(seconds=self._settings.lease_seconds),
        )
        if doc is None:
            return False

        # queued/processing documents need stage 1; enriching documents need stage 2.
        stage = (
            StageName.ENRICHING
            if doc["status"] == DocumentStatus.ENRICHING.value
            else StageName.PROCESSING
        )
        fence = Fence(doc["_id"], lease_id, doc["content_version"])
        job_log = log.bind(
            document_id=str(doc["_id"]), stage=stage.value, content_version=fence.content_version
        )
        previous_attempts = doc["stages"][stage.value]["attempts"]

        # Content cache: this exact content was processed before (a PATCH back to
        # earlier content, or a duplicate submitted while the first was still running).
        # Publish the known result instead of running the stages; same fence as always.
        if stage == StageName.PROCESSING and self._cache is not None:
            cached = await self._cache.get(doc["content_hash"])
            if cached is not None:
                ok = await self._repo.complete_from_cache(
                    fence,
                    content_hash=doc["content_hash"],
                    summary=cached.summary,
                    tags=cached.tags,
                    now=self._clock(),
                )
                self._log_outcome(job_log, ok, "completed_from_cache")
                if ok:
                    await self._release_slot(doc)
                return True

        # The previous holder started the final attempt and never finished (crashed, or
        # ran past its lease). Starting another would exceed the limit - and a document
        # that crashes the worker would be retried forever.
        if previous_attempts >= self._settings.max_attempts:
            ok = await self._repo.fail_stage(
                fence, stage, error="lease expired during the final attempt", now=self._clock()
            )
            self._log_outcome(job_log, ok, "stage_failed", attempts=previous_attempts)
            if ok:
                await self._release_slot(doc)
            return True

        if not await self._repo.start_stage(fence, stage, now=self._clock()):
            job_log.info("stale_job_skipped")  # PATCHed between our claim and our start
            return True

        attempt = previous_attempts + 1
        job_log = job_log.bind(attempt=attempt)
        job_log.info("stage_started")
        try:
            ok = await self._run_stage(fence, stage, doc)
            event = "stage_succeeded"
        except StageError as exc:
            job_log = job_log.bind(error=str(exc))
            ok, event = await self._record_failure(fence, stage, attempt, str(exc))

        self._log_outcome(job_log, ok, event)
        # The document reached completed or failed: give the user's slot back. Only if
        # our write landed; if it was fenced out, the document is still active (PATCHed).
        finished = event == "stage_failed" or (
            event == "stage_succeeded" and stage == StageName.ENRICHING
        )
        if ok and finished:
            await self._release_slot(doc)
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Claim and process jobs until `stop` is set. Finishes the current job first."""
        while not stop.is_set():
            try:
                found = await self.run_once()
            except PyMongoError as exc:
                # Mongo unreachable, or a write failed mid-job. Don't crash the loop:
                # back off and try again. An interrupted job is re-claimed when its
                # lease expires.
                log.warning("worker_db_error", error=str(exc))
                found = False

            if not found:
                # Nothing due: wait one poll interval, but wake immediately on shutdown.
                # The timeout is the normal path here, not an error.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.worker_poll_seconds)

    # ---- internals -------------------------------------------------------

    async def _run_stage(self, fence: Fence, stage: StageName, doc: dict[str, Any]) -> bool:
        if stage == StageName.PROCESSING:
            summary = await self._stages.processing(doc["content"])
            return await self._repo.complete_processing(fence, summary=summary, now=self._clock())

        # Tags may only be derived from a summary of the SAME content version.
        stage1 = doc.get("stage1_output")
        if stage1 is None or stage1["content_version"] != fence.content_version:
            raise StageError("stage-1 output is missing or belongs to another content version")

        tags = await self._stages.enriching(stage1["summary"])
        ok = await self._repo.complete_enriching(
            fence,
            content_hash=doc["content_hash"],
            summary=stage1["summary"],
            tags=tags,
            now=self._clock(),
        )
        # Written only when the result was actually published (PLAN A8).
        if ok and self._cache is not None:
            await self._cache.put(doc["content_hash"], summary=stage1["summary"], tags=tags)
        return ok

    async def _record_failure(
        self, fence: Fence, stage: StageName, attempt: int, error: str
    ) -> tuple[bool, str]:
        now = self._clock()
        if attempt >= self._settings.max_attempts:
            ok = await self._repo.fail_stage(fence, stage, error=error, now=now)
            return ok, "stage_failed"

        delay = self._settings.retry_base_seconds * 2 ** (attempt - 1)  # 2s, 4s, 8s ...
        ok = await self._repo.schedule_retry(
            fence, stage, error=error, retry_at=now + timedelta(seconds=delay), now=now
        )
        return ok, "stage_retry_scheduled"

    async def _release_slot(self, doc: dict[str, Any]) -> None:
        """The document is completed or failed: free the user's rate-limit slot."""
        if self._limiter is not None:
            await self._limiter.release(doc["user_id"], str(doc["_id"]))

    @staticmethod
    def _log_outcome(job_log: Any, ok: bool, event: str, **extra: Any) -> None:
        if ok:
            job_log.info(event, **extra)
        else:
            # The fence rejected the write: the content was PATCHed, or the lease
            # expired and another worker owns the job now. This output belongs to
            # content that is no longer current, so discarding it is correct.
            job_log.info("stale_write_discarded", intended=event, **extra)
