"""`BuildReport` — the run summary, recomputed from the catalog (design §7).

Nothing is cached and nothing is read from the running process: the report is a query, so it can
be replayed for a run that finished days ago and it can never drift from the data it describes.
The one exception is disk usage, which is measured through `StoreInspector` because no row can
know it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdp.application.ports import StoreInspector, UnitOfWorkFactory
from rdp.domain.run import RuleRate, rule_rates


@dataclass(frozen=True)
class Cumulative:
    """What the catalog holds in total — the same numbers whichever run last touched it."""

    episodes: int = 0
    frames: int = 0
    duration_s: float = 0.0
    by_source_embodiment: list[tuple[str, str, int, int]] = field(default_factory=list)
    verdicts: dict[str, dict[str, int]] = field(default_factory=dict)
    skip_reasons: dict[str, dict[str, int]] = field(default_factory=dict)
    disk_usage: dict[str, int] = field(default_factory=dict)

    @property
    def rates(self) -> list[RuleRate]:
        return rule_rates(self.verdicts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "episodes": self.episodes,
            "frames": self.frames,
            "duration_s": round(self.duration_s, 3),
            "by_source_embodiment": [
                {
                    "source_id": source,
                    "embodiment": embodiment,
                    "episodes": episodes,
                    "frames": frames,
                }
                for source, embodiment, episodes, frames in self.by_source_embodiment
            ],
            "verdicts": self.verdicts,
            "rates": [
                {
                    "rule_id": rate.rule_id,
                    "evaluated": rate.evaluated,
                    "hits": rate.hits,
                    "skipped": rate.skipped,
                    "errors": rate.errors,
                    "hit_rate": round(rate.hit_rate, 4),
                    "skip_rate": round(rate.skip_rate, 4),
                }
                for rate in self.rates
            ],
            "skip_reasons": self.skip_reasons,
            "disk_usage": self.disk_usage,
        }


@dataclass(frozen=True)
class Report:
    run: dict[str, Any] | None
    stage_counts: dict[str, int] = field(default_factory=dict)
    run_verdicts: dict[str, dict[str, int]] = field(default_factory=dict)
    run_skip_reasons: dict[str, dict[str, int]] = field(default_factory=dict)
    cumulative: Cumulative | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "stage_counts": self.stage_counts,
            "run_verdicts": self.run_verdicts,
            "run_skip_reasons": self.run_skip_reasons,
            "cumulative": self.cumulative.as_dict() if self.cumulative is not None else None,
        }


@dataclass
class BuildReport:
    uow_factory: UnitOfWorkFactory
    store: StoreInspector

    def __call__(self, run_id: str | None = None, *, cumulative_only: bool = False) -> Report:
        with self.uow_factory() as uow:
            run = None
            verdicts: dict[str, dict[str, int]] = {}
            skip_reasons: dict[str, dict[str, int]] = {}
            if not cumulative_only:
                found = uow.runs.get(run_id) if run_id else uow.runs.latest()
                if found is not None:
                    run = dict(found)
                    # Scoped to the run: what *this* run concluded, not what the catalog thinks
                    # now, which a later re-QC would have changed underneath it.
                    verdicts = uow.qc_results.verdict_counts(str(run["run_id"]))
                    skip_reasons = uow.qc_results.skip_reason_counts(str(run["run_id"]))
            totals = uow.episodes.corpus_totals()
            return Report(
                run=run,
                stage_counts=uow.episodes.counts_by_stage(),
                run_verdicts=verdicts,
                run_skip_reasons=skip_reasons,
                cumulative=Cumulative(
                    episodes=int(totals["episodes"]),
                    frames=int(totals["frames"]),
                    duration_s=totals["duration_s"],
                    by_source_embodiment=uow.episodes.counts_by_source_embodiment(),
                    verdicts=uow.qc_results.verdict_counts(),
                    skip_reasons=uow.qc_results.skip_reason_counts(),
                    disk_usage=self.store.usage_bytes(),
                ),
            )
