"""`SubsetPlan` — the export decision, before anything is written (design §6)."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rdp.domain.errors import InvariantViolation


class SubsetEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_uid: str = Field(min_length=1)
    n_frames: int = Field(gt=0)
    frame_start: int = 0
    frame_end: int

    @model_validator(mode="after")
    def _check(self) -> Self:
        # Invariant 5: an entry is always a whole episode. Truncating manufactures a boundary
        # that does not exist upstream, which is exactly what the boundary model exists to avoid.
        if self.frame_start != 0 or self.frame_end != self.n_frames:
            raise InvariantViolation(
                f"{self.episode_uid}: subset entries are whole episodes, got "
                f"[{self.frame_start}, {self.frame_end}) of {self.n_frames} (invariant 5)"
            )
        return self


class GroupAllocation(BaseModel):
    """What one stratum was offered and what it could actually take (design §6.3).

    Kept on the plan because "why did aloha get 31% of the budget" is a question the export has
    to be able to answer months later, from the `exports` row alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    embodiment: str = Field(min_length=1)
    eligible_episodes: int = Field(ge=0)
    eligible_frames: int = Field(ge=0)
    weight: float = Field(ge=0.0, le=1.0)
    quota_frames: int = Field(ge=0)
    selected_episodes: int = Field(ge=0)
    selected_frames: int = Field(ge=0)


class SubsetPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_frames: int = Field(gt=0)
    strategy: str
    seed: int | None = None
    entries: tuple[SubsetEntry, ...] = ()
    groups: tuple[GroupAllocation, ...] = ()

    @property
    def total_frames(self) -> int:
        return sum(entry.n_frames for entry in self.entries)

    def stats(self) -> dict[str, Any]:
        """The one definition of an export's numbers; the `exports` row stores exactly this."""
        return {
            "strategy": self.strategy,
            "seed": self.seed,
            "budget_frames": self.budget_frames,
            "used_frames": self.total_frames,
            "shortfall_frames": self.budget_frames - self.total_frames,
            "n_episodes": len(self.entries),
            "groups": [group.model_dump(mode="json") for group in self.groups],
        }

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.total_frames > self.budget_frames:
            raise InvariantViolation(
                f"plan holds {self.total_frames} frames, over the {self.budget_frames} budget "
                "(invariant 5)"
            )
        uids = [entry.episode_uid for entry in self.entries]
        if len(set(uids)) != len(uids):
            raise InvariantViolation("an episode may appear in a subset at most once")
        return self
