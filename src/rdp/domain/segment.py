"""`EpisodeSegment` — an episode that was cut out of a longer parent recording (design §2.2i).

Source D's episodes are annotation intervals over a 30-minute kitchen video: the recording is
continuous and the *episode* is an annotator's claim about where an action starts and stops.
That claim can be wrong — the interval can be inverted, run past the end of the video, be too
short to contain the action, or overlap its neighbour so heavily that the two are the same
event labelled twice. `SEGMENT_BOUNDS` is the rule that judges it.

Which is why this value object **validates almost nothing**. A domain object that rejected
`start_s >= end_s` would make the defect unrepresentable, and an unrepresentable defect cannot
be reported — it becomes an ingestion crash instead of a QC verdict. The invariants here are
about what we *recorded*, not about whether the annotator was right.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EpisodeSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_id: str = Field(min_length=1)
    """The recording this interval was cut from — a video id, not another episode's uid."""

    start_s: float
    end_s: float
    parent_duration_s: float | None = None
    """None when upstream does not publish the recording's length: unknown, not unbounded."""

    prev_end_s: float | None = None
    """Where the preceding interval of the same parent ended; None if this is the first."""

    next_start_s: float | None = None

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s
