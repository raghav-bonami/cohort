"""Status vocabulary for the document pipeline.

Two levels on purpose:
- DocumentStatus is the single overall position, stored on the document and indexed,
  so listing by status and counting active documents are cheap queries.
- StageState is the per-stage detail, so a caller can tell exactly which stage
  is running, waiting to retry, or failed, without ad-hoc string fields.
"""

from enum import StrEnum


class DocumentStatus(StrEnum):
    QUEUED = "queued"  # waiting for the first processing attempt
    PROCESSING = "processing"  # stage 1 is running or waiting to retry
    ENRICHING = "enriching"  # stage 2 is running or waiting to retry
    COMPLETED = "completed"  # both stages succeeded for the current content_version
    FAILED = "failed"  # a stage ran out of retries; see failed_stage


class StageName(StrEnum):
    PROCESSING = "processing"  # stage 1: produces the summary
    ENRICHING = "enriching"  # stage 2: produces tags from the stage-1 summary


class StageState(StrEnum):
    PENDING = "pending"  # not started yet, or waiting for a retry
    RUNNING = "running"  # a worker holds the lease and is working on it
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # out of retries (terminal)


# "Active" = counts towards the per-user limit of 3 (queued + processing + enriching).
ACTIVE_STATUSES: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.QUEUED, DocumentStatus.PROCESSING, DocumentStatus.ENRICHING}
)
TERMINAL_STATUSES: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.COMPLETED, DocumentStatus.FAILED}
)
