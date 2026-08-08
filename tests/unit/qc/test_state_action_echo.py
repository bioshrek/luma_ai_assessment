"""`STATE_ACTION_ECHO`: bit-equality, never correlation. ALOHA is why."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import meta, pose_channels

from rdp.domain.action_spec import SignalLevel, SignalSpec
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.state_action_echo import StateActionEcho

N = 50


def frames_of(action: np.ndarray, state: np.ndarray) -> FrameTable:
    return FrameTable(
        columns={
            "t": np.arange(action.size, dtype=np.float64) * 0.1,
            "action.ee.x": action,
            "action.ee.y": action,
            "state.ee.x": state,
            "state.ee.y": state,
        }
    )


def test_a_command_the_follower_tracks_closely_still_passes() -> None:
    """The ALOHA case: correlation ~1.0, and the rule must not care. Its action really is a
    leader arm's position; only bit-equality would mean nobody commanded anything."""
    action = np.arange(N, dtype=np.float64)
    result = StateActionEcho().evaluate(frames_of(action, action - 0.004), meta(n_frames=N))
    assert result.verdict is Verdict.PASS
    assert result.metrics["correlation"] > 0.999
    assert result.metrics["echo_fraction"] == 0.0
    assert result.metrics["max_abs_difference"] == pytest.approx(0.004)


def test_an_action_copied_from_state_is_a_review() -> None:
    action = np.arange(N, dtype=np.float64)
    result = StateActionEcho().evaluate(frames_of(action, action.copy()), meta(n_frames=N))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["echo_fraction"] == pytest.approx(1.0)
    assert "readback" in result.reason


def test_a_few_coincidentally_equal_frames_are_not_an_echo() -> None:
    """Measured: pusht is bit-equal on up to 0.0068 of frames and is not a readback."""
    action = np.arange(N, dtype=np.float64)
    state = action - 0.5
    state[:3] = action[:3]
    result = StateActionEcho().evaluate(frames_of(action, state), meta(n_frames=N))
    assert result.verdict is Verdict.PASS
    assert result.metrics["echo_fraction"] == pytest.approx(0.06)


def test_signals_in_different_spaces_are_not_compared_at_all() -> None:
    """A precondition the data decides, so it cannot be a declarative gate."""
    pose_state = SignalSpec(
        is_command=False, level=SignalLevel.PER_FRAME_CONTINUOUS, channels=pose_channels()
    )
    action = np.arange(N, dtype=np.float64)
    result = StateActionEcho().evaluate(
        frames_of(action, action), meta(n_frames=N, state_spec=pose_state)
    )
    assert result.verdict is Verdict.SKIPPED
    assert "not comparable" in result.reason
