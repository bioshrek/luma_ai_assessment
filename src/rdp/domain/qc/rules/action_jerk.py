"""`ACTION_JERK` — an isolated inter-frame discontinuity: a packet-loss step or a bad sample.

Three things this rule must get right, two of them traps:

- **physical channels only** (invariant 6). C's `terminate_episode` steps 0 -> 1 on the final
  frame, a magnitude far beyond that column's usual variation; without `physical_view()` every
  single C episode would be REVIEW. A unit test pins this.
- **delta channels are not differenced twice** — see `_support.step_magnitudes`.
- **the jump must be isolated.** Normal acceleration raises consecutive step sizes together;
  a dropped packet produces one large step against an otherwise quiet neighbourhood. Both
  conditions must hold, which is the design's "and the surrounding 2 frames are not smooth".
  The local baseline **excludes the immediately adjacent steps**, because a single corrupt
  sample is a jump out *and* a jump back: comparing a spike against its own echo would rate it
  perfectly smooth.

**Deviation from design §3, measured.** The design says "exceeds 5x that channel's p99.9". Within
a single episode of 69-500 samples the p99.9 *is* the maximum (numpy interpolates between the
top two order statistics), so `max / p99.9` is bounded by roughly 1.0 and the rule could never
fire on any real episode — measured across all 202: the ratio never exceeded 1.57. The reference
statistic is therefore p99 of the same channel's step magnitudes, which over ~100 samples is a
genuine order statistic. See ADR 014.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict
from rdp.domain.qc.rules._support import step_magnitudes

RULE_ID = "ACTION_JERK"

_MAX_ISOLATION = 1e9


def _required_levels() -> Mapping[str, SignalLevel]:
    return {"action": SignalLevel.PER_FRAME_CONTINUOUS}


@dataclass(frozen=True)
class ActionJerk:
    max_ratio: float = 8.0
    """`max |step| / p99 |step|`, per channel."""
    min_isolation: float = 5.0
    """How much larger the jump must be than the typical step around it."""
    isolation_window: int = 5
    """Half-width, in steps, of the neighbourhood the jump is judged against."""
    min_steps: int = 20
    """Below this, p99 is not an order statistic and the ratio is noise."""

    rule_id: str = RULE_ID
    severity: Severity = Severity.REVIEW
    required_capabilities: frozenset[str] = frozenset({"has_action"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_required_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        spec = meta.spec_of("action")
        view = frames.physical_view(spec)

        worst_ratio = 0.0
        worst_isolation = 0.0
        offenders: list[str] = []
        n_jerks = 0
        n_examined = 0
        for channel in spec.physical_channels:
            steps = step_magnitudes(view[channel.name], channel)
            steps = np.where(np.isfinite(steps), steps, 0.0)
            if steps.size < self.min_steps:
                continue
            reference = float(np.percentile(steps, 99))
            if reference <= 0.0:
                # A channel that never moves is `STATIC_EPISODE`'s and `GRIPPER_STUCK`'s
                # business; dividing by its p99 would report an infinite jerk.
                continue
            n_examined += 1
            ratios = steps / reference
            index = int(np.argmax(ratios))
            if float(ratios[index]) > worst_ratio:
                worst_ratio = float(ratios[index])
                worst_isolation = self._isolation(steps, index)
            hits = [
                position
                for position in np.flatnonzero(ratios > self.max_ratio)
                if self._isolation(steps, int(position)) > self.min_isolation
            ]
            if hits:
                n_jerks += len(hits)
                offenders.append(channel.name)

        metrics = {
            "jerk_ratio": worst_ratio,
            "jerk_isolation": worst_isolation,
            "n_jerks": float(n_jerks),
            "n_channels_examined": float(n_examined),
        }
        if not n_examined:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                metrics,
                f"no physical action channel has {self.min_steps} varying steps",
            )
        if offenders:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                f"{n_jerks} isolated discontinuity(ies) on {', '.join(sorted(offenders))}: "
                f"largest step is {worst_ratio:.2f}x the channel's p99",
            )
        return RuleResult(
            self.rule_id,
            Verdict.PASS,
            metrics,
            f"largest step is {worst_ratio:.2f}x its channel's p99 (limit {self.max_ratio})",
        )

    def _isolation(self, steps: NDArray[np.float64], index: int) -> float:
        """The step divided by the median step around it, its own echo excluded."""
        low = max(0, index - self.isolation_window)
        high = min(steps.size, index + self.isolation_window + 1)
        neighbourhood = np.concatenate((steps[low : max(low, index - 1)], steps[index + 2 : high]))
        if neighbourhood.size == 0:
            return 1.0
        baseline = float(np.median(neighbourhood))
        if baseline <= 0.0:
            # Quiet neighbourhood: as isolated as a step can be. Reported as a large finite
            # number rather than inf, because every metric is stored as JSON.
            return _MAX_ISOLATION if steps[index] > 0 else 1.0
        return min(float(steps[index]) / baseline, _MAX_ISOLATION)
