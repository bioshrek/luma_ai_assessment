"""`step_magnitudes` — the one definition of "how much did this channel move in one step".

`STATIC_EPISODE` and `ACTION_JERK` share it precisely so they cannot disagree about a delta
channel, which is the mistake ADR 003 warns about.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import gripper_channel, xy_channels

from rdp.domain.qc.rules._support import step_magnitudes

ABSOLUTE = xy_channels()[0]
DELTA = gripper_channel(is_delta=True)


def test_an_absolute_channel_moves_by_the_difference_between_samples() -> None:
    steps = step_magnitudes(np.array([0.0, 1.0, 3.0, 2.5]), ABSOLUTE)
    assert steps == pytest.approx([1.0, 2.0, 0.5])


def test_a_delta_channel_is_already_the_difference_and_is_not_differenced_again() -> None:
    """Differencing it twice would report acceleration and call a steady command motionless."""
    values = np.array([0.5, 0.5, 0.5, 0.5])
    assert step_magnitudes(values, DELTA) == pytest.approx([0.5, 0.5, 0.5])
    # What the wrong implementation would have said about the same, plainly moving, channel:
    assert np.abs(np.diff(values)).sum() == 0.0


def test_both_kinds_report_one_value_per_step_so_they_can_be_pooled() -> None:
    values = np.arange(6, dtype=np.float64)
    assert step_magnitudes(values, ABSOLUTE).size == 5
    assert step_magnitudes(values, DELTA).size == 5


def test_magnitudes_are_never_negative() -> None:
    steps = step_magnitudes(np.array([0.0, -4.0, 1.0]), ABSOLUTE)
    assert (steps >= 0).all()
    assert step_magnitudes(np.array([0.0, -4.0, 1.0]), DELTA) == pytest.approx([4.0, 1.0])


def test_a_single_frame_episode_has_no_steps_at_all() -> None:
    assert step_magnitudes(np.array([1.0]), ABSOLUTE).size == 0
    assert step_magnitudes(np.array([]), DELTA).size == 0
