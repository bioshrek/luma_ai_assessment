"""`TS_MONOTONIC` — non-monotonic or duplicate timestamps (design §3).

Gated on real timestamps. On a source whose clock we synthesized from a declared rate the rule
would pass by construction, which is a meaningless result — so it resolves to
`SKIPPED(reason=synthetic_timestamp)` instead. That is the line between degrading and passing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "TS_MONOTONIC"


@dataclass(frozen=True)
class TsMonotonic:
    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset()
    required_levels: Mapping[str, SignalLevel] = field(default_factory=dict)
    requires_real_timestamps: bool = True

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        t = frames.t
        if t.size < 2:
            return RuleResult(
                self.rule_id,
                Verdict.PASS,
                {"n_non_positive_dt": 0.0},
                "fewer than two frames; nothing to compare",
            )
        dt = np.diff(t)
        n_bad = int(np.count_nonzero(dt <= 0))
        n_nan = int(np.count_nonzero(~np.isfinite(t)))
        metrics = {
            "n_non_positive_dt": float(n_bad),
            "n_non_finite_t": float(n_nan),
            "min_dt": float(np.min(dt)),
            "median_dt": float(np.median(dt)),
            "max_dt": float(np.max(dt)),
        }
        if n_bad or n_nan:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"{n_bad} non-positive timestamp step(s), {n_nan} non-finite timestamp(s)",
            )
        return RuleResult(self.rule_id, Verdict.PASS, metrics, "timestamps strictly increasing")
