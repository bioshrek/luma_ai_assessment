"""The acceptance harness: a whole pipeline over fakes, restartable like a process.

Each `Rig.run()` opens its own catalog connection and closes it again, so a "restart" here goes
through exactly the durability boundary a real restart does — nothing is carried over in memory
except the counter file, which is on disk.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.fakes.faults import Crash, RaisingFaultInjector
from tests.fakes.rules import default_rules
from tests.fakes.source import FakeSource

from rdp.application.ingest_episodes import IngestEpisodes
from rdp.application.ports import UnitOfWork
from rdp.application.recover_incomplete import RecoverIncomplete, RecoveryReport
from rdp.domain.qc.rule import QCRule
from rdp.domain.run import IngestionRun
from rdp.domain.source import Source
from rdp.infrastructure.clock import SystemClock
from rdp.infrastructure.faults import NoopFaultInjector
from rdp.infrastructure.persistence.catalog import SqliteCatalog
from rdp.infrastructure.storage.maintenance import StoreMaintenance
from rdp.infrastructure.storage.parquet_frame_store import ParquetFrameStore

# Identity of the run that wrote a row, and when. Excluded from baseline comparison because a
# resumed pipeline legitimately attributes the last stretch of work to the second run; what must
# match is everything that describes the *data*.
VOLATILE_EPISODE_COLUMNS = frozenset({"first_seen_run", "last_update_run", "updated_at"})
VOLATILE_QC_COLUMNS = frozenset({"id", "run_id", "created_at"})


@dataclass
class Rig:
    root: Path
    fake: FakeSource
    ruleset_version: str = "rules@1"
    rules: Sequence[QCRule] = field(default_factory=default_rules)

    @classmethod
    def build(cls, root: Path, n_episodes: int = 3, **kwargs: Any) -> Rig:
        root.mkdir(parents=True, exist_ok=True)
        fake = FakeSource(root / "calls.json", n_episodes=n_episodes)
        return cls(root=root, fake=fake, **kwargs)

    @property
    def source(self) -> Source:
        return Source(source_id="fake", kind="fake", uri="memory://", embodiment="pusht_planar")

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.sqlite"

    def run(
        self, run_id: str, fault: str | None = None, occurrence: int = 1
    ) -> tuple[IngestionRun, RecoveryReport]:
        catalog = SqliteCatalog(self.catalog_path, self.ruleset_version)
        clock = SystemClock()

        def uow_factory() -> UnitOfWork:
            return catalog.unit_of_work(clock.now_iso())

        try:
            frame_store = ParquetFrameStore(self.root / "normalized")
            recovery = RecoverIncomplete(
                uow_factory=uow_factory,
                maintenance=StoreMaintenance(self.root, frame_store),
                clock=clock,
            )(run_id)
            run = IngestionRun(run_id=run_id, started_at=clock.now_iso())
            run.resumed_from = recovery.resumed_from
            run.recovery = recovery.as_dict()
            with uow_factory() as uow:
                uow.runs.start(run)
                uow.commit()

            ingest = IngestEpisodes(
                uow_factory=uow_factory,
                frame_store=frame_store,
                clock=clock,
                faults=(
                    RaisingFaultInjector(fault, occurrence)
                    if fault is not None
                    else NoopFaultInjector()
                ),
                rules=self.rules,
                ruleset_version=self.ruleset_version,
                raw_root=self.root / "raw",
            )
            try:
                ingest(self.source, self.fake, run)
            except Crash:
                # No `runs.finish`, no cleanup: the row keeps `finished_at IS NULL`, which is
                # precisely the trace a `kill -9` leaves behind.
                return run, recovery
            run.finish(clock.now_iso())
            with uow_factory() as uow:
                uow.runs.finish(run)
                uow.commit()
            return run, recovery
        finally:
            catalog.close()

    # -- observation --------------------------------------------------------------------

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Every catalog row that describes data, with run identity and clock stripped out."""
        conn = sqlite3.connect(self.catalog_path)
        conn.row_factory = sqlite3.Row
        try:
            episodes = [
                {k: v for k, v in dict(row).items() if k not in VOLATILE_EPISODE_COLUMNS}
                for row in conn.execute("SELECT * FROM episodes ORDER BY episode_uid")
            ]
            qc = [
                {k: v for k, v in dict(row).items() if k not in VOLATILE_QC_COLUMNS}
                for row in conn.execute(
                    "SELECT * FROM qc_results ORDER BY episode_uid, rule_id"
                )
            ]
        finally:
            conn.close()
        return {"episodes": episodes, "qc_results": qc}

    def artifact_hashes(self) -> dict[str, str]:
        root = self.root / "normalized"
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def rows(self, table: str, order: str) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.catalog_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        finally:
            conn.close()

    def temp_files(self) -> list[Path]:
        return [path for path in self.root.rglob("*.tmp") if path.is_file()]
