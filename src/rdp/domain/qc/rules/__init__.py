"""The rule catalogue. One module per rule; each is a pure function of frames + metadata."""

from __future__ import annotations

from rdp.domain.qc.rules.ts_monotonic import TsMonotonic

__all__ = ["TsMonotonic"]
