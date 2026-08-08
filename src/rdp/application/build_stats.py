"""`BuildStats` — the distribution of every QC metric, per source and per rule (design §3).

The point of this command is that a threshold in `config/qc.yaml` must be answerable with a
query rather than an opinion. Every rule writes numeric metrics next to its verdict, so the
evidence for "8.0 leaves 40% headroom above the corpus maximum" is already in the catalog; this
use case reads it back and summarizes it, without re-running a single rule.

Grouped by source as well as by rule, because a jerk ratio is comparable across sources and a
travel fraction in pixels is not. Mixing them into one distribution would produce a number that
is true of nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rdp.application.ports import UnitOfWorkFactory
from rdp.domain.stats import ChannelStats, summarize


@dataclass(frozen=True)
class MetricDistribution:
    source_id: str
    rule_id: str
    metric: str
    stats: ChannelStats

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "rule_id": self.rule_id,
            "metric": self.metric,
            **self.stats.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class Stats:
    verdicts: dict[str, dict[str, int]] = field(default_factory=dict)
    distributions: list[MetricDistribution] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdicts": self.verdicts,
            "distributions": [d.as_dict() for d in self.distributions],
        }


@dataclass
class BuildStats:
    uow_factory: UnitOfWorkFactory

    def __call__(self) -> Stats:
        with self.uow_factory() as uow:
            samples = uow.qc_results.metric_samples()
            verdicts = uow.qc_results.verdict_counts()

        grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for source_id, rule_id, metric, value in samples:
            grouped[(source_id, rule_id, metric)].append(value)

        return Stats(
            verdicts=verdicts,
            distributions=[
                MetricDistribution(source_id, rule_id, metric, summarize(values))
                for (source_id, rule_id, metric), values in sorted(grouped.items())
            ],
        )
