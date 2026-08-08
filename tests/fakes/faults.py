"""An in-process stand-in for `kill -9`.

`Crash` derives from `BaseException`, not `Exception`, on purpose: the per-episode error handler
in `IngestEpisodes` catches `Exception` and writes a `FAILED` row. A crash writes nothing — that
difference is the whole point of the test, so the exception must be uncatchable by that handler.
"""

from __future__ import annotations


class Crash(BaseException):
    pass


class RaisingFaultInjector:
    def __init__(self, checkpoint: str, occurrence: int = 1) -> None:
        self.checkpoint = checkpoint
        self.occurrence = occurrence
        self.hits = 0

    def maybe_crash(self, checkpoint: str) -> None:
        if checkpoint != self.checkpoint:
            return
        self.hits += 1
        if self.hits >= self.occurrence:
            raise Crash(f"{checkpoint} occurrence {self.hits}")
