"""Invariant 4: gating is the engine's job, and a degraded rule reports SKIPPED, not PASS."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pytest
from tests.factories import frames, meta, pose_frames, pose_meta

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import synthesized_at
from rdp.domain.qc.engine import (
    SYNTHETIC_TIMESTAMP,
    downgrade_basis,
    evaluate_all,
    evaluate_rule,
    gate,
    roll_up,
)
from rdp.domain.qc.rule import EpisodeVerdict, QCEpisodeView, RuleResult, Severity, Verdict
from rdp.domain.qc.rules.action_range import ActionRange
from rdp.domain.qc.rules.pose_coverage import PoseCoverage
from rdp.domain.qc.rules.ts_monotonic import RULE_ID, TsMonotonic


@dataclass(frozen=True)
class _Rule:
    """A stand-in rule whose verdict the test dictates."""

    verdict: Verdict = Verdict.PASS
    raises: bool = False
    rule_id: str = "STUB"
    severity: Severity = Severity.FAIL
    required_capabilities: frozenset[str] = frozenset()
    required_levels: Mapping[str, SignalLevel] = field(default_factory=dict)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        if self.raises:
            raise ZeroDivisionError("rule bug")
        return RuleResult(self.rule_id, self.verdict)


def test_a_missing_capability_names_itself_in_the_skip_reason() -> None:
    rule = _Rule(required_capabilities=frozenset({"has_gripper"}))
    assert gate(rule, meta()) == "capability_unmet:has_gripper"


def test_a_wrong_signal_level_is_distinguished_from_a_missing_signal() -> None:
    rule = _Rule(required_levels={"action": SignalLevel.PER_FRAME_CONTINUOUS})
    assert gate(rule, meta()) is None
    labelled = meta(action_level=SignalLevel.EPISODE_LABEL)
    assert gate(rule, labelled) == "action_level_is_episode_label"
    absent = meta(action_level=SignalLevel.ABSENT)
    assert gate(rule, absent) == "action_level_is_absent"


def test_synthesized_timestamps_gate_out_the_timestamp_rules() -> None:
    assert gate(TsMonotonic(), meta()) is None
    assert gate(TsMonotonic(), meta(timestamp_source=synthesized_at(10.0))) == SYNTHETIC_TIMESTAMP


def test_every_result_carries_n_frames_even_when_skipped() -> None:
    result = evaluate_rule(_Rule(required_capabilities=frozenset({"has_imu"})), frames(), meta())
    assert result.verdict is Verdict.SKIPPED
    assert result.metrics["n_frames"] == 4.0


def test_a_rule_that_raises_becomes_ERROR_and_the_run_continues() -> None:
    results = evaluate_all([_Rule(raises=True), _Rule()], frames(), meta())
    assert [r.verdict for r in results] == [Verdict.ERROR, Verdict.PASS]
    assert "ZeroDivisionError" in results[0].reason


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ((Verdict.PASS, Verdict.SKIPPED), EpisodeVerdict.PASS),
        ((), EpisodeVerdict.PASS),
        ((Verdict.SKIPPED,), EpisodeVerdict.PASS),
        ((Verdict.REVIEW, Verdict.PASS), EpisodeVerdict.REVIEW),
        ((Verdict.ERROR, Verdict.PASS), EpisodeVerdict.REVIEW),
        ((Verdict.FAIL, Verdict.REVIEW), EpisodeVerdict.FAIL),
    ],
)
def test_roll_up_lets_fail_dominate_and_sends_errors_to_review(
    verdicts: tuple[Verdict, ...], expected: EpisodeVerdict
) -> None:
    assert roll_up([RuleResult("R", v) for v in verdicts]) is expected


def test_ts_monotonic_passes_on_a_strictly_increasing_clock() -> None:
    result = TsMonotonic().evaluate(frames(), meta())
    assert result.verdict is Verdict.PASS
    assert result.metrics["min_dt"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "t",
    [
        [0.0, 0.1, 0.1, 0.2],  # duplicate
        [0.0, 0.2, 0.1, 0.3],  # out of order
        [0.0, 0.1, float("nan"), 0.3],  # not finite
    ],
)
def test_ts_monotonic_fails_on_a_broken_clock(t: list[float]) -> None:
    table = FrameTable(columns={"t": np.array(t, dtype=np.float64)})
    result = TsMonotonic().evaluate(table, meta())
    assert result.verdict is Verdict.FAIL
    assert result.rule_id == RULE_ID


def test_ts_monotonic_has_nothing_to_compare_in_a_single_frame_episode() -> None:
    table = FrameTable(columns={"t": np.zeros(1)})
    assert TsMonotonic().evaluate(table, meta()).verdict is Verdict.PASS


# -- invariant 13: a FAIL read entirely off non-measured channels is a REVIEW ------------------


def test_a_fail_on_estimated_channels_only_is_downgraded_and_says_why() -> None:
    rule = _Rule(verdict=Verdict.FAIL, required_levels={"state": SignalLevel.PER_FRAME_CONTINUOUS})
    result = evaluate_rule(rule, pose_frames([True] * 4), pose_meta())
    assert result.verdict is Verdict.REVIEW
    # The basis is in the reason, so nobody has to reverse-engineer why FAIL became REVIEW.
    assert "origin=estimated" in result.reason
    assert "invariant 13" in result.reason


def test_the_same_fail_stands_when_the_rule_reads_a_measured_channel() -> None:
    rule = _Rule(verdict=Verdict.FAIL, required_levels={"state": SignalLevel.PER_FRAME_CONTINUOUS})
    assert evaluate_rule(rule, frames(), meta()).verdict is Verdict.FAIL


def test_a_rule_that_declares_no_signals_is_never_downgraded() -> None:
    """`downgrade_basis` must not turn "we know nothing" into "nothing was measured"."""
    assert downgrade_basis(_Rule(verdict=Verdict.FAIL), pose_meta()) is None
    assert evaluate_rule(_Rule(verdict=Verdict.FAIL), frames(), pose_meta()).verdict is Verdict.FAIL


def test_only_a_fail_is_downgraded() -> None:
    levels = {"state": SignalLevel.PER_FRAME_CONTINUOUS}
    rule = _Rule(verdict=Verdict.REVIEW, required_levels=levels)
    assert evaluate_rule(rule, pose_frames([True] * 4), pose_meta()).verdict is Verdict.REVIEW


# -- ACTION_RANGE -----------------------------------------------------------------------------


def test_action_range_passes_on_values_inside_the_declared_bounds() -> None:
    result = evaluate_rule(ActionRange(), frames(), meta())
    assert result.verdict is Verdict.PASS
    assert result.metrics["n_out_of_range"] == 0.0


def test_action_range_fails_on_a_non_finite_action_and_names_the_channel() -> None:
    table = frames()
    columns = {name: table.column(name) for name in table.column_names}
    columns["action.ee.x"] = np.array([0.0, 1.0, np.inf, 3.0])
    result = evaluate_rule(ActionRange(), FrameTable(columns=columns), meta())
    assert result.verdict is Verdict.FAIL
    assert "ee.x" in result.reason
    assert result.metrics["n_non_finite"] == 1.0


def test_action_range_skips_an_episode_level_label_rather_than_failing_it() -> None:
    result = evaluate_rule(ActionRange(), pose_frames([True] * 4), pose_meta())
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == "action_level_is_episode_label"


# -- POSE_COVERAGE ----------------------------------------------------------------------------


def test_pose_coverage_passes_when_the_reconstruction_is_complete() -> None:
    result = evaluate_rule(PoseCoverage(), pose_frames([True] * 10), pose_meta())
    assert result.verdict is Verdict.PASS
    assert result.metrics["pose_coverage"] == pytest.approx(1.0)
    assert result.metrics["longest_gap_s"] == pytest.approx(0.0)


def test_sparse_holes_and_one_long_hole_are_different_complaints() -> None:
    scattered = [True, False, True, False, True, False, True, True, True, True]
    result = evaluate_rule(PoseCoverage(min_coverage=0.9), pose_frames(scattered), pose_meta())
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["pose_coverage"] == pytest.approx(0.7)
    assert "coverage" in result.reason

    contiguous = [True] * 4 + [False] * 6 + [True] * 10
    gap = evaluate_rule(PoseCoverage(min_coverage=0.5), pose_frames(contiguous), pose_meta())
    assert gap.verdict is Verdict.REVIEW
    # Six missing frames at 10 Hz: from the first frame without a pose to the one that has one
    # again. Measured on the clock, so the same hole in a 50 fps video is a smaller complaint.
    assert gap.metrics["longest_gap_s"] == pytest.approx(0.6)
    assert "unregistered run" in gap.reason


def test_pose_coverage_is_a_review_never_a_fail() -> None:
    """A hole in a COLMAP reconstruction is a fact about the model, not about the data."""
    assert PoseCoverage().severity is Severity.REVIEW
    result = evaluate_rule(PoseCoverage(min_coverage=1.0), pose_frames([False] * 4), pose_meta())
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["n_unregistered"] == 4.0
