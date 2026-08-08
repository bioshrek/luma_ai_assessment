"""QC rules with no opinion about data, so a crash test measures the pipeline and not a rule.

Two of them, because `qc.mid_rule` is only a distinct crash point when a rule has already run
and the QC transaction has not yet been written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rdp.domain.action_spec import SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict


@dataclass(frozen=True)
class AlwaysPasses:
    rule_id: str
    severity: Severity = Severity.REVIEW
    required_capabilities: frozenset[str] = frozenset()
    required_levels: Mapping[str, SignalLevel] = field(default_factory=dict)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        return RuleResult(self.rule_id, Verdict.PASS, {"width": float(len(frames.column_names))})


def default_rules() -> list[AlwaysPasses]:
    return [AlwaysPasses("FAKE_A"), AlwaysPasses("FAKE_B")]
