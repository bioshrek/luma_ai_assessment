"""System clock. Isolated behind a port so tests get deterministic timestamps."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now_iso(self) -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")
