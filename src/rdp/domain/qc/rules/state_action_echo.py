"""`STATE_ACTION_ECHO` — the action column is a readback of state, not a command.

The capture-script bug this catches is real and expensive: a recorder that writes
`action[t] = state[t]` produces a dataset that trains a policy to predict where the robot
already is. It is invisible to every other rule — the numbers are all in range, smooth, and
well-formed.

**The trap (design §3, and the reason this rule exists in this form).** ALOHA (source B) is
joint-position teleoperated: the leader arm's angles *are* the command and the follower tracks
them within a few milliradians. Measured over all 50 ingested episodes, the action/state
correlation is 0.88-0.97 and `max abs(a-s)` is 0.45-0.88 rad. Any correlation threshold high
enough to catch a genuine echo would also condemn B, which is not broken — it is how bimanual
teleoperation works. So the test is **bit-equality**, which no physical follower ever achieves:
measured on B, the bit-equal fraction never exceeded 0.0005.

The correlation is still computed, and reported as a metric. A number that must *not* be used
as a threshold is worth publishing precisely so the next person can see why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import SignalLevel, SignalSpec
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "STATE_ACTION_ECHO"


def _required_levels() -> Mapping[str, SignalLevel]:
    return {"action": SignalLevel.PER_FRAME_CONTINUOUS, "state": SignalLevel.PER_FRAME_CONTINUOUS}


@dataclass(frozen=True)
class StateActionEcho:
    tolerance: float = 1e-9
    """"Bit-identical" with room for one float64 round-trip through parquet, and no more."""
    max_echo_fraction: float = 0.9

    rule_id: str = RULE_ID
    severity: Severity = Severity.REVIEW
    required_capabilities: frozenset[str] = frozenset({"has_action", "has_state"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_required_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        action_spec, state_spec = meta.spec_of("action"), meta.spec_of("state")
        # "Same space, same dimensionality" is a precondition the *data* decides, so it cannot
        # be a declarative gate; an incomparable pair is SKIPPED with the reason spelled out.
        if action_spec.space != state_spec.space:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                {},
                f"not comparable: action is {action_spec.space}, state is {state_spec.space}",
            )
        if action_spec.physical_dim != state_spec.physical_dim:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                {},
                f"not comparable: {action_spec.physical_dim} physical action channels vs "
                f"{state_spec.physical_dim} state channels",
            )

        action = _matrix(frames, action_spec)
        state = _matrix(frames, state_spec)
        if action.size == 0:
            return RuleResult(self.rule_id, Verdict.SKIPPED, {}, "no physical channels to compare")

        difference = np.abs(action - state)
        comparable = np.isfinite(difference)
        rows = np.logical_and.reduce(comparable, axis=1)
        if not bool(rows.any()):
            return RuleResult(self.rule_id, Verdict.SKIPPED, {}, "no frame has both signals")

        worst_per_frame = np.max(np.where(comparable, difference, 0.0), axis=1)
        echoed = (worst_per_frame < self.tolerance) & rows
        fraction = float(np.count_nonzero(echoed)) / float(np.count_nonzero(rows))
        metrics = {
            "echo_fraction": fraction,
            "max_abs_difference": float(worst_per_frame[rows].max()),
            "correlation": _correlation(action[rows], state[rows]),
        }
        if fraction > self.max_echo_fraction:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                f"action is bit-identical to state in {fraction:.3f} of frames "
                f"(> {self.max_echo_fraction}): it looks like a readback, not a command",
            )
        return RuleResult(
            self.rule_id,
            Verdict.PASS,
            metrics,
            f"action differs from state by up to {metrics['max_abs_difference']:.6g} "
            f"(correlation {metrics['correlation']:.3f} is not evidence either way)",
        )


def _matrix(frames: FrameTable, spec: SignalSpec) -> NDArray[np.float64]:
    view = frames.physical_view(spec)
    columns = [view[channel.name] for channel in spec.physical_channels]
    if not columns:
        return np.empty((0, 0), dtype=np.float64)
    stacked: NDArray[np.float64] = np.stack(columns, axis=1).astype(np.float64, copy=False)
    return stacked


def _correlation(action: NDArray[np.float64], state: NDArray[np.float64]) -> float:
    """Pooled Pearson correlation over all channels — a diagnostic, never a threshold."""
    a, s = action.reshape(-1), state.reshape(-1)
    if a.size < 2 or float(a.std()) == 0.0 or float(s.std()) == 0.0:
        return 0.0
    return float(np.corrcoef(a, s)[0, 1])
