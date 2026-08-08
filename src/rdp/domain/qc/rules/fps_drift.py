"""`FPS_DRIFT` — the measured frame rate disagrees with the declared one, or frames were dropped.

The rule is `requires_real_timestamps=True`, which on this corpus means it is `SKIPPED` on every
episode: three of four sources publish no timestamp at all and we synthesize the clock from the
control rate, and D's clock comes from annotation seconds. Comparing a synthesized clock against
the nominal rate it was *generated from* would produce a drift of zero on every episode — a rule
that always passes for a reason unrelated to the data. `SKIPPED(reason=synthetic_timestamp)` is
the honest outcome, and the engine counts it separately (design §3, "degrading is not passing").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "FPS_DRIFT"


def _no_levels() -> Mapping[str, SignalLevel]:
    return {}


@dataclass(frozen=True)
class FpsDrift:
    max_drift: float = 0.05
    gap_factor: float = 3.0
    max_gap_fraction: float = 0.01
    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    """Drift alone is a REVIEW; a dropped-frame gap is a FAIL, so the declared ceiling is FAIL."""

    required_capabilities: frozenset[str] = frozenset()
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_no_levels)
    requires_real_timestamps: bool = True

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        fps = meta.fps_nominal
        if fps is None or fps <= 0:
            return RuleResult(
                self.rule_id, Verdict.SKIPPED, {}, "no nominal frame rate is declared"
            )
        dt = np.diff(frames.t)
        if dt.size == 0:
            return RuleResult(self.rule_id, Verdict.SKIPPED, {}, "fewer than two frames")

        expected = 1.0 / fps
        median_dt = float(np.median(dt))
        drift = abs(median_dt - expected) / expected
        # A gap is measured against what this episode actually did, not against the nominal
        # rate: a recording that ran uniformly slow has drift, not dropped frames.
        n_gaps = int(np.count_nonzero(dt > self.gap_factor * median_dt)) if median_dt > 0 else 0
        gap_fraction = n_gaps / float(dt.size)
        metrics = {
            "median_dt": median_dt,
            "expected_dt": expected,
            "fps_drift": drift,
            "n_gaps": float(n_gaps),
            "gap_fraction": gap_fraction,
            "max_dt": float(dt.max()),
        }

        if gap_fraction > self.max_gap_fraction:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"{n_gaps} gap(s) longer than {self.gap_factor}x the median step "
                f"({gap_fraction:.3f} of steps > {self.max_gap_fraction})",
            )
        complaints = []
        if drift > self.max_drift:
            complaints.append(
                f"median step {median_dt:.6f}s is {drift:.3f} off the declared {expected:.6f}s"
            )
        if n_gaps:
            complaints.append(f"{n_gaps} dropped-frame gap(s)")
        if complaints:
            return RuleResult(self.rule_id, Verdict.REVIEW, metrics, "; ".join(complaints))
        return RuleResult(
            self.rule_id, Verdict.PASS, metrics, f"clock matches {fps:g} Hz within {drift:.3f}"
        )
