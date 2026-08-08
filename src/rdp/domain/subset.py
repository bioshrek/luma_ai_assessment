"""`SubsetPlan` — the export decision, before anything is written (design §6)."""

from __future__ import annotations

from typing import Self

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


class SubsetPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_frames: int = Field(gt=0)
    strategy: str
    seed: int | None = None
    entries: tuple[SubsetEntry, ...] = ()

    @property
    def total_frames(self) -> int:
        return sum(entry.n_frames for entry in self.entries)

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
