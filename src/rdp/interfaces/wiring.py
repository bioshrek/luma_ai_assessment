"""Composition root. The only place that knows which adapter implements which port.

Everything below `interfaces/` receives its collaborators; nothing constructs its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from rdp.application.build_report import BuildReport
from rdp.application.export_subset import ExportSubset
from rdp.application.ingest_episodes import IngestEpisodes
from rdp.application.ports import SourcePort
from rdp.application.recover_incomplete import RecoverIncomplete
from rdp.domain.embodiment import EmbodimentRegistry
from rdp.domain.qc.rule import QCRule
from rdp.domain.source import Source
from rdp.infrastructure.clock import SystemClock
from rdp.infrastructure.config.loader import load_embodiments, load_rules, load_sources
from rdp.infrastructure.faults import fault_injector_from_env
from rdp.infrastructure.persistence.catalog import SqliteCatalog, SqliteUnitOfWork
from rdp.infrastructure.sources.lerobot_adapter import LeRobotAdapter
from rdp.infrastructure.sources.rlds_adapter import RLDSAdapter
from rdp.infrastructure.sources.upstream_fetch import UpstreamFetcher
from rdp.infrastructure.storage.jsonl_writer import JsonlSubsetWriter
from rdp.infrastructure.storage.maintenance import StoreMaintenance
from rdp.infrastructure.storage.parquet_frame_store import ParquetFrameStore

DEFAULT_STORE = Path("store")
DEFAULT_CONFIG = Path("config")
DEFAULT_REPORTS = Path("reports")


@dataclass(frozen=True)
class Paths:
    store: Path
    config: Path
    reports: Path = DEFAULT_REPORTS

    @property
    def catalog(self) -> Path:
        return self.store / "catalog.sqlite"

    @property
    def raw(self) -> Path:
        """Immutable and authoritative. Never edited after staging."""
        return self.store / "raw"

    @property
    def normalized(self) -> Path:
        """Derived and disposable: a schema change is a re-normalization, not a migration."""
        return self.store / "normalized"

    @property
    def cache(self) -> Path:
        return self.store / "cache"

    @property
    def sources_yaml(self) -> Path:
        return self.config / "sources.yaml"

    @property
    def sources_local_yaml(self) -> Path:
        return self.config / "sources.local.yaml"

    @property
    def embodiments_yaml(self) -> Path:
        return self.config / "embodiments.yaml"

    @property
    def qc_yaml(self) -> Path:
        return self.config / "qc.yaml"


class Container:
    def __init__(
        self,
        store: Path = DEFAULT_STORE,
        config: Path = DEFAULT_CONFIG,
        reports: Path = DEFAULT_REPORTS,
    ) -> None:
        self.paths = Paths(store=store, config=config, reports=reports)
        self.clock = SystemClock()

    @cached_property
    def sources(self) -> dict[str, Source]:
        return load_sources(self.paths.sources_yaml, self.paths.sources_local_yaml)

    @cached_property
    def embodiments(self) -> EmbodimentRegistry:
        return load_embodiments(self.paths.embodiments_yaml)

    @cached_property
    def _ruleset(self) -> tuple[list[QCRule], str]:
        return load_rules(self.paths.qc_yaml)

    @property
    def rules(self) -> list[QCRule]:
        return self._ruleset[0]

    @property
    def ruleset_version(self) -> str:
        return self._ruleset[1]

    @cached_property
    def catalog(self) -> SqliteCatalog:
        return SqliteCatalog(self.paths.catalog, self.ruleset_version)

    @cached_property
    def frame_store(self) -> ParquetFrameStore:
        return ParquetFrameStore(self.paths.normalized)

    @cached_property
    def maintenance(self) -> StoreMaintenance:
        return StoreMaintenance(self.paths.store, self.frame_store)

    def unit_of_work(self) -> SqliteUnitOfWork:
        return self.catalog.unit_of_work(self.clock.now_iso())

    def adapter_for(self, source: Source) -> SourcePort:
        fetcher = UpstreamFetcher(self.paths.cache)
        if source.kind == "lerobot":
            return LeRobotAdapter(fetcher, self.embodiments)
        if source.kind == "rlds":
            return RLDSAdapter(fetcher, self.embodiments, source.shard_layout_revision)
        raise NotImplementedError(
            f"no adapter for kind {source.kind!r} yet; source D arrives in M4"
        )

    def ingest(self) -> IngestEpisodes:
        return IngestEpisodes(
            uow_factory=self.unit_of_work,
            frame_store=self.frame_store,
            clock=self.clock,
            faults=fault_injector_from_env(),
            rules=self.rules,
            ruleset_version=self.ruleset_version,
            raw_root=self.paths.raw,
        )

    def recover(self) -> RecoverIncomplete:
        return RecoverIncomplete(
            uow_factory=self.unit_of_work, maintenance=self.maintenance, clock=self.clock
        )

    def export(self) -> ExportSubset:
        return ExportSubset(
            uow_factory=self.unit_of_work, writer=JsonlSubsetWriter(), clock=self.clock
        )

    def report(self) -> BuildReport:
        return BuildReport(uow_factory=self.unit_of_work)

    def new_run_id(self) -> str:
        stamp = self.clock.now_iso().replace(":", "").replace("-", "").split(".")[0]
        return f"run_{stamp}_{uuid.uuid4().hex[:6]}"
