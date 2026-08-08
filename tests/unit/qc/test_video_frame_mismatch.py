"""`VIDEO_FRAME_MISMATCH`: a declared camera that is absent, or a video of the wrong length.

Skipped corpus-wide because no source here fetches pixels, so these unit tests are the only
proof that it fires at all.
"""

from __future__ import annotations

import numpy as np
from tests.factories import camera, meta

from rdp.domain.camera import CameraEncoding
from rdp.domain.capabilities import Capabilities
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.video_frame_mismatch import VideoFrameMismatch

N = 40
WITH_VIDEO = Capabilities(has_action=True, has_state=True, has_rgb=True, has_video=True)


def frames_of(n: int = N) -> FrameTable:
    values = np.arange(n, dtype=np.float64)
    return FrameTable(
        columns={
            "t": values * 0.1,
            "action.ee.x": values,
            "action.ee.y": values,
            "state.ee.x": values,
            "state.ee.y": values,
        }
    )


def test_a_video_as_long_as_the_episode_passes() -> None:
    view = meta(n_frames=N, capabilities=WITH_VIDEO, cameras=[camera(n_frames=N)])
    result = VideoFrameMismatch().evaluate(frames_of(), view)
    assert result.verdict is Verdict.PASS
    assert result.metrics["max_frame_delta"] == 0.0


def test_one_frame_of_slack_is_not_a_defect() -> None:
    """Encoders and row counts disagree by a frame at the boundaries."""
    view = meta(n_frames=N, capabilities=WITH_VIDEO, cameras=[camera(n_frames=N + 1)])
    assert VideoFrameMismatch().evaluate(frames_of(), view).verdict is Verdict.PASS


def test_a_video_of_the_wrong_length_fails_and_reports_both_counts() -> None:
    view = meta(n_frames=N, capabilities=WITH_VIDEO, cameras=[camera(n_frames=N + 12)])
    result = VideoFrameMismatch().evaluate(frames_of(), view)
    assert result.verdict is Verdict.FAIL
    assert result.metrics["n_mismatched"] == 1.0
    assert "52 vs 40" in result.reason


def test_a_camera_declared_but_absent_fails() -> None:
    view = meta(
        n_frames=N, capabilities=WITH_VIDEO, cameras=[camera(is_present=False, n_frames=None)]
    )
    result = VideoFrameMismatch().evaluate(frames_of(), view)
    assert result.verdict is Verdict.FAIL
    assert result.metrics["n_missing"] == 1.0
    assert "declared but absent" in result.reason


def test_an_unmeasured_frame_count_is_not_zero() -> None:
    view = meta(n_frames=N, capabilities=WITH_VIDEO, cameras=[camera(n_frames=None)])
    result = VideoFrameMismatch().evaluate(frames_of(), view)
    assert result.verdict is Verdict.SKIPPED
    assert result.metrics["n_measured"] == 0.0


def test_pixels_inlined_in_the_records_are_not_a_video_to_check() -> None:
    """Invariant 11: C has `has_rgb` and no standalone file, so the question cannot be asked."""
    view = meta(
        n_frames=N,
        capabilities=Capabilities(has_action=True, has_state=True, has_rgb=True),
        cameras=[camera(encoding=CameraEncoding.INLINE_FRAMES)],
    )
    result = evaluate_rule(VideoFrameMismatch(), frames_of(), view)
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == "capability_unmet:has_video"
