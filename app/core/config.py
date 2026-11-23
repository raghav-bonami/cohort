from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "cohort-insights-api"
    log_level: str = "INFO"

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "cohort"
    redis_url: str = "redis://localhost:6379/0"

    # ---- worker -----------------------------------------------------------
    # Independent claim loops inside one worker process. Scale further with more
    # processes (docker compose up --scale worker=3); the atomic claim makes both safe.
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    # How long an idle loop waits before looking for due jobs again.
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    # How long a claim stays exclusive. If a worker dies, its job becomes claimable
    # again this long after the claim. Must exceed the longest stage (validated below).
    lease_seconds: float = Field(default=60.0, gt=0)
    # Attempts per stage (first try + retries) before the document is marked failed.
    max_attempts: int = Field(default=3, ge=1)
    # Wait before retry n = retry_base_seconds * 2**(n-1): 2s, 4s, 8s ...
    retry_base_seconds: float = Field(default=2.0, ge=0)

    # Simulated stage timings (seconds) and failure chance. Tests set delays to 0.
    processing_min_seconds: float = Field(default=10.0, ge=0)
    processing_max_seconds: float = Field(default=20.0, ge=0)
    enriching_min_seconds: float = Field(default=5.0, ge=0)
    enriching_max_seconds: float = Field(default=15.0, ge=0)
    stage_failure_rate: float = Field(default=0.1, ge=0, le=1)

    @model_validator(mode="after")
    def _check_worker_timings(self) -> "Settings":
        if (
            self.processing_min_seconds > self.processing_max_seconds
            or self.enriching_min_seconds > self.enriching_max_seconds
        ):
            raise ValueError("stage min_seconds must not exceed max_seconds")
        # A lease shorter than a stage would let a healthy job be re-claimed mid-run.
        # (Correctness would still hold, because fencing discards the loser's write,
        # but every long job would be processed twice.)
        longest_stage = max(self.processing_max_seconds, self.enriching_max_seconds)
        if self.lease_seconds <= longest_stage:
            raise ValueError("lease_seconds must exceed the longest stage duration")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
