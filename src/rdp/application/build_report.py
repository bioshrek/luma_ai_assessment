"""`BuildReport` — the run summary, recomputed from the catalog (design §7).

Nothing is cached: the report is a query, so it can be replayed for an old run and it can never
drift from the data it describes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdp.application.ports import UnitOfWorkFactory


@dataclass(frozen=True)
class Report:
    run: dict[str, Any] | None
    stage_counts: dict[str, int] = field(default_factory=dict)
    rule_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "stage_counts": self.stage_counts,
            "rule_counts": self.rule_counts,
        }


@dataclass
class BuildReport:
    uow_factory: UnitOfWorkFactory

    def __call__(self, run_id: str | None = None) -> Report:
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id) if run_id else uow.runs.latest()
            return Report(
                run=dict(run) if run is not None else None,
                stage_counts=uow.episodes.counts_by_stage(),
                rule_counts=uow.qc_results.verdict_counts(run_id),
            )
