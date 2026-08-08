"""The ports. Everything the application needs from the outside world, as Protocols.

These four families are the whole scale-out story (design §10): swapping SQLite for Postgres,
the local filesystem for object storage, or a source for another is an adapter change behind
one of these, with no edit to `domain/` or `application/`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rdp.domain.episode import CanonicalEpisode, Episode, EpisodeMeta, make_uid
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import RuleResult
from rdp.domain.run import IngestionRun
from rdp.domain.source import Source
from rdp.domain.stage import IngestionStage


@dataclass(frozen=True)
class EpisodeRef:
    """A discovered episode: enough to identify and later fetch it, nothing more.

    `extra` carries source-specific locators (shard index, row range). The application never
    interprets it; only the adapter that produced it does.
    """

    source_id: str
    upstream_id: str
    n_frames_hint: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return make_uid(self.source_id, self.upstream_id)


@dataclass(frozen=True)
class RawEpisode:
    """Bytes on local disk, exactly as upstream produced them. Immutable and authoritative."""

    ref: EpisodeRef
    path: Path
    upstream_revision: str


class SourcePort(Protocol):
    """The one plug-in point. A fifth source is one implementation plus one config entry."""

    @property
    def kind(self) -> str:
        """Matches `kind` in sources.yaml. One adapter serves every source of that kind."""
        ...

    @property
    def adapter_version(self) -> str: ...

    def list_episodes(self, source: Source) -> Iterator[EpisodeRef]:
        """Cheap discovery. Must not download frame payloads."""
        ...

    def fetch(self, ref: EpisodeRef, source: Source, dest: Path) -> RawEpisode:
        """Materialise raw bytes under `dest`. Idempotent: a second call is a no-op."""
        ...

    def normalize(self, raw: RawEpisode, source: Source) -> CanonicalEpisode:
        """Pure-ish translation of raw bytes into the unified schema. No network."""
        ...


class EpisodeRepository(Protocol):
    def get(self, uid: str) -> Episode | None: ...

    def upsert(self, episode: Episode) -> None:
        """Idempotent on `(source_id, upstream_id)`."""
        ...

    def list_by_stage(self, stage: IngestionStage) -> list[Episode]: ...

    def list_exportable(
        self, *, verdicts: Sequence[str], embodiment: str | None = None
    ) -> list[Episode]: ...

    def counts_by_stage(self) -> dict[str, int]: ...


class QCResultRepository(Protocol):
    def record(self, episode_uid: str, run_id: str, results: Sequence[RuleResult]) -> None:
        """Idempotent on `(episode_uid, rule_id, run_id)`."""
        ...

    def rules_hit(self, episode_uid: str) -> list[str]:
        """Rule ids whose latest verdict for this episode was FAIL or REVIEW."""
        ...

    def verdict_counts(self, run_id: str | None = None) -> dict[str, dict[str, int]]: ...


class RunRepository(Protocol):
    def start(self, run: IngestionRun) -> None: ...

    def finish(self, run: IngestionRun) -> None: ...

    def get(self, run_id: str) -> Mapping[str, Any] | None: ...

    def latest(self) -> Mapping[str, Any] | None: ...


class ExportRepository(Protocol):
    def record(
        self,
        *,
        export_id: str,
        run_id: str,
        strategy: str,
        budget_frames: int,
        n_episodes: int,
        n_frames: int,
        path: str,
        created_at: str,
    ) -> None: ...


class SourceRepository(Protocol):
    def upsert(self, source: Source) -> None: ...


class UnitOfWork(AbstractContextManager["UnitOfWork"], Protocol):
    """One transaction. Committing an episode's stage advance is one of these — never a batch.

    That granularity is what makes `kill -9` safe: whatever committed is durable, and whatever
    did not is idempotently redone.
    """

    @property
    def episodes(self) -> EpisodeRepository: ...

    @property
    def qc_results(self) -> QCResultRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def exports(self) -> ExportRepository: ...

    @property
    def sources(self) -> SourceRepository: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class FrameStore(Protocol):
    """Where `frames.parquet` and `episode.json` live. Local FS now, object storage later."""

    def write(self, episode: CanonicalEpisode) -> str:
        """Write atomically and return the stored path. Rewriting equal content is a no-op."""
        ...

    def read_frames(self, path: str) -> FrameTable: ...

    def read_meta(self, path: str) -> EpisodeMeta: ...


class Clock(Protocol):
    def now_iso(self) -> str: ...


class SubsetWriter(Protocol):
    """Writes the export manifest. One JSON object per episode, written atomically."""

    def write(self, path: Path, records: Sequence[Mapping[str, Any]]) -> None: ...


class FaultInjector(Protocol):
    """A production port that exists for testability (design §8.5).

    The production implementation is a no-op. Tests use it to crash at named checkpoints, which
    is the only way to cover all crash points; a manual `kill -9` can only ever sample one.
    """

    def maybe_crash(self, checkpoint: str) -> None: ...


class RunReporter(Protocol):
    def publish(self, run: IngestionRun) -> None: ...
