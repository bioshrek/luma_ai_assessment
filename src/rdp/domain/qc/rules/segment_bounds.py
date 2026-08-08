"""`SEGMENT_BOUNDS` — the annotation interval that defines this episode is not credible.

Only source D reaches this rule, and it exists because D's episode boundary is not a physical
event but a human's claim about one (design §2.2i). The recording is a continuous 30-minute
kitchen video; an "episode" is the interval an annotator typed. That interval can be inverted,
can run past the end of the video, can be too short to contain the action it names, or can
overlap its neighbour so heavily that the two annotations describe the same moment twice.

Which is why `EpisodeSegment` validates none of this: a defect the domain model forbids is a
defect QC can never report.

**Measured over all 67,217 EPIC-100 training segments:** duration p1 = 0.4 s, p50 = 1.57 s;
643 segments are shorter than 0.4 s; none ends past its video's duration; 19,162 overlap the
following segment at all and 5,448 overlap it by more than half. Overlap is therefore expected
and only a *majority* overlap is worth a human's time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "SEGMENT_BOUNDS"


def _no_levels() -> Mapping[str, SignalLevel]:
    return {}


@dataclass(frozen=True)
class SegmentBounds:
    min_duration_s: float = 0.4
    max_overlap_fraction: float = 0.5

    rule_id: str = RULE_ID
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset({"is_segment"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_no_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        segment = meta.segment
        if segment is None:
            return RuleResult(
                self.rule_id, Verdict.SKIPPED, {}, "the episode records no segment bounds"
            )

        duration = segment.duration_s
        overlap_prev = _overlap(segment.prev_end_s, segment.start_s)
        overlap_next = _overlap(segment.end_s, segment.next_start_s)
        fraction = max(overlap_prev, overlap_next) / duration if duration > 0 else 0.0
        metrics = {
            "segment_duration_s": duration,
            "overlap_prev_s": overlap_prev,
            "overlap_next_s": overlap_next,
            "overlap_fraction": fraction,
        }

        if duration <= 0:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"interval is empty or inverted: {segment.start_s:.3f}s..{segment.end_s:.3f}s",
            )
        if segment.start_s < 0:
            return RuleResult(
                self.rule_id, Verdict.FAIL, metrics, f"starts before {segment.parent_id} does"
            )
        if segment.parent_duration_s is not None and segment.end_s > segment.parent_duration_s:
            return RuleResult(
                self.rule_id,
                Verdict.FAIL,
                metrics,
                f"ends at {segment.end_s:.3f}s, past the {segment.parent_duration_s:.3f}s of "
                f"{segment.parent_id}",
            )

        complaints = []
        if duration < self.min_duration_s:
            complaints.append(f"{duration:.3f}s is shorter than {self.min_duration_s}s")
        if fraction > self.max_overlap_fraction:
            complaints.append(
                f"{fraction:.3f} of it overlaps an adjacent segment "
                f"(> {self.max_overlap_fraction})"
            )
        if complaints:
            return RuleResult(self.rule_id, Verdict.REVIEW, metrics, "; ".join(complaints))
        return RuleResult(
            self.rule_id,
            Verdict.PASS,
            metrics,
            f"{duration:.3f}s and {frames.n_frames} frames within {segment.parent_id}, "
            f"overlapping {fraction:.3f}",
        )


def _overlap(earlier_end: float | None, later_start: float | None) -> float:
    """Seconds by which one interval reaches into the next. Never negative; 0 when unknown."""
    if earlier_end is None or later_start is None:
        return 0.0
    return max(0.0, earlier_end - later_start)
