"""`ACTION_JERK`: an isolated inter-frame discontinuity, and the two traps around it.

The corpus never exceeds `max|step| / p99|step|` of 5.63, so the episodes here are built by
hand: a smooth ramp, and the same ramp with one sample corrupted.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import flag_channel, gripper_channel, meta, spec, xy_channels

from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.action_jerk import ActionJerk

N = 200


def ramp(n: int = N, step: float = 0.1) -> np.ndarray:
    return np.arange(n, dtype=np.float64) * step


def table(**action: np.ndarray) -> FrameTable:
    n = len(next(iter(action.values())))
    columns = {"t": np.arange(n, dtype=np.float64) * 0.1}
    columns.update({f"action.{name}": values for name, values in action.items()})
    return FrameTable(columns=columns)


def xy_frames(x: np.ndarray, y: np.ndarray | None = None) -> FrameTable:
    return table(**{"ee.x": x, "ee.y": ramp(len(x)) if y is None else y})


def test_a_smooth_trajectory_has_no_jerk() -> None:
    result = ActionJerk().evaluate(xy_frames(ramp()), meta(n_frames=N))
    assert result.verdict is Verdict.PASS
    assert result.metrics["jerk_ratio"] == pytest.approx(1.0)
    assert result.metrics["n_jerks"] == 0.0
    assert result.metrics["n_channels_examined"] == 2.0


def test_one_corrupted_sample_is_a_review_and_names_its_channel() -> None:
    x = ramp()
    x[100] += 5.0
    result = ActionJerk().evaluate(xy_frames(x), meta(n_frames=N))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["jerk_ratio"] > 8.0
    assert result.metrics["n_jerks"] == 2.0  # the jump out and the jump back
    assert "ee.x" in result.reason


def test_a_spike_is_not_excused_by_its_own_echo() -> None:
    """A single bad sample is two adjacent large steps; a naive neighbourhood median would
    compare each against the other and rate the pair perfectly smooth."""
    x = ramp()
    x[100] += 5.0
    result = ActionJerk().evaluate(xy_frames(x), meta(n_frames=N))
    assert result.metrics["jerk_isolation"] > ActionJerk().min_isolation


def test_a_hard_but_gradual_acceleration_is_not_a_jerk() -> None:
    """Real motion raises consecutive steps together, which is the isolation test's whole job."""
    x = np.concatenate([ramp(100, 0.1), ramp(100, 0.1)[-1] + np.cumsum(np.full(100, 1.2))])
    result = ActionJerk().evaluate(xy_frames(x), meta(n_frames=N))
    assert result.verdict is Verdict.PASS


def test_a_non_physical_flag_is_excluded_from_the_statistics() -> None:
    """C's `terminate_episode` steps 0 -> 1 on the final frame. Counted, every C episode would
    be REVIEW; invariant 6 keeps it out (`physical_view`)."""
    flag = np.zeros(N)
    flag[-1] = 1.0
    frames = table(**{"ee.x": ramp(), "ee.y": ramp(), "flag.terminate_episode": flag})
    action = spec(channels=(*xy_channels(), flag_channel()))
    result = ActionJerk().evaluate(frames, meta(n_frames=N, action_spec=action))
    assert result.verdict is Verdict.PASS
    assert result.metrics["n_channels_examined"] == 2.0


def test_a_delta_channel_is_judged_on_its_commands_not_their_derivative() -> None:
    values = np.full(N, 0.5)
    values[100] = 6.0
    frames = table(gripper=values)
    action = spec(channels=(gripper_channel(is_delta=True),))
    result = ActionJerk().evaluate(frames, meta(n_frames=N, action_spec=action))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["n_jerks"] == 1.0  # one command, not a jump out and back


def test_an_episode_too_short_for_p99_to_mean_anything_is_skipped() -> None:
    result = ActionJerk().evaluate(xy_frames(ramp(10)), meta(n_frames=10))
    assert result.verdict is Verdict.SKIPPED
    assert result.metrics["n_channels_examined"] == 0.0


def test_a_channel_that_never_moves_is_left_to_the_rules_that_are_about_stillness() -> None:
    """Dividing by a p99 of zero would report an infinite jerk on a merely idle channel."""
    result = ActionJerk().evaluate(xy_frames(np.zeros(N), np.zeros(N)), meta(n_frames=N))
    assert result.verdict is Verdict.SKIPPED
