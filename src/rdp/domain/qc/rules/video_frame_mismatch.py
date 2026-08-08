"""`VIDEO_FRAME_MISMATCH` — a declared camera is missing, or its video is not the episode's length.

Gated on `has_video`, which by invariant 11 means a standalone decodable file. That gate is the
whole point of splitting `has_rgb` from `has_video` (design §2.2e): C's imagery is an array
inlined in the TFRecord record, so "the mp4 has a different number of frames than the parquet"
is not a question that can be asked of it, and the rule resolves to `SKIPPED` rather than
inventing an answer. On this corpus no source fetches pixels, so it skips everywhere — that is
a property of the run configuration, not of the rule.

`n_frames=None` on a camera means the count was never measured, which is not zero. Only a
measured disagreement is a defect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rdp.domain.action_spec import SignalLevel
from rdp.domain.camera import CameraEncoding
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "VIDEO_FRAME_MISMATCH"


def _no_levels() -> Mapping[str, SignalLevel]:
    return {}


@dataclass(frozen=True)
class VideoFrameMismatch:
    max_frame_delta: int = 1
    """One frame of slack: encoders disagree with row counts by a frame at the boundaries."""

    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset({"has_video"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_no_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        cameras = [c for c in meta.cameras if c.encoding is CameraEncoding.MP4_SIDECAR]
        missing = [c.name for c in cameras if not c.is_present]
        measured = [
            (c.name, c.n_frames) for c in cameras if c.is_present and c.n_frames is not None
        ]

        worst = 0
        mismatched: list[str] = []
        for name, n_video in measured:
            delta = abs(int(n_video) - frames.n_frames)
            worst = max(worst, delta)
            if delta > self.max_frame_delta:
                mismatched.append(f"{name} ({n_video} vs {frames.n_frames})")

        metrics = {
            "n_cameras": float(len(cameras)),
            "n_missing": float(len(missing)),
            "n_measured": float(len(measured)),
            "n_mismatched": float(len(mismatched)),
            "max_frame_delta": float(worst),
        }
        complaints = []
        if missing:
            complaints.append(f"declared but absent: {', '.join(sorted(missing))}")
        if mismatched:
            complaints.append(f"frame count disagrees with the row count: {', '.join(mismatched)}")
        if complaints:
            return RuleResult(self.rule_id, Verdict.FAIL, metrics, "; ".join(complaints))
        if not measured:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                metrics,
                "every declared camera is present, but no frame count was measured",
            )
        return RuleResult(
            self.rule_id,
            Verdict.PASS,
            metrics,
            f"{len(measured)} video(s) within {worst} frame(s) of the {frames.n_frames} rows",
        )
