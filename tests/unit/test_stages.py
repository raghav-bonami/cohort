import random

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.worker.stages import SUMMARY_WORDS, SimulatedStages, StageError, make_summary, make_tags


def no_delay(**overrides) -> Settings:
    return Settings(
        processing_min_seconds=0,
        processing_max_seconds=0,
        enriching_min_seconds=0,
        enriching_max_seconds=0,
        **overrides,
    )


def test_summary_keeps_short_content_and_normalises_spaces():
    assert make_summary("one  two\nthree") == "one two three"


def test_summary_truncates_long_content():
    summary = make_summary(" ".join(f"w{i}" for i in range(100)))
    assert summary.endswith(" ...")
    assert len(summary.split()) == SUMMARY_WORDS + 1  # 30 words + "..."


def test_tags_are_ranked_deterministic_and_capped():
    summary = "mongo mongo redis redis redis fastapi python docker worker lease"
    # redis(3), mongo(2), then ties broken alphabetically, capped at 5
    assert make_tags(summary) == ["redis", "mongo", "docker", "fastapi", "lease"]


def test_tags_skip_stopwords_and_short_words():
    assert make_tags("this is that with an api") == []


async def test_stage_always_fails_at_rate_one():
    stages = SimulatedStages(no_delay(stage_failure_rate=1.0), rng=random.Random(1))
    with pytest.raises(StageError):
        await stages.processing("text")


async def test_stage_never_fails_at_rate_zero():
    stages = SimulatedStages(no_delay(stage_failure_rate=0.0), rng=random.Random(1))
    assert await stages.processing("hello world") == "hello world"


async def test_simulated_delay_is_within_configured_range():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    settings = Settings(processing_min_seconds=10, processing_max_seconds=20, stage_failure_rate=0)
    await SimulatedStages(settings, rng=random.Random(7), sleep=fake_sleep).processing("x")
    assert 10 <= slept[0] <= 20


def test_lease_must_outlast_the_longest_stage():
    with pytest.raises(ValidationError):
        Settings(lease_seconds=15, processing_max_seconds=20)
