"""Subset selection strategies. Pure functions of candidates + budget (design §6)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from rdp.domain.errors import BudgetTooSmall
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.subset import GroupAllocation, SubsetEntry, SubsetPlan

SEQUENTIAL = "sequential"
BALANCED = "balanced"

# Shares of the whole budget, applied to the square-root-smoothed weights (design §6.3): no
# embodiment takes more than 40% of a budget it has to share, none is squeezed below 5%.
DEFAULT_CAP_SHARE = 0.40
DEFAULT_FLOOR_SHARE = 0.05


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_uid: str = Field(min_length=1)
    n_frames: int = Field(gt=0)
    source_id: str
    embodiment: str
    qc_verdict: EpisodeVerdict
    task: str | None = None


def plan_sequential(candidates: Sequence[Candidate], budget_frames: int) -> SubsetPlan:
    """Take whole episodes in the given order until the next one would not fit.

    Never truncates: the frame budget is a ceiling, not a target. If even the shortest candidate
    exceeds the budget the caller is told rather than handed a fabricated half-episode.
    """
    if not candidates:
        return SubsetPlan(budget_frames=budget_frames, strategy=SEQUENTIAL)
    _reject_unusable_budget(candidates, budget_frames)
    entries: list[SubsetEntry] = []
    used = 0
    for candidate in candidates:
        if used + candidate.n_frames > budget_frames:
            continue
        entries.append(_entry(candidate))
        used += candidate.n_frames
    return SubsetPlan(budget_frames=budget_frames, strategy=SEQUENTIAL, entries=tuple(entries))


def plan_balanced(
    candidates: Sequence[Candidate],
    budget_frames: int,
    *,
    seed: int | None = None,
    floor_share: float = DEFAULT_FLOOR_SHARE,
    cap_share: float = DEFAULT_CAP_SHARE,
) -> SubsetPlan:
    """Stratify by embodiment, quality first, round-robin over `(source, task)` (design §6).

    Strictly proportional quotas would let a 50 Hz embodiment drown a 10 Hz one — five times the
    frames is not five times the information — so the between-group weights are square-root
    smoothed and then clamped into `[floor, cap]`. Episodes are still whole, always.
    """
    if not candidates:
        return SubsetPlan(budget_frames=budget_frames, strategy=BALANCED, seed=seed)
    _reject_unusable_budget(candidates, budget_frames)

    members: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        members.setdefault(candidate.embodiment, []).append(candidate)
    frames = {name: sum(c.n_frames for c in group) for name, group in members.items()}
    weights = _smoothed_weights(frames, floor_share=floor_share, cap_share=cap_share)
    groups = [
        _Group(
            embodiment=name,
            weight=weights[name],
            queue=_order_group(members[name], seed),
            eligible_episodes=len(members[name]),
            eligible_frames=frames[name],
        )
        for name in sorted(members)
    ]

    used = 0
    for group in groups:
        group.quota = int(group.weight * budget_frames)
        used += group.fill(ceiling=budget_frames - used)
    used += _redistribute(groups, budget_frames, used)

    return SubsetPlan(
        budget_frames=budget_frames,
        strategy=BALANCED,
        seed=seed,
        entries=tuple(_entry(c) for group in groups for c in group.taken),
        groups=tuple(group.allocation() for group in groups),
    )


def _redistribute(groups: Sequence[_Group], budget_frames: int, used: int) -> int:
    """Re-offer what the first pass could not place, until nothing fits anywhere any more.

    A group whose episodes ran out before its quota did releases the rest; the cap is not
    re-applied here, because leaving budget unspent to honour a cap nobody is competing for
    would trade real training frames for a tidy-looking table.
    """
    gained_total = 0
    while True:
        leftover = budget_frames - used - gained_total
        hungry = [
            group
            for group in sorted(groups, key=lambda g: (-g.weight, g.embodiment))
            if group.can_take(leftover)
        ]
        if not hungry:
            return gained_total
        total_weight = sum(group.weight for group in hungry)
        gained = 0
        for group in hungry:
            group.quota += int(leftover * group.weight / total_weight)
            gained += group.fill(ceiling=budget_frames - used - gained_total - gained)
        if gained == 0:
            # Every proportional share rounded away. Hand the remainder out in weight order
            # rather than stall one rounding error short of the budget.
            for group in hungry:
                group.quota = budget_frames
                gained += group.fill(ceiling=budget_frames - used - gained_total - gained)
            return gained_total + gained
        gained_total += gained


@dataclass
class _Group:
    """One stratum: its share of the budget and the queue it draws from, in selection order."""

    embodiment: str
    weight: float
    queue: list[Candidate]
    eligible_episodes: int
    eligible_frames: int
    quota: int = 0
    used: int = 0
    taken: list[Candidate] = field(default_factory=list)

    def can_take(self, ceiling: int) -> bool:
        return any(candidate.n_frames <= ceiling for candidate in self.queue)

    def fill(self, *, ceiling: int) -> int:
        """Take whole episodes that fit both this group's quota and the global budget left."""
        gained = 0
        skipped: list[Candidate] = []
        for candidate in self.queue:
            if candidate.n_frames <= min(self.quota - self.used, ceiling - gained):
                self.taken.append(candidate)
                self.used += candidate.n_frames
                gained += candidate.n_frames
            else:
                skipped.append(candidate)
        self.queue = skipped
        return gained

    def allocation(self) -> GroupAllocation:
        return GroupAllocation(
            embodiment=self.embodiment,
            eligible_episodes=self.eligible_episodes,
            eligible_frames=self.eligible_frames,
            weight=self.weight,
            quota_frames=self.quota,
            selected_episodes=len(self.taken),
            selected_frames=self.used,
        )


def _entry(candidate: Candidate) -> SubsetEntry:
    return SubsetEntry(
        episode_uid=candidate.episode_uid,
        n_frames=candidate.n_frames,
        frame_start=0,
        frame_end=candidate.n_frames,
    )


def _reject_unusable_budget(candidates: Sequence[Candidate], budget_frames: int) -> None:
    shortest = min(candidate.n_frames for candidate in candidates)
    if shortest > budget_frames:
        raise BudgetTooSmall(
            f"budget {budget_frames} frames is below the shortest eligible episode ({shortest} "
            "frames); episodes are never truncated"
        )


def _smoothed_weights(
    frames: Mapping[str, int], *, floor_share: float, cap_share: float
) -> dict[str, float]:
    roots = {name: math.sqrt(count) for name, count in frames.items()}
    total = sum(roots.values())
    weights = {name: root / total for name, root in roots.items()}
    even = 1.0 / len(weights)
    # With many groups the floor cannot be honoured for all of them, and with few the cap can sit
    # below an equal split; both bounds give way to the equal share rather than to a set of
    # weights that does not sum to one.
    return _clamp(weights, floor=min(floor_share, even), cap=max(cap_share, even))


def _clamp(weights: Mapping[str, float], *, floor: float, cap: float) -> dict[str, float]:
    """Clamp into `[floor, cap]` and renormalise the rest, repeating until nothing violates."""
    free = dict(weights)
    fixed: dict[str, float] = {}
    remaining = 1.0
    while free:
        total = sum(free.values())
        scaled = {name: value / total * remaining for name, value in free.items()}
        violating = {
            name: floor if value < floor else cap
            for name, value in scaled.items()
            if value < floor or value > cap
        }
        if not violating:
            fixed.update(scaled)
            break
        for name, bound in violating.items():
            fixed[name] = bound
            remaining -= bound
            del free[name]
    return {name: fixed[name] for name in weights}


def _order_group(candidates: Sequence[Candidate], seed: int | None) -> list[Candidate]:
    """Clean episodes before REVIEW ones, then round-robin over `(source, task)`.

    The round-robin is a degenerate identity while each embodiment has exactly one source and one
    task; it is what stops a single task consuming a whole embodiment's quota once that changes.
    """
    ordered: list[Candidate] = []
    for tier in sorted({_quality_rank(c) for c in candidates}):
        buckets: dict[tuple[str, str], list[Candidate]] = {}
        for candidate in candidates:
            if _quality_rank(candidate) == tier:
                buckets.setdefault((candidate.source_id, candidate.task or ""), []).append(
                    candidate
                )
        for bucket in buckets.values():
            bucket.sort(key=lambda c: (_shuffle_key(c.episode_uid, seed), c.episode_uid))
        keys = sorted(buckets)
        for index in range(max(len(buckets[key]) for key in keys)):
            ordered.extend(buckets[key][index] for key in keys if index < len(buckets[key]))
    return ordered


def _quality_rank(candidate: Candidate) -> int:
    return 0 if candidate.qc_verdict is EpisodeVerdict.PASS else 1


def _shuffle_key(episode_uid: str, seed: int | None) -> str:
    """A seeded permutation that does not depend on iteration order, so a resume cannot skew it.

    Without a seed the order is the episode uid, which is what makes an unseeded export as
    reproducible as a seeded one.
    """
    if seed is None:
        return ""
    return hashlib.blake2b(f"{seed}:{episode_uid}".encode(), digest_size=16).hexdigest()
