"""The stratified strategy: who gets how much of the budget, and why (design §6.3).

The quota arithmetic is pinned by hand-computed numbers rather than by re-deriving it in the
test, because a sampler that silently drifts still returns a plausible-looking subset.
"""

from __future__ import annotations

import pytest

from rdp.domain.curation.sampler import BALANCED, Candidate, plan_balanced
from rdp.domain.errors import BudgetTooSmall
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.subset import SubsetPlan


def _group(
    embodiment: str,
    count: int,
    n_frames: int,
    *,
    source_id: str | None = None,
    task: str = "t",
    verdict: EpisodeVerdict = EpisodeVerdict.PASS,
) -> list[Candidate]:
    source = source_id or embodiment
    return [
        Candidate(
            episode_uid=f"{source}:{task}:{index:03d}",
            n_frames=n_frames,
            source_id=source,
            embodiment=embodiment,
            qc_verdict=verdict,
            task=task,
        )
        for index in range(count)
    ]


def _selected(plan: SubsetPlan) -> dict[str, int]:
    return {group.embodiment: group.selected_frames for group in plan.groups}


def test_square_root_smoothing_and_the_cap_split_a_hundred_fold_spread() -> None:
    """400 / 2 000 / 10 000 / 40 000 eligible frames — a 100x spread between the extremes.

    sqrt weights are .055 / .123 / .274 / .548; the cap pins `huge` at .40 and the other three
    renormalise over the remaining .60 to .073 / .163 / .364.
    """
    candidates = [
        *_group("tiny", 4, 100),
        *_group("small", 10, 200),
        *_group("mid", 20, 500),
        *_group("huge", 40, 1000),
    ]
    plan = plan_balanced(candidates, 10_000)

    weights = {g.embodiment: g.weight for g in plan.groups}
    assert weights["huge"] == pytest.approx(0.40)
    assert weights["mid"] == pytest.approx(0.3643, abs=1e-4)
    assert weights["small"] == pytest.approx(0.1629, abs=1e-4)
    assert weights["tiny"] == pytest.approx(0.0728, abs=1e-4)
    quotas = {g.embodiment: g.quota_frames for g in plan.groups}
    assert quotas["huge"] == 4000
    # Proportional-to-size would have given `huge` 40 000/52 400 = 76% of the budget; smoothing
    # plus the cap is what keeps the small embodiments in the training mix at all. `tiny` holds
    # only 400 frames against a 728-frame quota, so it is exhausted and `mid` absorbs the rest.
    assert _selected(plan) == {"huge": 4000, "mid": 4000, "small": 1600, "tiny": 400}
    assert plan.total_frames == 10_000


def test_the_floor_lifts_a_group_that_smoothing_alone_would_starve() -> None:
    candidates = [
        *_group("a", 10, 100_000),
        *_group("b", 10, 100_000),
        *_group("c", 10, 100_000),
        *_group("rare", 10, 10),
    ]
    plan = plan_balanced(candidates, 100_000)

    weights = {g.embodiment: g.weight for g in plan.groups}
    # Unsmoothed this group is 0.003% of the corpus and sqrt smoothing still leaves it at 0.3%.
    assert weights["rare"] == pytest.approx(0.05)
    assert weights["a"] == pytest.approx(0.95 / 3, abs=1e-4)


def test_a_group_that_runs_out_releases_the_rest_of_its_quota() -> None:
    """Residual redistribution: an unspendable quota is re-offered, not left on the table."""
    plan = plan_balanced([*_group("scarce", 2, 100), *_group("plentiful", 50, 100)], 2000)

    selected = _selected(plan)
    assert selected["scarce"] == 200  # everything it had
    assert selected["plentiful"] == 1800  # 900 more than its own 50% quota
    assert plan.total_frames == 2000


def test_clean_episodes_are_taken_before_episodes_under_review() -> None:
    candidates = [
        *_group("arm", 3, 100, task="clean"),
        *_group("arm", 3, 100, task="flagged", verdict=EpisodeVerdict.REVIEW),
    ]
    plan = plan_balanced(candidates, 400)

    assert [entry.episode_uid for entry in plan.entries] == [
        "arm:clean:000",
        "arm:clean:001",
        "arm:clean:002",
        "arm:flagged:000",
    ]


def test_round_robin_stops_one_task_from_eating_a_whole_group_quota() -> None:
    candidates = [*_group("arm", 5, 100, task="pick"), *_group("arm", 5, 100, task="place")]
    plan = plan_balanced(candidates, 400)

    tasks = [entry.episode_uid.split(":")[1] for entry in plan.entries]
    assert tasks == ["pick", "place", "pick", "place"]


def test_the_same_seed_selects_the_same_episodes_and_a_different_seed_does_not() -> None:
    candidates = _group("arm", 40, 100)
    first = plan_balanced(candidates, 1000, seed=7)
    again = plan_balanced(candidates, 1000, seed=7)
    other = plan_balanced(candidates, 1000, seed=8)

    assert [e.episode_uid for e in first.entries] == [e.episode_uid for e in again.entries]
    assert [e.episode_uid for e in first.entries] != [e.episode_uid for e in other.entries]
    assert first.total_frames == other.total_frames == 1000


def test_an_unseeded_export_is_reproducible_too() -> None:
    candidates = _group("arm", 10, 100)
    assert [e.episode_uid for e in plan_balanced(candidates, 500).entries] == [
        f"arm:t:{index:03d}" for index in range(5)
    ]


def test_the_shortfall_is_smaller_than_every_episode_left_behind() -> None:
    candidates = [*_group("a", 5, 700), *_group("b", 5, 300)]
    plan = plan_balanced(candidates, 2500)

    chosen = {entry.episode_uid for entry in plan.entries}
    shortfall = plan.budget_frames - plan.total_frames
    assert plan.total_frames <= plan.budget_frames
    assert all(c.n_frames > shortfall for c in candidates if c.episode_uid not in chosen)


def test_one_embodiment_takes_the_whole_budget() -> None:
    plan = plan_balanced(_group("aloha_bimanual", 10, 100), 500)

    assert plan.groups[0].weight == pytest.approx(1.0)
    assert plan.total_frames == 500


def test_a_budget_below_the_shortest_episode_is_an_error_not_a_half_episode() -> None:
    with pytest.raises(BudgetTooSmall):
        plan_balanced(_group("arm", 3, 300), 100)


def test_no_candidates_yields_an_empty_plan_that_still_names_its_strategy() -> None:
    plan = plan_balanced([], 100, seed=3)
    assert plan.entries == () and plan.strategy == BALANCED and plan.seed == 3


def test_the_plan_reports_what_each_group_was_offered_and_what_it_took() -> None:
    plan = plan_balanced([*_group("a", 2, 100), *_group("b", 50, 100)], 2000, seed=1)

    stats = plan.stats()
    assert stats["strategy"] == BALANCED
    assert stats["seed"] == 1
    assert stats["used_frames"] + stats["shortfall_frames"] == stats["budget_frames"]
    assert {group["embodiment"] for group in stats["groups"]} == {"a", "b"}
    assert sum(group["selected_frames"] for group in stats["groups"]) == plan.total_frames
