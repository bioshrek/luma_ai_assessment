"""`SEGMENT_BOUNDS`: D's episode boundary is a human's claim, so it is itself data under test."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import meta

from rdp.domain.capabilities import Capabilities
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.segment_bounds import SegmentBounds
from rdp.domain.segment import EpisodeSegment

N = 30
IS_SEGMENT = Capabilities(has_action=True, has_state=True, is_segment=True)


def frames_of(n: int = N) -> FrameTable:
    return FrameTable(columns={"t": np.arange(n, dtype=np.float64) / 30.0})


def view(**segment: float | str | None):
    fields: dict[str, object] = {
        "parent_id": "P01_01",
        "start_s": 10.0,
        "end_s": 12.0,
        "parent_duration_s": 1800.0,
    }
    fields.update(segment)
    return meta(
        n_frames=N,
        capabilities=IS_SEGMENT,
        segment=EpisodeSegment(**fields),  # type: ignore[arg-type]
    )


def test_a_plausible_interval_inside_its_video_passes() -> None:
    result = SegmentBounds().evaluate(frames_of(), view(prev_end_s=9.8, next_start_s=11.9))
    assert result.verdict is Verdict.PASS
    assert result.metrics["segment_duration_s"] == pytest.approx(2.0)
    assert result.metrics["overlap_prev_s"] == pytest.approx(0.0)


def test_an_inverted_interval_fails() -> None:
    result = SegmentBounds().evaluate(frames_of(), view(start_s=12.0, end_s=10.0))
    assert result.verdict is Verdict.FAIL
    assert "inverted" in result.reason


def test_an_interval_running_past_the_end_of_its_video_fails() -> None:
    result = SegmentBounds().evaluate(frames_of(), view(end_s=1900.0))
    assert result.verdict is Verdict.FAIL
    assert "past the" in result.reason


def test_an_interval_too_short_to_contain_the_action_it_names_is_a_review() -> None:
    """Measured: 643 of EPIC-100's 67,217 training segments are shorter than 0.4 s."""
    result = SegmentBounds().evaluate(frames_of(), view(end_s=10.2))
    assert result.verdict is Verdict.REVIEW
    assert "shorter than" in result.reason


def test_overlapping_a_neighbour_by_a_majority_is_a_review() -> None:
    """19,162 segments overlap the next one at all, so only a majority overlap is reported."""
    result = SegmentBounds().evaluate(frames_of(), view(next_start_s=10.5))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["overlap_fraction"] == pytest.approx(0.75)
    assert "overlaps an adjacent segment" in result.reason


def test_a_modest_overlap_is_normal_in_this_dataset() -> None:
    result = SegmentBounds().evaluate(frames_of(), view(next_start_s=11.5))
    assert result.verdict is Verdict.PASS
    assert result.metrics["overlap_fraction"] == pytest.approx(0.25)


def test_an_episode_that_is_a_whole_recording_never_reaches_the_rule() -> None:
    result = evaluate_rule(SegmentBounds(), frames_of(), meta(n_frames=N))
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == "capability_unmet:is_segment"
