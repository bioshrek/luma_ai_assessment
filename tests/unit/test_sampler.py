"""Invariant 5: a subset is whole episodes. The budget is a ceiling, never a target."""

from __future__ import annotations

import pytest

from rdp.domain.curation.sampler import SEQUENTIAL, Candidate, plan_sequential
from rdp.domain.errors import BudgetTooSmall, InvariantViolation
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.subset import SubsetEntry, SubsetPlan


def _candidates(*lengths: int) -> list[Candidate]:
    return [
        Candidate(
            episode_uid=f"pusht:e{i}",
            n_frames=n,
            source_id="pusht",
            embodiment="pusht_planar",
            qc_verdict=EpisodeVerdict.PASS,
        )
        for i, n in enumerate(lengths)
    ]


def test_it_takes_whole_episodes_until_the_next_one_would_not_fit() -> None:
    plan = plan_sequential(_candidates(100, 100, 100), budget_frames=250)
    assert plan.total_frames == 200
    assert [e.episode_uid for e in plan.entries] == ["pusht:e0", "pusht:e1"]
    assert plan.strategy == SEQUENTIAL


def test_a_long_episode_is_skipped_rather_than_truncated() -> None:
    plan = plan_sequential(_candidates(300, 50), budget_frames=100)
    assert [e.episode_uid for e in plan.entries] == ["pusht:e1"]
    assert plan.total_frames == 50


def test_a_budget_below_the_shortest_episode_is_an_error_not_a_half_episode() -> None:
    with pytest.raises(BudgetTooSmall):
        plan_sequential(_candidates(300, 400), budget_frames=100)


def test_no_candidates_yields_an_empty_plan() -> None:
    assert plan_sequential([], budget_frames=100).entries == ()


def test_an_entry_must_span_the_whole_episode() -> None:
    with pytest.raises(InvariantViolation, match="invariant 5"):
        SubsetEntry(episode_uid="pusht:e0", n_frames=100, frame_start=0, frame_end=50)


def test_a_plan_may_not_exceed_its_budget_or_repeat_an_episode() -> None:
    entry = SubsetEntry(episode_uid="pusht:e0", n_frames=100, frame_start=0, frame_end=100)
    with pytest.raises(InvariantViolation):
        SubsetPlan(budget_frames=50, strategy=SEQUENTIAL, entries=(entry,))
    with pytest.raises(InvariantViolation):
        SubsetPlan(budget_frames=500, strategy=SEQUENTIAL, entries=(entry, entry))
