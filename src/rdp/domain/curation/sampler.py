"""Subset selection strategies. Pure functions of candidates + budget (design §6)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from rdp.domain.errors import BudgetTooSmall
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.subset import SubsetEntry, SubsetPlan

SEQUENTIAL = "sequential"


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_uid: str = Field(min_length=1)
    n_frames: int = Field(gt=0)
    source_id: str
    embodiment: str
    qc_verdict: EpisodeVerdict


def plan_sequential(candidates: Sequence[Candidate], budget_frames: int) -> SubsetPlan:
    """Take whole episodes in the given order until the next one would not fit.

    Never truncates: the frame budget is a ceiling, not a target. If even the shortest candidate
    exceeds the budget the caller is told rather than handed a fabricated half-episode.
    """
    if not candidates:
        return SubsetPlan(budget_frames=budget_frames, strategy=SEQUENTIAL, entries=())
    shortest = min(candidate.n_frames for candidate in candidates)
    if shortest > budget_frames:
        raise BudgetTooSmall(
            f"budget {budget_frames} frames is below the shortest eligible episode ({shortest} "
            "frames); episodes are never truncated"
        )
    entries: list[SubsetEntry] = []
    used = 0
    for candidate in candidates:
        if used + candidate.n_frames > budget_frames:
            continue
        entries.append(
            SubsetEntry(
                episode_uid=candidate.episode_uid,
                n_frames=candidate.n_frames,
                frame_start=0,
                frame_end=candidate.n_frames,
            )
        )
        used += candidate.n_frames
    return SubsetPlan(budget_frames=budget_frames, strategy=SEQUENTIAL, entries=tuple(entries))
