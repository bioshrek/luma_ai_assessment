"""`ACTION_RANGE` — action values outside the declared channel limits, or not finite (design §3).

Two gates matter more than the arithmetic:

- **physical channels only.** C's `terminate_episode` rides inside the action vector but its
  "limits" are `{0, 1}`; judging it against physical bounds is meaningless (invariant 6).
- **`level == per_frame_continuous`.** D has `has_action=True` and no action column at all, so a
  capability-only gate would reach for a column that does not exist. The engine reports
  `SKIPPED(reason=action_level_is_episode_label)` instead — a different conclusion from
  "there is no action", and counted separately.

Limits are read from `config/embodiments.yaml`, so this rule needs no threshold of its own: a
channel that declares neither `min` nor `max` is only checked for NaN/Inf.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "ACTION_RANGE"


def _required_levels() -> Mapping[str, SignalLevel]:
    return {"action": SignalLevel.PER_FRAME_CONTINUOUS}


@dataclass(frozen=True)
class ActionRange:
    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset({"has_action"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_required_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        spec = meta.spec_of("action")
        view = frames.physical_view(spec)
        n_non_finite = 0
        n_out_of_range = 0
        offenders: list[str] = []
        for channel in spec.physical_channels:
            values = view[channel.name]
            finite = np.isfinite(values)
            bad_finite = int(np.count_nonzero(~finite))
            outside = np.zeros_like(finite)
            if channel.min is not None:
                outside |= finite & (values < channel.min)
            if channel.max is not None:
                outside |= finite & (values > channel.max)
            bad_range = int(np.count_nonzero(outside))
            n_non_finite += bad_finite
            n_out_of_range += bad_range
            if bad_finite or bad_range:
                offenders.append(channel.name)

        metrics = {
            "n_non_finite": float(n_non_finite),
            "n_out_of_range": float(n_out_of_range),
            "n_physical_channels": float(len(spec.physical_channels)),
        }
        if offenders:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"{n_non_finite} non-finite and {n_out_of_range} out-of-range value(s) on "
                f"{', '.join(sorted(offenders))}",
            )
        return RuleResult(
            self.rule_id, Verdict.PASS, metrics, "all physical action channels within limits"
        )
