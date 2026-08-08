"""`TERMINATION_CONSISTENCY`: markers that disagree with where the episode ends."""

from __future__ import annotations

import numpy as np
from tests.factories import meta

from rdp.domain.capabilities import Capabilities
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.termination_consistency import TerminationConsistency

N = 40
COLUMN = "raw.next.done"
HAS_SIGNAL = Capabilities(has_action=True, has_state=True, has_termination_signal=True)


def frames_of(marked_at: list[int], n: int = N) -> FrameTable:
    done = np.zeros(n, dtype=np.float64)
    done[marked_at] = 1.0
    return FrameTable(
        columns={"t": np.arange(n, dtype=np.float64) * 0.1, COLUMN: done},
        raw_frame_columns=(COLUMN,),
    )


def view(termination_column: str | None = COLUMN):
    return meta(
        n_frames=N,
        capabilities=HAS_SIGNAL,
        termination_column=termination_column,
        raw_frame_columns=(COLUMN,),
    )


def test_a_marker_on_the_final_frame_passes() -> None:
    result = TerminationConsistency().evaluate(frames_of([N - 1]), view())
    assert result.verdict is Verdict.PASS
    assert result.metrics["terminal_run"] == 1.0
    assert result.metrics["n_interior_signals"] == 0.0


def test_pushts_two_frame_terminal_run_is_not_a_defect() -> None:
    """Measured: LeRobot's pusht sets `next.done` on the last TWO frames of all 80 episodes."""
    result = TerminationConsistency().evaluate(frames_of([N - 2, N - 1]), view())
    assert result.verdict is Verdict.PASS
    assert result.metrics["terminal_run"] == 2.0


def test_a_marker_with_ordinary_frames_after_it_means_episodes_were_concatenated() -> None:
    result = TerminationConsistency().evaluate(frames_of([19, N - 1]), view())
    assert result.verdict is Verdict.FAIL
    assert result.metrics["n_interior_signals"] == 1.0
    assert "concatenated" in result.reason


def test_an_episode_that_ends_without_a_marker_is_a_review() -> None:
    result = TerminationConsistency().evaluate(frames_of([]), view())
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["terminal_run"] == 0.0
    assert "without being marked" in result.reason


def test_a_marker_stuck_on_for_the_whole_tail_is_a_review() -> None:
    result = TerminationConsistency().evaluate(frames_of(list(range(N - 6, N))), view())
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["terminal_run"] == 6.0
    assert "stuck flag" in result.reason


def test_the_column_name_is_never_guessed() -> None:
    """A source that trimmed its markers away must not have one invented for it."""
    result = TerminationConsistency().evaluate(frames_of([N - 1]), view(termination_column=None))
    assert result.verdict is Verdict.SKIPPED
    assert "names no column" in result.reason


def test_a_source_without_an_end_signal_never_reaches_the_rule() -> None:
    result = evaluate_rule(TerminationConsistency(), frames_of([N - 1]), meta(n_frames=N))
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == "capability_unmet:has_termination_signal"
