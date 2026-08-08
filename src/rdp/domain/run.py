"""`IngestionRun` — the per-run tally that `rdp report` renders (design §7, §8.1).

Mutable by design: it is an accumulator for one process, not a persisted aggregate.

This module is also where the *names and definitions* of the pipeline's statistics live, so the
CLI report, the markdown file and any future metrics exporter cannot disagree about what "hit
rate" or "skipped" means. Presenters format these numbers; they never compute a new one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from rdp.domain.qc.rule import RuleResult, Verdict

DISCOVERED = "discovered"
SKIPPED_ALREADY_PROCESSED = "skipped_already_processed"
STALE_RENORMALIZE = "stale_renormalize"
STALE_REQC = "stale_reqc"
RESUMED = "resumed_in_progress"
FETCHED = "fetched"
NORMALIZED = "normalized"
QC_DONE = "qc_done"
COMMITTED = "committed"
FAILED = "failed"

COUNTERS = (
    DISCOVERED,
    SKIPPED_ALREADY_PROCESSED,
    STALE_RENORMALIZE,
    STALE_REQC,
    RESUMED,
    FETCHED,
    NORMALIZED,
    QC_DONE,
    COMMITTED,
    FAILED,
)

FETCH = "fetch"
NORMALIZE = "normalize"
QC = "qc"
COMMIT = "commit"

STAGES = (FETCH, NORMALIZE, QC, COMMIT)
"""The stages wall time is attributed to. Timing is *measured*, not derived from the catalog,
which is why it is the one part of the report SQL cannot reproduce."""

TOP_FAILURE_REASONS = 5


def failure_reason(error: str) -> str:
    """The bucket a failure counts towards: its exception type, not its whole message.

    Messages carry episode ids and paths, so counting them verbatim gives one bucket per
    episode and a "top reasons" table that names nothing.
    """
    head, _, _ = error.partition(":")
    return head.strip() or "unknown"


@dataclass(frozen=True)
class RuleRate:
    """What one QC rule did to the corpus. `skipped` is a result, not a missing one."""

    rule_id: str
    evaluated: int
    """Episodes the rule actually ran on: everything except SKIPPED."""
    hits: int
    """Episodes it objected to — FAIL or REVIEW. A rule with no hits is evidence, not a bug."""
    skipped: int
    errors: int

    @property
    def total(self) -> int:
        return self.evaluated + self.skipped

    @property
    def hit_rate(self) -> float:
        """Over the episodes it ran on. Dividing by the corpus would flatter a skipped rule."""
        return self.hits / self.evaluated if self.evaluated else 0.0

    @property
    def skip_rate(self) -> float:
        return self.skipped / self.total if self.total else 0.0


def rule_rates(counts: dict[str, dict[str, int]]) -> list[RuleRate]:
    """Turn `{rule_id: {verdict: n}}` into rates. One definition, used by every presenter."""
    rates = []
    for rule_id, verdicts in sorted(counts.items()):
        skipped = verdicts.get(Verdict.SKIPPED.value, 0)
        errors = verdicts.get(Verdict.ERROR.value, 0)
        hits = verdicts.get(Verdict.FAIL.value, 0) + verdicts.get(Verdict.REVIEW.value, 0)
        evaluated = sum(verdicts.values()) - skipped
        rates.append(RuleRate(rule_id, evaluated, hits, skipped, errors))
    return rates


@dataclass
class IngestionRun:
    run_id: str
    started_at: str
    args: dict[str, Any] = field(default_factory=dict)
    finished_at: str | None = None
    status: str = "RUNNING"
    resumed_from: str | None = None
    """The interrupted run this one picked up from; None when the previous run finished."""
    recovery: dict[str, Any] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)
    rule_counts: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    skip_reasons: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    """`{rule_id: {reason: n}}`. Never flattened: "there is no action" and "the action is an
    episode label" are different conclusions and must stay separately countable (design §3)."""
    failures: list[tuple[str, str]] = field(default_factory=list)
    failure_reasons: Counter[str] = field(default_factory=Counter)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    stage_calls: Counter[str] = field(default_factory=Counter)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def record_rule(self, result: RuleResult) -> None:
        self.rule_counts[result.rule_id][result.verdict] += 1
        if result.verdict == Verdict.SKIPPED and result.reason:
            self.skip_reasons[result.rule_id][result.reason] += 1

    def record_failure(self, episode_uid: str, error: str) -> None:
        self.counters[FAILED] += 1
        self.failures.append((episode_uid, error))
        self.failure_reasons[failure_reason(error)] += 1

    def record_stage(self, stage: str, seconds: float) -> None:
        self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + seconds
        self.stage_calls[stage] += 1

    def top_failure_reasons(self, n: int = TOP_FAILURE_REASONS) -> list[tuple[str, int]]:
        ranked = sorted(self.failure_reasons.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:n]

    def finish(self, now: str, status: str = "COMPLETED") -> None:
        self.finished_at = now
        self.status = status

    def stats(self) -> dict[str, Any]:
        return {
            "counters": {name: self.counters.get(name, 0) for name in COUNTERS},
            "recovery": dict(self.recovery),
            "rule_counts": {
                rule: dict(counts) for rule, counts in sorted(self.rule_counts.items())
            },
            "skip_reasons": {
                rule: dict(sorted(reasons.items()))
                for rule, reasons in sorted(self.skip_reasons.items())
            },
            "stage_seconds": {
                stage: round(self.stage_seconds.get(stage, 0.0), 3) for stage in STAGES
            },
            "stage_calls": {stage: self.stage_calls.get(stage, 0) for stage in STAGES},
            "failure_reasons": dict(self.top_failure_reasons(len(self.failure_reasons))),
            "failures": [{"episode_uid": uid, "error": error} for uid, error in self.failures],
        }

    def as_payload(self) -> dict[str, Any]:
        """The same shape `RunRepository.get()` returns, so one renderer serves both."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "resumed_from": self.resumed_from,
            "args": dict(self.args),
            "stats": self.stats(),
        }
