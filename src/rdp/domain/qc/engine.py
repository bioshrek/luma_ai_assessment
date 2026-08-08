"""The rule executor. Gating and roll-up live here so no rule can bypass them (invariant 4)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import EpisodeVerdict, QCEpisodeView, QCRule, RuleResult, Verdict

SYNTHETIC_TIMESTAMP = "synthetic_timestamp"


def gate(rule: QCRule, meta: QCEpisodeView) -> str | None:
    """Return a skip reason, or None if the rule may run.

    A specific reason matters: "action is an episode-level label" and "there is no action" are
    different conclusions and the report counts them separately (design §3).
    """
    for capability in sorted(rule.required_capabilities):
        if not meta.capabilities.has(capability):
            return f"capability_unmet:{capability}"
    for signal, required in rule.required_levels.items():
        actual = meta.level_of(signal)
        if actual != required:
            return f"{signal}_level_is_{actual}"
    if rule.requires_real_timestamps and not meta.has_real_timestamps:
        return SYNTHETIC_TIMESTAMP
    return None


def evaluate_rule(rule: QCRule, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
    """Run one rule. A rule that raises becomes an ERROR verdict; the run continues."""
    n_frames = {"n_frames": float(frames.n_frames)}
    reason = gate(rule, meta)
    if reason is not None:
        return RuleResult(rule.rule_id, Verdict.SKIPPED, n_frames, reason)
    try:
        result = rule.evaluate(frames, meta)
    except Exception as exc:  # one bad episode never aborts a run (design §3)
        return RuleResult(rule.rule_id, Verdict.ERROR, n_frames, f"{type(exc).__name__}: {exc}")
    return RuleResult(result.rule_id, result.verdict, {**n_frames, **result.metrics}, result.reason)


def evaluate_all(
    rules: Iterable[QCRule], frames: FrameTable, meta: QCEpisodeView
) -> list[RuleResult]:
    return [evaluate_rule(rule, frames, meta) for rule in rules]


def roll_up(results: Sequence[RuleResult]) -> EpisodeVerdict:
    """FAIL dominates REVIEW dominates PASS. An all-SKIPPED episode passes.

    An ERROR rolls up to REVIEW, not FAIL: a crashing rule is evidence about our code, not about
    the data, and a human has to look at it.
    """
    verdicts = {r.verdict for r in results}
    if Verdict.FAIL in verdicts:
        return EpisodeVerdict.FAIL
    if Verdict.REVIEW in verdicts or Verdict.ERROR in verdicts:
        return EpisodeVerdict.REVIEW
    return EpisodeVerdict.PASS
