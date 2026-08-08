"""QC rule protocol and verdict vocabulary (design §3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from rdp.domain.action_spec import SignalLevel
from rdp.domain.capabilities import Capabilities
from rdp.domain.frames import FrameTable


class Verdict(StrEnum):
    """One rule's conclusion about one episode."""

    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class EpisodeVerdict(StrEnum):
    """The episode-level roll-up stored in `episodes.qc_verdict`."""

    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class Severity(StrEnum):
    FAIL = "FAIL"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    verdict: Verdict
    metrics: Mapping[str, float] = field(default_factory=dict)
    reason: str = ""


class QCRule(Protocol):
    """A pure function of `(FrameTable, EpisodeMeta) -> RuleResult`. No IO, no database.

    Gating is declarative and applied by the engine, so a rule cannot bypass `SKIPPED`
    (invariant 4).
    """

    @property
    def rule_id(self) -> str: ...

    @property
    def severity(self) -> Severity: ...

    @property
    def required_capabilities(self) -> frozenset[str]: ...

    @property
    def required_levels(self) -> Mapping[str, SignalLevel]: ...

    @property
    def requires_real_timestamps(self) -> bool: ...

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult: ...


class QCEpisodeView(Protocol):
    """The slice of episode metadata Quality is allowed to see (design §8.2).

    Deliberately minimal: a rule that needs to branch on `source_id` is a wrong rule.
    """

    @property
    def capabilities(self) -> Capabilities: ...

    @property
    def has_real_timestamps(self) -> bool: ...

    def level_of(self, signal: str) -> SignalLevel: ...
