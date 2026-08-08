"""`POSE_COVERAGE` — how much of the segment the camera-pose layer actually registered.

Only source D reaches this rule. EPIC-Fields poses come from a COLMAP reconstruction that fails
on some frames; those frames are stored as NULL (never 0 — a zero pose is a *place*, and
inventing one is the exact failure mode design §8.5 forbids). So a NaN here is a fact about the
reconstruction, not corrupt data, and the verdict is REVIEW: the episode is usable for anything
that does not need continuous pose, and a human decides.

Two independent complaints, because they mean different things:

- **coverage** — the fraction of frames with a pose at all.
- **the longest continuous hole, in seconds** — 20% missing spread evenly is a usable trajectory;
  20% missing as one contiguous second is a discontinuity.

Channels are found by `space`, never by name: `camera_translation_abs` is the semantic that
matters, and source D's upstream calls the field `frame_XXXXXXXXXX.jpg`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import ChannelSpace, SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "POSE_COVERAGE"

_POSE_SPACES = frozenset(
    {ChannelSpace.CAMERA_TRANSLATION_ABS, ChannelSpace.CAMERA_ROTATION_ABS}
)


def _required_levels() -> Mapping[str, SignalLevel]:
    return {"state": SignalLevel.PER_FRAME_CONTINUOUS}


@dataclass(frozen=True)
class PoseCoverage:
    min_coverage: float = 0.8
    max_gap_s: float = 0.5
    rule_id: str = RULE_ID
    severity: Severity = Severity.REVIEW
    required_capabilities: frozenset[str] = frozenset({"has_camera_pose"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_required_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        spec = meta.spec_of("state")
        view = frames.physical_view(spec)
        pose = [view[c.name] for c in spec.physical_channels if c.space in _POSE_SPACES]
        if not pose:
            raise ValueError("has_camera_pose is set but no camera-pose channel is declared")

        # A frame counts as registered only when the whole pose is present: half a pose is not
        # a pose, and the layers are written together or not at all.
        registered = np.logical_and.reduce([np.isfinite(values) for values in pose])
        n = int(registered.shape[0])
        coverage = float(np.count_nonzero(registered)) / n if n else 0.0
        gap_s = _longest_gap_seconds(registered, frames.t)
        metrics = {
            "pose_coverage": coverage,
            "longest_gap_s": gap_s,
            "n_unregistered": float(n - int(np.count_nonzero(registered))),
        }

        complaints = []
        if coverage < self.min_coverage:
            complaints.append(f"coverage {coverage:.3f} < {self.min_coverage}")
        if gap_s > self.max_gap_s:
            complaints.append(f"longest unregistered run {gap_s:.3f}s > {self.max_gap_s}s")
        if complaints:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                "camera pose is incomplete: " + "; ".join(complaints),
            )
        return RuleResult(
            self.rule_id, Verdict.PASS, metrics, f"camera pose registered on {coverage:.3f}"
        )


def _longest_gap_seconds(registered: NDArray[np.bool_], t: NDArray[np.float64]) -> float:
    """Measured on the clock, not in frames: a hole is only as bad as the time it spans."""
    worst = 0.0
    start: int | None = None
    for index, present in enumerate(registered):
        if not present and start is None:
            start = index
        elif present and start is not None:
            worst = max(worst, float(t[index] - t[start]))
            start = None
    if start is not None:
        worst = max(worst, float(t[-1] - t[start]))
    return worst
