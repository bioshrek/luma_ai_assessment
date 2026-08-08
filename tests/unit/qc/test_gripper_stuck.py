"""`GRIPPER_STUCK`: the rule reads `is_delta` before it reads a value (ADR 003)."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import gripper_channel, meta, spec

from rdp.domain.capabilities import Capabilities
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.gripper_stuck import GripperStuck

N = 80
HAS_GRIPPER = Capabilities(has_action=True, has_state=True, has_gripper=True)


def frames_of(values: np.ndarray) -> FrameTable:
    return FrameTable(
        columns={
            "t": np.arange(values.size, dtype=np.float64) * 0.1,
            "action.gripper": values,
        }
    )


def view(is_delta: bool, n_frames: int = N):
    return meta(
        n_frames=n_frames,
        capabilities=HAS_GRIPPER,
        action_spec=spec(channels=(gripper_channel(is_delta=is_delta),)),
    )


def test_an_absolute_gripper_that_opens_and_closes_passes() -> None:
    values = np.linspace(0.0, 1.0, N)
    result = GripperStuck().evaluate(frames_of(values), view(is_delta=False))
    assert result.verdict is Verdict.PASS
    assert result.metrics["min_unique_values"] == float(N)


def test_an_absolute_gripper_frozen_at_one_opening_is_a_review() -> None:
    result = GripperStuck().evaluate(frames_of(np.full(N, 0.5)), view(is_delta=False))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["min_unique_values"] == 1.0
    assert "gripper" in result.reason


def test_a_delta_gripper_resting_at_zero_is_not_stuck_it_is_idle() -> None:
    """C's gripper is a ternary CHANGE command: 0 is what it says most of the time. Judged by
    B's "one unique value" test this episode would be condemned for being normal."""
    values = np.zeros(N)
    values[20] = 1.0
    values[60] = -1.0
    result = GripperStuck().evaluate(frames_of(values), view(is_delta=True))
    assert result.verdict is Verdict.PASS
    assert result.metrics["min_gripper_travel"] == pytest.approx(2.0)
    assert "min_unique_values" not in result.metrics


def test_a_delta_gripper_never_commanded_at_all_is_a_review() -> None:
    """Measured on 5 of source C's 12 episodes."""
    result = GripperStuck().evaluate(frames_of(np.zeros(N)), view(is_delta=True))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["min_gripper_travel"] == 0.0


def test_an_episode_too_short_to_reach_a_grasp_is_skipped() -> None:
    values = np.full(10, 0.5)
    result = GripperStuck().evaluate(frames_of(values), view(is_delta=False, n_frames=10))
    assert result.verdict is Verdict.SKIPPED
    assert "too short" in result.reason


def test_an_embodiment_without_a_gripper_never_reaches_the_rule() -> None:
    result = evaluate_rule(GripperStuck(), frames_of(np.zeros(N)), meta(n_frames=N))
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == "capability_unmet:has_gripper"
