"""Monotonic server-side durations; repeated stages accumulate, including failures."""

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from pydantic import Field

from moniepoint_demo.models import Record


class Timings(Record):
    total_ms: float = 0
    stages_ms: dict[str, float] = Field(default_factory=dict)

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            duration = (perf_counter() - started) * 1000
            self.stages_ms[stage] = self.stages_ms.get(stage, 0) + duration
