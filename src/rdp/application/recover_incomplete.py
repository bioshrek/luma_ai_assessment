"""`RecoverIncomplete` — the startup recovery pass (design §5, iron rule 3).

A crash leaves three kinds of debris, and each one is a lie the next run would otherwise
believe:

1. a `runs` row with no `finished_at` — a run that never stopped, only died;
2. an `episode_state` row still holding a lease nobody owns any more;
3. a `*.tmp` file from a write that never reached its `os.replace`, and — worse — a
   `NORMALIZED` episode whose parquet the catalog vouches for but the filesystem cannot open.

Sweeping them is cheap and unconditional, so it runs before every ingestion rather than behind
a flag. Note what recovery deliberately does *not* do: it never rolls a stage back because a
stage was in flight. The recorded stage is the last durably completed one, so there is nothing
to undo — only a lease to release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rdp.application.ports import ArtifactMaintenance, Clock, UnitOfWorkFactory
from rdp.domain.stage import IngestionStage

UNREADABLE_ARTIFACT = "normalized artifact is unreadable"
DEFAULT_OWNER = "rdp"

# Stages whose promises the filesystem has to be able to keep.
_ARTIFACT_BEARING = (IngestionStage.NORMALIZED, IngestionStage.QC_DONE)


@dataclass(frozen=True)
class RecoveryReport:
    resumed_from: str | None
    """The interrupted run this one continues. Non-null is the machine-checkable proof that a
    restart was a resume and not a fresh start."""

    interrupted_runs: tuple[str, ...] = ()
    orphan_temp_files: tuple[str, ...] = ()
    expired_leases: tuple[str, ...] = ()
    demoted: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (
            self.interrupted_runs
            or self.orphan_temp_files
            or self.expired_leases
            or self.demoted
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "resumed_from": self.resumed_from,
            "interrupted_runs": list(self.interrupted_runs),
            "orphan_temp_files": list(self.orphan_temp_files),
            "expired_leases": list(self.expired_leases),
            "demoted": list(self.demoted),
        }


@dataclass
class RecoverIncomplete:
    uow_factory: UnitOfWorkFactory
    maintenance: ArtifactMaintenance
    clock: Clock
    owner: str = DEFAULT_OWNER

    def __call__(self, run_id: str) -> RecoveryReport:
        now = self.clock.now_iso()
        # Swept before the transaction: a `*.tmp` is by definition not referenced by any row,
        # so deleting it can never race a reader.
        orphans = tuple(self.maintenance.sweep_orphan_temp_files())
        with self.uow_factory() as uow:
            interrupted = tuple(str(row["run_id"]) for row in uow.runs.unfinished())
            for previous in interrupted:
                uow.runs.mark_interrupted(previous, now)

            expired = []
            for state in uow.episode_states.list_leased():
                if not state.lease_reclaimable(now=now, owner=self.owner):
                    continue
                uow.episode_states.upsert(state.released(stage=state.stage, now=now))
                expired.append(state.episode_uid)

            demoted = []
            for stage in _ARTIFACT_BEARING:
                for episode in uow.episodes.list_by_stage(stage):
                    path = episode.frames_path
                    if path is not None and self.maintenance.frames_readable(path):
                        continue
                    uow.episodes.upsert(
                        episode.requeue(
                            stage=IngestionStage.FETCHED,
                            reason=UNREADABLE_ARTIFACT,
                            run_id=run_id,
                            now=now,
                        )
                    )
                    demoted.append(episode.uid)
            uow.commit()

        return RecoveryReport(
            # The newest unfinished run is the one this process is picking up from; older ones
            # are earlier casualties and are merely closed out.
            resumed_from=interrupted[-1] if interrupted else None,
            interrupted_runs=interrupted,
            orphan_temp_files=orphans,
            expired_leases=tuple(expired),
            demoted=tuple(demoted),
        )
