"""`IngestionRun` — the per-run tally that `rdp report` renders (design §7, §8.1).

Mutable by design: it is an accumulator for one process, not a persisted aggregate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from rdp.domain.qc.rule import RuleResult

DISCOVERED = "discovered"
SKIPPED_ALREADY_PROCESSED = "skipped_already_processed"
FETCHED = "fetched"
NORMALIZED = "normalized"
QC_DONE = "qc_done"
COMMITTED = "committed"
FAILED = "failed"

COUNTERS = (
    DISCOVERED,
    SKIPPED_ALREADY_PROCESSED,
    FETCHED,
    NORMALIZED,
    QC_DONE,
    COMMITTED,
    FAILED,
)


@dataclass
class IngestionRun:
    run_id: str
    started_at: str
    args: dict[str, Any] = field(default_factory=dict)
    finished_at: str | None = None
    status: str = "RUNNING"
    counters: Counter[str] = field(default_factory=Counter)
    rule_counts: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    skip_reasons: Counter[str] = field(default_factory=Counter)
    failures: list[tuple[str, str]] = field(default_factory=list)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def record_rule(self, result: RuleResult) -> None:
        self.rule_counts[result.rule_id][result.verdict] += 1
        if result.verdict == "SKIPPED" and result.reason:
            self.skip_reasons[f"{result.rule_id}:{result.reason}"] += 1

    def record_failure(self, episode_uid: str, error: str) -> None:
        self.counters[FAILED] += 1
        self.failures.append((episode_uid, error))

    def finish(self, now: str, status: str = "COMPLETED") -> None:
        self.finished_at = now
        self.status = status

    def stats(self) -> dict[str, Any]:
        return {
            "counters": {name: self.counters.get(name, 0) for name in COUNTERS},
            "rule_counts": {
                rule: dict(counts) for rule, counts in sorted(self.rule_counts.items())
            },
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "failures": [{"episode_uid": uid, "error": error} for uid, error in self.failures],
        }
