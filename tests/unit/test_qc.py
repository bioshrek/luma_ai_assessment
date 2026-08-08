"""Invariant 4: gating is the engine's job, and a degraded rule reports SKIPPED, not PASS."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pytest
from tests.factories import frames, meta

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import synthesized_at
from rdp.domain.qc.engine import SYNTHETIC_TIMESTAMP, evaluate_all, evaluate_rule, gate, roll_up
from rdp.domain.qc.rule import EpisodeVerdict, QCEpisodeView, RuleResult, Severity, Verdict
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
