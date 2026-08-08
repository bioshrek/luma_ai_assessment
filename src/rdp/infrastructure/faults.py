"""The production `FaultInjector`: does nothing, always.

It exists so that crash-resume is exercised deterministically at every checkpoint in tests
rather than sampled by hand (design §8.5).
"""

from __future__ import annotations


class NoopFaultInjector:
    def maybe_crash(self, checkpoint: str) -> None:
        return None
