"""The `IngestionStage` state machine (design §4, §8.4 invariant 1)."""

from __future__ import annotations

from enum import StrEnum

from rdp.domain.errors import IllegalStageTransition


class IngestionStage(StrEnum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    NORMALIZED = "NORMALIZED"
    QC_DONE = "QC_DONE"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"

    @property
    def order(self) -> int:
        """Position in the pipeline. `FAILED` is off-pipeline and has no position."""
        if self is IngestionStage.FAILED:
            raise IllegalStageTransition("FAILED has no pipeline position")
        return PIPELINE.index(self)

    def at_least(self, other: IngestionStage) -> bool:
        if self is IngestionStage.FAILED:
            return False
        return self.order >= other.order

    def advance(self) -> IngestionStage:
        """Move exactly one step forward. Skipping and reversing are rejected."""
        if self is IngestionStage.FAILED:
            raise IllegalStageTransition(
                "FAILED must be reset_to() an earlier stage before advancing"
            )
        if self is IngestionStage.COMMITTED:
            raise IllegalStageTransition("COMMITTED is terminal")
        return PIPELINE[self.order + 1]

    def advance_to(self, target: IngestionStage) -> IngestionStage:
        """Advance one step and assert the caller expected that same step."""
        nxt = self.advance()
        if nxt is not target:
            raise IllegalStageTransition(f"{self} advances to {nxt}, not {target}")
        return nxt

    def reset_to(self, target: IngestionStage, reason: str) -> IngestionStage:
        """The only legal way to move backwards or off-pipeline. A reason is mandatory."""
        if not reason.strip():
            raise IllegalStageTransition("reset_to() requires a non-empty reason")
        if target is IngestionStage.COMMITTED:
            raise IllegalStageTransition("cannot reset_to COMMITTED; use advance()")
        return target


PIPELINE: tuple[IngestionStage, ...] = (
    IngestionStage.DISCOVERED,
    IngestionStage.FETCHED,
    IngestionStage.NORMALIZED,
    IngestionStage.QC_DONE,
    IngestionStage.COMMITTED,
)
