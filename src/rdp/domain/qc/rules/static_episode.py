"""`STATIC_EPISODE` — too short to contain a demonstration, or nothing in it ever moved.

The only ungated rule: an episode with no action, no state and no camera still has a length, and
"30 frames of nothing" is a defect on every source. What each of the three tests can say depends
on what the episode has, so each degrades on its own:

1. **too short** — `n_frames < min_frames`. Always available.
2. **frozen** — the fraction of steps in which *no* physical channel moved at all. Exact
   equality is deliberate: a real sensor never repeats a value bit-for-bit, so a run of
   identical rows means the recorder stalled, not that the operator paused.
3. **too little travel** — total travel expressed as a fraction of the channel's own declared
   range, which is the only way to compare a pusher measured in pixels against a joint measured
   in radians. Only *positional* channels are eligible: a gripper's range is actuation, which
   `GRIPPER_STUCK` already judges, and a rotation parameterization's range (a quaternion
   component in [-1, 1]) is not a distance at all. Channels that declare no limits sit the test
   out; if none is eligible, the test is not attempted rather than silently passed.

A NaN is not stillness (D's unregistered pose frames): holes are excluded from every count, and
a channel that is nothing but holes reports no travel rather than no movement.

Because it is ungated, `required_levels` is empty, and the engine's invariant-13 downgrade has
nothing to key off. The rule therefore applies that rule itself: a motion complaint about
channels that were all estimated is a REVIEW, while "this episode has 4 frames" is a fact about
the episode and stays a FAIL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import (
    ROTATION_SPACES,
    Channel,
    ChannelSpace,
    SignalLevel,
    SignalSpec,
)
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict
from rdp.domain.qc.rules._support import derived_basis, step_magnitudes

RULE_ID = "STATIC_EPISODE"


def _no_levels() -> Mapping[str, SignalLevel]:
    return {}


@dataclass(frozen=True)
class StaticEpisode:
    min_frames: int = 20
    max_still_fraction: float = 0.95
    min_travel_fraction: float = 0.01
    """Total travel as a fraction of the channel's declared range."""

    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset()
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_no_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        metrics: dict[str, float] = {}
        complaints: list[str] = []

        if frames.n_frames < self.min_frames:
            complaints.append(f"{frames.n_frames} frames < {self.min_frames}")
        too_short = bool(complaints)

        signals = self._per_frame_signals(meta)
        motion = [
            (channel, step_magnitudes(values, channel))
            for signal in signals
            for channel, values in _physical(frames, meta.spec_of(signal))
        ]
        if motion:
            still = self._still_fraction([steps for _, steps in motion])
            if still is not None:
                metrics["still_fraction"] = still
                if still > self.max_still_fraction:
                    complaints.append(
                        f"{still:.3f} of steps moved no channel at all "
                        f"(> {self.max_still_fraction})"
                    )
            travel = self._travel_fraction(motion)
            if travel is not None:
                metrics["travel_fraction"] = travel
                if travel < self.min_travel_fraction:
                    complaints.append(
                        f"the busiest channel covered {travel:.4f} of its declared range "
                        f"(< {self.min_travel_fraction})"
                    )

        if not complaints:
            return RuleResult(
                self.rule_id, Verdict.PASS, metrics, f"{frames.n_frames} frames, and they move"
            )
        reason = "; ".join(complaints)
        # The engine's downgrade keys off `required_levels`, which this rule leaves empty so that
        # it can run on any episode. So it applies invariant 13 itself, over what it read.
        basis = None if too_short else derived_basis(meta, signals)
        if basis is None:
            return RuleResult(self.rule_id, Verdict.FAIL, metrics, reason)
        return RuleResult(
            self.rule_id,
            Verdict.REVIEW,
            metrics,
            f"{reason} [FAIL downgraded to REVIEW: every channel read has origin={basis}, "
            f"not measured (invariant 13)]",
        )

    def _per_frame_signals(self, meta: QCEpisodeView) -> list[str]:
        return [
            signal
            for signal in ("action", "state")
            if meta.level_of(signal) is SignalLevel.PER_FRAME_CONTINUOUS
        ]

    def _still_fraction(self, motion: list[NDArray[np.float64]]) -> float | None:
        """Steps in which every channel that had a reading reported no movement at all."""
        known = [np.isfinite(steps) for steps in motion]
        observed = np.logical_or.reduce(known)
        if not bool(observed.any()):
            return None
        frozen = np.logical_and.reduce(
            [~present | (steps == 0.0) for steps, present in zip(motion, known, strict=True)]
        )
        return float(np.count_nonzero(frozen & observed)) / float(np.count_nonzero(observed))

    def _travel_fraction(
        self, motion: list[tuple[Channel, NDArray[np.float64]]]
    ) -> float | None:
        best: float | None = None
        for channel, steps in motion:
            if not _is_positional(channel):
                continue
            if channel.min is None or channel.max is None:
                continue
            declared = float(channel.max) - float(channel.min)
            if declared <= 0.0:
                continue
            finite = steps[np.isfinite(steps)]
            if finite.size == 0:
                # Every step is a hole. "It never moved" would be an invention, not a reading.
                continue
            best = max(best or 0.0, float(finite.sum()) / declared)
        return best


def _is_positional(channel: Channel) -> bool:
    """Does this channel's declared range measure a distance travelled?

    A gripper's does not - it measures actuation, and `GRIPPER_STUCK` owns that question. A
    rotation's does not either: berkeley_ur5's axis-angle and EPIC's camera quaternion both
    declare limits, but the fraction of [-1, 1] a quaternion component sweeps is not a distance.
    """
    return channel.space not in ROTATION_SPACES and channel.space is not ChannelSpace.GRIPPER


def _physical(
    frames: FrameTable, spec: SignalSpec
) -> list[tuple[Channel, NDArray[np.float64]]]:
    view = frames.physical_view(spec)
    return [(channel, view[channel.name]) for channel in spec.physical_channels]
