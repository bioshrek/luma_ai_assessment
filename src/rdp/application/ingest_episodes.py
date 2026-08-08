"""`IngestEpisodes` — discover, fetch, normalize, QC, commit (design §5).

The single most important property of this module: **one transaction per episode per stage**.
Nothing is batched to the end of the run, so a `kill -9` at any instant leaves a state the next
run can idempotently continue from.
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
    UnitOfWorkFactory,
)
from rdp.domain import run as counters
from rdp.domain.episode import CanonicalEpisode, Episode, EpisodeMeta
from rdp.domain.frames import FrameTable
from rdp.domain.qc.engine import evaluate_rule, roll_up
from rdp.domain.qc.rule import QCRule
from rdp.domain.run import IngestionRun
from rdp.domain.source import Source
from rdp.domain.stage import IngestionStage
from rdp.domain.stats import channel_stats

FETCH_BEFORE = "fetch.before"
FETCH_AFTER = "fetch.after"
NORMALIZE_AFTER_WRITE_BEFORE_COMMIT = "normalize.after_write_before_commit"
QC_MID_RULE = "qc.mid_rule"
COMMIT_AFTER_FILE_BEFORE_DB = "commit.after_file_before_db"


@dataclass
class IngestEpisodes:
    uow_factory: UnitOfWorkFactory
    frame_store: FrameStore
    clock: Clock
    faults: FaultInjector
    rules: Sequence[QCRule]
    ruleset_version: str
    raw_root: Path

    def __call__(
        self,
        source: Source,
        adapter: SourcePort,
        run: IngestionRun,
        max_episodes: int | None = None,
    ) -> IngestionRun:
        limit = max_episodes if max_episodes is not None else source.max_episodes
        for index, ref in enumerate(adapter.list_episodes(source)):
            if limit is not None and index >= limit:
                break
            self._process(ref, source, adapter, run)
        return run

    def _process(
        self, ref: EpisodeRef, source: Source, adapter: SourcePort, run: IngestionRun
    ) -> None:
        episode = self._ensure_discovered(ref, run)
        if episode.stage is IngestionStage.COMMITTED:
            # Scenario 2: a re-run must not re-ingest what is already done.
            run.count(counters.SKIPPED_ALREADY_PROCESSED)
            return
        try:
            episode = self._fetch_and_normalize(episode, ref, source, adapter, run)
            episode = self._run_qc(episode, run)
            episode = self._commit(episode, run)
        except Exception as exc:  # one bad episode must not abort the run
            error = f"{type(exc).__name__}: {exc}"
            self._save(episode.failed(error=error, run_id=run.run_id, now=self.clock.now_iso()))
            run.record_failure(episode.uid, error)

    def _ensure_discovered(self, ref: EpisodeRef, run: IngestionRun) -> Episode:
        now = self.clock.now_iso()
        with self.uow_factory() as uow:
            existing = uow.episodes.get(ref.uid)
            if existing is not None:
                if existing.stage is IngestionStage.FAILED:
                    # A previous run's failure is retried from the top; raw bytes are immutable
                    # and every downstream write is idempotent, so redoing them is safe.
                    retried = existing.model_copy(
                        update={
                            "stage": existing.stage.reset_to(
                                IngestionStage.DISCOVERED, reason="retry after failure"
                            ),
                            "last_update_run": run.run_id,
                            "updated_at": now,
                        }
                    )
                    uow.episodes.upsert(retried)
                    uow.commit()
                    return retried
                return existing
            episode = Episode.discovered(
                source_id=ref.source_id, upstream_id=ref.upstream_id, run_id=run.run_id, now=now
            )
            uow.episodes.upsert(episode)
            uow.commit()
        run.count(counters.DISCOVERED)
        return episode

    def _fetch_and_normalize(
        self,
        episode: Episode,
        ref: EpisodeRef,
        source: Source,
        adapter: SourcePort,
        run: IngestionRun,
    ) -> Episode:
        raw: RawEpisode | None = None
        if episode.stage is IngestionStage.DISCOVERED:
            self.faults.maybe_crash(FETCH_BEFORE)
            raw = adapter.fetch(ref, source, self._staging_dir(ref))
            self.faults.maybe_crash(FETCH_AFTER)
            episode = self._save(episode.fetched(run_id=run.run_id, now=self.clock.now_iso()))
            run.count(counters.FETCHED)

        if episode.stage.at_least(IngestionStage.NORMALIZED):
            return episode

        if raw is None:
            # Resumed mid-pipeline. `fetch` is idempotent and returns immediately when the
            # staging directory is already complete.
            raw = adapter.fetch(ref, source, self._staging_dir(ref))
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
            )
        )
        run.count(counters.NORMALIZED)
        return episode

    def _run_qc(self, episode: Episode, run: IngestionRun) -> Episode:
        if episode.stage.at_least(IngestionStage.QC_DONE):
            return episode
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
            episode = episode.qc_done(verdict=verdict, run_id=run.run_id, now=now)
            uow.episodes.upsert(episode)
            uow.commit()
        for result in results:
            run.record_rule(result)
        run.count(counters.QC_DONE)
        return episode

    def _commit(self, episode: Episode, run: IngestionRun) -> Episode:
        self.faults.maybe_crash(COMMIT_AFTER_FILE_BEFORE_DB)
        episode = self._save(episode.committed(run_id=run.run_id, now=self.clock.now_iso()))
        run.count(counters.COMMITTED)
        return episode

    def _load(self, episode: Episode) -> CanonicalEpisode:
        """Rebuild the canonical episode from the store, so QC works identically on a resume."""
        meta: EpisodeMeta | None = episode.meta
        if meta is None or episode.frames_path is None:
            raise RuntimeError(f"{episode.uid}: normalized episode has no stored frames")
        frames: FrameTable = self.frame_store.read_frames(episode.frames_path)
        return CanonicalEpisode(meta=meta, frames=frames)

    def _staging_dir(self, ref: EpisodeRef) -> Path:
        return self.raw_root / ref.source_id / ref.upstream_id

    def _save(self, episode: Episode) -> Episode:
        with self.uow_factory() as uow:
            uow.episodes.upsert(episode)
            uow.commit()
        return episode
