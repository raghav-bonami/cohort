"""Simulated pipeline stages.

Timing and failures are simulated (sleep + random failure), but the outputs are real
functions of their input. That matters: the content cache (Phase 7) reuses results by
content hash, which is only correct if the same content always produces the same
summary and tags.
"""

import asyncio
import random
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.core.config import Settings

SUMMARY_WORDS = 30
MAX_TAGS = 5
STOPWORDS = frozenset(
    "about also been from have into more only other some such than that their them "
    "then there these they this those were what when which will with would your".split()
)

Sleep = Callable[[float], Awaitable[None]]


class StageError(Exception):
    """One attempt of a stage failed. Retried until max_attempts is reached."""


class Stages(Protocol):
    """What the worker needs from the stages. Tests pass a scripted fake."""

    async def processing(self, content: str) -> str: ...

    async def enriching(self, summary: str) -> list[str]: ...


def make_summary(content: str) -> str:
    """Mock summary: the first SUMMARY_WORDS words, whitespace normalised."""
    words = content.split()
    summary = " ".join(words[:SUMMARY_WORDS])
    return summary + " ..." if len(words) > SUMMARY_WORDS else summary


def make_tags(summary: str) -> list[str]:
    """Mock tags: the most frequent words of 4+ letters in the summary, minus stopwords.

    Ties are broken alphabetically so the output is deterministic.
    """
    words = [w for w in re.findall(r"[a-z]{4,}", summary.lower()) if w not in STOPWORDS]
    ranked = sorted(Counter(words).items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:MAX_TAGS]]


class SimulatedStages:
    """Production stages: sleep for a random time, fail with stage_failure_rate."""

    def __init__(
        self,
        settings: Settings,
        rng: random.Random | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._rng = rng or random.Random()  # injectable so tests are deterministic
        self._sleep = sleep

    async def processing(self, content: str) -> str:
        s = self._settings
        await self._simulate("processing", s.processing_min_seconds, s.processing_max_seconds)
        return make_summary(content)

    async def enriching(self, summary: str) -> list[str]:
        s = self._settings
        await self._simulate("enriching", s.enriching_min_seconds, s.enriching_max_seconds)
        return make_tags(summary)

    async def _simulate(self, name: str, low: float, high: float) -> None:
        await self._sleep(self._rng.uniform(low, high))
        if self._rng.random() < self._settings.stage_failure_rate:
            raise StageError(f"simulated {name} failure")
