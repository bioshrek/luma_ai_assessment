"""System clock. Isolated behind a port so tests get deterministic timestamps."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now_iso(self) -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    def horizon_iso(self, seconds: float) -> str:
        """Lease expiry. Kept here so the domain never does date arithmetic."""
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")

    def monotonic(self) -> float:
        """Durations only. Immune to an NTP step, which `now_iso` is not."""
        return time.monotonic()
