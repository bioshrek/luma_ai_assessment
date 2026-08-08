"""`STATIC_EPISODE`: the only ungated rule, and its three independently degrading tests."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import gripper_channel, meta, pose_frames, pose_meta, spec, xy_channels

from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.static_episode import StaticEpisode

N = 30


def bounded_channels(low: float = 0.0, high: float = 100.0) -> tuple:
    return tuple(c.model_copy(update={"min": low, "max": high}) for c in xy_channels())


def frames_of(x: np.ndarray) -> FrameTable:
    n = x.size
    return FrameTable(
        columns={
            "t": np.arange(n, dtype=np.float64) * 0.1,
            "action.ee.x": x,
            "action.ee.y": x,
            "state.ee.x": x,
            "state.ee.y": x,
        }
    )


def test_an_episode_that_is_long_and_moves_passes() -> None:
    result = StaticEpisode().evaluate(frames_of(np.arange(N, dtype=np.float64)), meta(n_frames=N))
    assert result.verdict is Verdict.PASS
    assert result.metrics["still_fraction"] == 0.0


def test_an_episode_shorter_than_any_demonstration_fails_on_its_length_alone() -> None:
    result = StaticEpisode().evaluate(frames_of(np.arange(4, dtype=np.float64)), meta(n_frames=4))
    assert result.verdict is Verdict.FAIL
    assert "4 frames" in result.reason


def test_a_recorder_that_stalled_repeats_values_bit_for_bit() -> None:
    result = StaticEpisode().evaluate(frames_of(np.ones(N)), meta(n_frames=N))
    assert result.verdict is Verdict.FAIL
    assert result.metrics["still_fraction"] == pytest.approx(1.0)
    # No channel declares limits here, so the travel test is not attempted rather than passed.
    assert "travel_fraction" not in result.metrics


def test_travel_is_judged_against_the_channel_s_own_declared_range() -> None:
    """The only way a pusher in pixels and a joint in radians can be asked the same question."""
    action = spec(channels=bounded_channels(0.0, 100.0))
    crawling = np.arange(N, dtype=np.float64) * 0.01  # 0.29 of travel over a range of 100
    result = StaticEpisode().evaluate(frames_of(crawling), meta(n_frames=N, action_spec=action))
    assert result.verdict is Verdict.FAIL
    assert result.metrics["travel_fraction"] == pytest.approx(0.0029)
    assert "declared range" in result.reason


def test_the_same_travel_over_a_smaller_declared_range_is_fine() -> None:
    action = spec(channels=bounded_channels(0.0, 1.0))
    result = StaticEpisode().evaluate(
        frames_of(np.arange(N, dtype=np.float64) * 0.01), meta(n_frames=N, action_spec=action)
    )
    assert result.verdict is Verdict.PASS
    assert result.metrics["travel_fraction"] == pytest.approx(0.29)


def test_a_grippers_declared_range_is_not_a_distance() -> None:
    """Regression: berkeley_ur5's arm channels declare no limits and its gripper does, so the
    travel test landed on the gripper alone and failed five episodes whose arm moved throughout.
    Whether a gripper actuates is `GRIPPER_STUCK`'s question."""
    gripper = gripper_channel().model_copy(update={"min": -1.0, "max": 1.0})
    action = spec(channels=(*xy_channels(), gripper))
    moving = np.arange(N, dtype=np.float64)
    frames = FrameTable(
        columns={
            "t": np.arange(N, dtype=np.float64) * 0.1,
            "action.ee.x": moving,
            "action.ee.y": moving,
            "action.gripper": np.zeros(N),
            "state.ee.x": moving,
            "state.ee.y": moving,
        }
    )
    result = StaticEpisode().evaluate(frames, meta(n_frames=N, action_spec=action))
    assert result.verdict is Verdict.PASS
    assert "travel_fraction" not in result.metrics


def test_a_quaternion_component_is_not_a_distance_either() -> None:
    """Regression: EPIC's camera quaternion declares [-1, 1], the only bounded channel D has.
    The fraction of that interval it sweeps says nothing about how far the camera travelled."""
    result = evaluate_rule(StaticEpisode(), pose_frames([True] * N), pose_meta(n_frames=N))
    assert "travel_fraction" not in result.metrics


def test_a_hole_in_the_data_is_not_stillness() -> None:
    """D's unregistered pose frames are NaN. Counting them as "did not move" would condemn
    every episode the reconstruction failed on."""
    result = evaluate_rule(StaticEpisode(), pose_frames([False] * N), pose_meta(n_frames=N))
    assert result.verdict is Verdict.PASS
    assert "still_fraction" not in result.metrics
    assert "travel_fraction" not in result.metrics


def test_a_frozen_reconstruction_is_a_review_not_a_fail() -> None:
    """Invariant 13, applied by the rule itself because it is ungated: a camera pose that never
    moves is a fact about COLMAP, not evidence that the recording is corrupt."""
    result = evaluate_rule(StaticEpisode(), pose_frames([True] * N), pose_meta(n_frames=N))
    assert result.verdict is Verdict.REVIEW
    assert "origin=estimated" in result.reason
    assert "invariant 13" in result.reason


def test_a_short_estimated_episode_still_fails_on_its_length() -> None:
    """Its 4 frames are a fact about the episode, not about the reconstruction."""
    result = evaluate_rule(StaticEpisode(), pose_frames([True] * 4), pose_meta(n_frames=4))
    assert result.verdict is Verdict.FAIL
