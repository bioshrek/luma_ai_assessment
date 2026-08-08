"""QC bounded context: pure rule functions plus the executor that gates them."""

from __future__ import annotations

from rdp.domain.qc.engine import evaluate_all, evaluate_rule, gate, roll_up
from rdp.domain.qc.rule import (
    EpisodeVerdict,
    QCEpisodeView,
    QCRule,
    RuleResult,
    Severity,
    Verdict,
)

__all__ = [
    "EpisodeVerdict",
    "QCEpisodeView",
    "QCRule",
    "RuleResult",
    "Severity",
    "Verdict",
    "evaluate_all",
    "evaluate_rule",
    "gate",
    "roll_up",
]
