"""`IngestEpisodes` — discover, fetch, normalize, QC, commit (design §5).

The single most important property of this module: **one transaction per episode per stage**.
Nothing is batched to the end of the run, so a `kill -9` at any instant leaves a state the next
run can idempotently continue from.

Two rules govern every branch below:

- **File first, DB state second.** The recorded stage is always the last *durably completed*
  one, so a crash can only cost the stage that was in flight — never one the catalog claims to
  have finished.
- **Never redo a recorded stage.** An episode found at `FETCHED` is not fetched again: the row
  saying `FETCHED` is itself the receipt that the raw bytes are on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rdp.application.ports import (
    Clock,
    EpisodeRef,
    FaultInjector,
    FrameStore,
    RawEpisode,
    SourcePort,
    UnitOfWork,
    UnitOfWorkFactory,
)
from rdp.domain import run as counters
from rdp.domain.episode import SCHEMA_VERSION, CanonicalEpisode, Episode, EpisodeMeta
from rdp.domain.episode_state import EpisodeState
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule, roll_up
from rdp.domain.qc.rule import QCRule
from rdp.domain.run import IngestionRun
from rdp.domain.source import Source
from rdp.domain.stage import IngestionStage
from rdp.domain.staleness import PipelineVersions, Staleness, assess
from rdp.domain.stats import channel_stats

FETCH_BEFORE = "fetch.before"
FETCH_AFTER = "fetch.after"
NORMALIZE_BEFORE = "normalize.before"
NORMALIZE_AFTER_WRITE_BEFORE_COMMIT = "normalize.after_write_before_commit"
QC_BEFORE = "qc.before"
QC_MID_RULE = "qc.mid_rule"
QC_AFTER_EPISODE = "qc.after_episode_n"
COMMIT_AFTER_FILE_BEFORE_DB = "commit.after_file_before_db"

CHECKPOINTS: tuple[str, ...] = (
    FETCH_BEFORE,
    FETCH_AFTER,
    NORMALIZE_BEFORE,
    NORMALIZE_AFTER_WRITE_BEFORE_COMMIT,
    QC_BEFORE,
    QC_MID_RULE,
    QC_AFTER_EPISODE,
    COMMIT_AFTER_FILE_BEFORE_DB,
)
"""Every point the pipeline can be crashed at on purpose. The acceptance matrix is parametrized
over exactly this tuple, so a checkpoint cannot be added without also being tested."""

DEFAULT_LEASE_TTL_S = 900.0
DEFAULT_OWNER = "rdp"


@dataclass(frozen=True)
class _Lease:
    """Who is working on this episode, on which attempt. Renewed at every stage advance."""

    owner: str
    attempt: int


@dataclass
class IngestEpisodes:
    uow_factory: UnitOfWorkFactory
    frame_store: FrameStore
    clock: Clock
    faults: FaultInjector
    rules: Sequence[QCRule]
    ruleset_version: str
    raw_root: Path
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    owner: str = DEFAULT_OWNER

    def __call__(
        self,
        source: Source,
        adapter: SourcePort,
        run: IngestionRun,
        max_episodes: int | None = None,
    ) -> IngestionRun:
        limit = max_episodes if max_episodes is not None else source.max_episodes
        versions = PipelineVersions(
            schema_version=SCHEMA_VERSION,
            adapter_version=adapter.adapter_version,
            ruleset_version=self.ruleset_version,
        )
        for index, ref in enumerate(adapter.list_episodes(source)):
            if limit is not None and index >= limit:
                break
            self._process(ref, source, adapter, run, versions)
        return run

    def _process(
        self,
        ref: EpisodeRef,
        source: Source,
        adapter: SourcePort,
        run: IngestionRun,
        versions: PipelineVersions,
    ) -> None:
        claimed = self._claim(ref, run, versions)
        if claimed is None:
            return
        episode, lease = claimed
        try:
            episode = self._fetch_and_normalize(episode, lease, ref, source, adapter, run)
            episode = self._run_qc(episode, lease, run)
            self._commit(episode, lease, run)
        except Exception as exc:  # one bad episode must not abort the run
            error = f"{type(exc).__name__}: {exc}"
            self._save(
                episode.failed(error=error, run_id=run.run_id, now=self.clock.now_iso()),
                lease,
                release=True,
                error=error,
            )
            run.record_failure(episode.uid, error)

    # -- claim -------------------------------------------------------------------------

    def _claim(
        self, ref: EpisodeRef, run: IngestionRun, versions: PipelineVersions
    ) -> tuple[Episode, _Lease] | None:
        """Decide what this episode still needs, and take a lease on doing it.

        Returns None when there is nothing to do — acceptance scenario 2, and the only outcome
        that writes nothing at all, which is what leaves every `updated_at` untouched.
        """
        now = self.clock.now_iso()
        with self.uow_factory() as uow:
            existing = uow.episodes.get(ref.uid)
            if existing is None:
                episode = Episode.discovered(
                    source_id=ref.source_id,
                    upstream_id=ref.upstream_id,
                    run_id=run.run_id,
                    now=now,
                )
                run.count(counters.DISCOVERED)
            else:
                resolved = self._resume_or_skip(existing, run, versions, now)
                if resolved is None:
                    return None
                episode = resolved
            lease = self._take_lease(uow, episode, now)
            uow.episodes.upsert(episode)
            uow.commit()
        return episode, lease

    def _resume_or_skip(
        self, existing: Episode, run: IngestionRun, versions: PipelineVersions, now: str
    ) -> Episode | None:
        if existing.stage is IngestionStage.FAILED:
            # Raw bytes are immutable and every downstream write is idempotent, so a previous
            # run's failure is safe to retry from the top.
            return existing.requeue(
                stage=IngestionStage.DISCOVERED,
                reason="retry after failure",
                run_id=run.run_id,
                now=now,
            )
        if existing.stage is not IngestionStage.COMMITTED:
            # Left mid-pipeline by a crash: continue from the recorded stage, never restart.
            run.count(counters.RESUMED)
            return existing
        staleness = assess(existing, versions)
        if staleness is Staleness.FRESH:
            run.count(counters.SKIPPED_ALREADY_PROCESSED)
            return None
        run.count(
            counters.STALE_REQC if staleness is Staleness.REDO_QC else counters.STALE_RENORMALIZE
        )
        return existing.requeue(
            stage=staleness.rewind_to, reason=str(staleness), run_id=run.run_id, now=now
        )

    def _take_lease(self, uow: UnitOfWork, episode: Episode, now: str) -> _Lease:
        previous = uow.episode_states.get(episode.uid)
        attempt = previous.next_attempt() if previous is not None else 1
        uow.episode_states.upsert(
            EpisodeState.start(
                episode_uid=episode.uid,
                stage=episode.stage,
                attempt=attempt,
                owner=self.owner,
                expires_at=self.clock.horizon_iso(self.lease_ttl_s),
                now=now,
            )
        )
        return _Lease(owner=self.owner, attempt=attempt)

    # -- stages ------------------------------------------------------------------------

    def _fetch_and_normalize(
        self,
        episode: Episode,
        lease: _Lease,
        ref: EpisodeRef,
        source: Source,
        adapter: SourcePort,
        run: IngestionRun,
    ) -> Episode:
        if episode.stage is IngestionStage.DISCOVERED:
            self.faults.maybe_crash(FETCH_BEFORE)
            adapter.fetch(ref, source, self._staging_dir(ref))
            self.faults.maybe_crash(FETCH_AFTER)
            episode = self._save(
                episode.fetched(run_id=run.run_id, now=self.clock.now_iso()), lease
            )
            run.count(counters.FETCHED)

        if episode.stage.at_least(IngestionStage.NORMALIZED):
            return episode

        # The raw handle is reconstructed, not re-fetched: a resume that called `fetch` again
        # would repeat a download the catalog has already accounted for.
        raw = RawEpisode(ref=ref, path=self._staging_dir(ref), upstream_revision=source.revision)
        self.faults.maybe_crash(NORMALIZE_BEFORE)
        canonical = adapter.normalize(raw, source)
        # File first, DB state second: a crash between the two leaves an orphan artifact that
        # the next run overwrites with identical bytes. The reverse order would lose data.
        frames_path = self.frame_store.write(canonical)
        self.faults.maybe_crash(NORMALIZE_AFTER_WRITE_BEFORE_COMMIT)
        episode = self._save(
            episode.normalized(
                meta=canonical.meta,
                frames_path=frames_path,
                content_hash=canonical.content_hash(),
                adapter_version=adapter.adapter_version,
                channel_stats=channel_stats(
                    canonical.frames, canonical.meta.action_spec, canonical.meta.state_spec
                ),
                run_id=run.run_id,
                now=self.clock.now_iso(),
            ),
            lease,
        )
        run.count(counters.NORMALIZED)
        return episode

    def _run_qc(self, episode: Episode, lease: _Lease, run: IngestionRun) -> Episode:
        if episode.stage.at_least(IngestionStage.QC_DONE):
            return episode
        self.faults.maybe_crash(QC_BEFORE)
        canonical = self._load(episode)
        results = []
        for position, rule in enumerate(self.rules):
            if position:
                self.faults.maybe_crash(QC_MID_RULE)
            results.append(evaluate_rule(rule, canonical.frames, canonical.meta))
        verdict = roll_up(results)
        now = self.clock.now_iso()
        with self.uow_factory() as uow:
            # Results and the verdict that summarises them land in the same transaction, so the
            # two can never disagree after a crash.
            uow.qc_results.record(episode.uid, run.run_id, results)
            episode = episode.qc_done(
                verdict=verdict, ruleset_version=self.ruleset_version, run_id=run.run_id, now=now
            )
            uow.episodes.upsert(episode)
            uow.episode_states.upsert(self._state_for(episode, lease, now))
            uow.commit()
        for result in results:
            run.record_rule(result)
        run.count(counters.QC_DONE)
        self.faults.maybe_crash(QC_AFTER_EPISODE)
        return episode

    def _commit(self, episode: Episode, lease: _Lease, run: IngestionRun) -> Episode:
        self.faults.maybe_crash(COMMIT_AFTER_FILE_BEFORE_DB)
        episode = self._save(
            episode.committed(run_id=run.run_id, now=self.clock.now_iso()), lease, release=True
        )
        run.count(counters.COMMITTED)
        return episode

    # -- persistence -------------------------------------------------------------------

    def _load(self, episode: Episode) -> CanonicalEpisode:
        """Rebuild the canonical episode from the store, so QC works identically on a resume."""
        meta: EpisodeMeta | None = episode.meta
        if meta is None or episode.frames_path is None:
            raise RuntimeError(f"{episode.uid}: normalized episode has no stored frames")
        frames: FrameTable = self.frame_store.read_frames(episode.frames_path)
        streams = self.frame_store.read_streams(episode.frames_path)
        return CanonicalEpisode(meta=meta, frames=frames, streams=streams)

    def _staging_dir(self, ref: EpisodeRef) -> Path:
        return self.raw_root / ref.source_id / ref.upstream_id

    def _save(
        self,
        episode: Episode,
        lease: _Lease,
        *,
        release: bool = False,
        error: str | None = None,
    ) -> Episode:
        """One stage advance = one transaction covering both the catalog and the state row."""
        now = self.clock.now_iso()
        with self.uow_factory() as uow:
            uow.episodes.upsert(episode)
            uow.episode_states.upsert(self._state_for(episode, lease, now, release, error))
            uow.commit()
        return episode

    def _state_for(
        self,
        episode: Episode,
        lease: _Lease,
        now: str,
        release: bool = False,
        error: str | None = None,
    ) -> EpisodeState:
        if release:
            return EpisodeState(
                episode_uid=episode.uid,
                stage=episode.stage,
                attempt=lease.attempt,
                last_error=error,
                updated_at=now,
            )
        return EpisodeState.start(
            episode_uid=episode.uid,
            stage=episode.stage,
            attempt=lease.attempt,
            owner=lease.owner,
            expires_at=self.clock.horizon_iso(self.lease_ttl_s),
            now=now,
        )
