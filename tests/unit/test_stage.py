"""Invariant 1: the stage machine only moves one step forward, or backwards via `reset_to`."""

from __future__ import annotations

import pytest

from rdp.domain.errors import IllegalStageTransition
from rdp.domain.stage import PIPELINE, IngestionStage


def test_advance_walks_the_pipeline_in_order() -> None:
    stage = IngestionStage.DISCOVERED
    walked = [stage]
    while stage is not IngestionStage.COMMITTED:
        stage = stage.advance()
        walked.append(stage)
    assert tuple(walked) == PIPELINE


def test_advance_to_rejects_skipping_a_stage() -> None:
    with pytest.raises(IllegalStageTransition):
        IngestionStage.DISCOVERED.advance_to(IngestionStage.NORMALIZED)


def test_committed_is_terminal() -> None:
    with pytest.raises(IllegalStageTransition):
        IngestionStage.COMMITTED.advance()


def test_failed_has_no_pipeline_position_and_never_satisfies_at_least() -> None:
    with pytest.raises(IllegalStageTransition):
        _ = IngestionStage.FAILED.order
    assert IngestionStage.FAILED.at_least(IngestionStage.DISCOVERED) is False


def test_failed_must_be_reset_before_it_can_advance() -> None:
    with pytest.raises(IllegalStageTransition):
        IngestionStage.FAILED.advance()
    assert IngestionStage.FAILED.reset_to(
        IngestionStage.DISCOVERED, "retry after failure"
    ) is IngestionStage.DISCOVERED


def test_reset_to_demands_a_reason_and_refuses_to_fabricate_committed() -> None:
    with pytest.raises(IllegalStageTransition):
        IngestionStage.FETCHED.reset_to(IngestionStage.DISCOVERED, "  ")
    with pytest.raises(IllegalStageTransition):
        IngestionStage.QC_DONE.reset_to(IngestionStage.COMMITTED, "shortcut")
