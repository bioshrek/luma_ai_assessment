"""`FPS_DRIFT`: a clock that disagrees with the declared rate, and dropped frames.

The most important case is the one where the rule refuses to answer: on a synthesized clock it
would measure the rate it was generated from and pass by construction.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import meta

from rdp.domain.frames import FrameTable
from rdp.domain.provenance import synthesized_at
from rdp.domain.qc.engine import SYNTHETIC_TIMESTAMP, evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.fps_drift import FpsDrift


def clock(t: np.ndarray) -> FrameTable:
    return FrameTable(columns={"t": np.asarray(t, dtype=np.float64)})


def test_a_clock_that_matches_the_declared_rate_passes() -> None:
    result = FpsDrift().evaluate(clock(np.arange(20) * 0.1), meta(fps_nominal=10.0))
    assert result.verdict is Verdict.PASS
    assert result.metrics["fps_drift"] == pytest.approx(0.0)
    assert result.metrics["n_gaps"] == 0.0


def test_a_clock_running_at_half_the_declared_rate_is_a_review() -> None:
    result = FpsDrift().evaluate(clock(np.arange(20) * 0.2), meta(fps_nominal=10.0))
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["fps_drift"] == pytest.approx(1.0)
    assert "off the declared" in result.reason


def test_a_dropped_frame_gap_is_a_fail_and_is_not_reported_as_drift() -> None:
    """A gap is measured against the episode's own median step, so the rate stays correct."""
    t = np.arange(20, dtype=np.float64) * 0.1
    t[10:] += 0.5
    result = FpsDrift().evaluate(clock(t), meta(fps_nominal=10.0))
    assert result.verdict is Verdict.FAIL
    assert result.metrics["n_gaps"] == 1.0
    assert result.metrics["fps_drift"] == pytest.approx(0.0)
    assert result.metrics["max_dt"] == pytest.approx(0.6)


def test_a_synthesized_clock_is_skipped_rather_than_passing_by_construction() -> None:
    result = evaluate_rule(
        FpsDrift(), clock(np.arange(20) * 0.1), meta(timestamp_source=synthesized_at(10.0))
    )
    assert result.verdict is Verdict.SKIPPED
    assert result.reason == SYNTHETIC_TIMESTAMP


def test_without_a_declared_rate_there_is_nothing_to_compare_against() -> None:
    result = FpsDrift().evaluate(clock(np.arange(20) * 0.1), meta(fps_nominal=None))
    assert result.verdict is Verdict.SKIPPED
    assert "nominal frame rate" in result.reason
