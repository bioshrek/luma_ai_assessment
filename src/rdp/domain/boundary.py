"""`EpisodeBoundary` — where the episode ended, and **who decided** (design §2.2g)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from rdp.domain.errors import InvariantViolation


class TerminationSource(StrEnum):
    ENV_RULE = "env_rule"
    POLICY_FLAG = "policy_flag"
    OPERATOR = "operator"
    ANNOTATOR = "annotator"


class EndReason(StrEnum):
    SUCCESS = "success"
    TRUNCATED = "truncated"
    OPERATOR_STOP = "operator_stop"
    ANNOTATION_BOUND = "annotation_bound"
    UNKNOWN = "unknown"


class SuccessAdjudicator(StrEnum):
    SIMULATOR = "simulator"
    POLICY = "policy"
    OPERATOR = "operator"
    NONE = "none"
    """No adjudicator exists in this system at all — different from 'outcome unknown'."""


class EpisodeBoundary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    termination_source: TerminationSource
    end_reason: EndReason
    is_truncated: bool | None
    """None = the upstream export merged terminated/truncated (A and B; see ADR 002)."""
    success: bool | None
    """None means unknown, never False."""
    success_adjudicator: SuccessAdjudicator

    @model_validator(mode="after")
    def _check(self) -> Self:
        # Invariant 7.
        if self.is_truncated is True and self.end_reason is EndReason.SUCCESS:
            raise InvariantViolation(
                "is_truncated=True contradicts end_reason='success' (invariant 7)"
            )
        # Invariant 14.
        if self.success_adjudicator is SuccessAdjudicator.NONE and self.success is not None:
            raise InvariantViolation(
                "success_adjudicator='none' implies success is None (invariant 14)"
            )
        return self
