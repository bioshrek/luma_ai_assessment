"""`ExportSubset` — turn the catalog into a curated training manifest (design §6).

The manifest is self-describing: a consumer must be able to train from a line without querying
the catalog, and without having to guess what `action` means for that embodiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdp.application.ports import (
    Clock,
    ExportRepository,
    SubsetWriter,
    UnitOfWorkFactory,
)
from rdp.domain.curation.sampler import (
    BALANCED,
    SEQUENTIAL,
    Candidate,
    plan_balanced,
    plan_sequential,
)
from rdp.domain.episode import Episode, EpisodeMeta
from rdp.domain.errors import InvariantViolation
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.subset import SubsetPlan

STRATEGIES = (BALANCED, SEQUENTIAL)


@dataclass(frozen=True)
class ExportResult:
    path: Path
    plan: SubsetPlan
    n_episodes: int
    n_frames: int


@dataclass
class ExportSubset:
    uow_factory: UnitOfWorkFactory
    writer: SubsetWriter
    clock: Clock

    def __call__(
        self,
        *,
        out: Path,
        budget_frames: int,
        strategy: str = BALANCED,
        include_review: bool = False,
        embodiment: str | None = None,
        run_id: str = "",
        seed: int | None = None,
    ) -> ExportResult:
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; available: {', '.join(STRATEGIES)}")
        verdicts = [EpisodeVerdict.PASS.value]
        if include_review:
            verdicts.append(EpisodeVerdict.REVIEW.value)

        with self.uow_factory() as uow:
            episodes = uow.episodes.list_exportable(verdicts=verdicts, embodiment=embodiment)
            licenses = uow.sources.licenses()
            candidates = [self._candidate(episode) for episode in episodes]
            plan = (
                plan_balanced(candidates, budget_frames, seed=seed)
                if strategy == BALANCED
                else plan_sequential(candidates, budget_frames)
            )
            # The manifest is written in the plan's order, so the same seed yields the same
            # bytes: reproducibility is a property of the file, not just of the episode set.
            by_uid = {episode.uid: episode for episode in episodes}
            records = [
                self._record(
                    by_uid[entry.episode_uid],
                    uow.qc_results.rules_hit(entry.episode_uid),
                    licenses.get(by_uid[entry.episode_uid].source_id),
                )
                for entry in plan.entries
            ]
            self.writer.write(out, records)
            self._record_export(uow.exports, out, plan, run_id, embodiment, include_review)
            uow.commit()

        return ExportResult(
            path=out, plan=plan, n_episodes=len(plan.entries), n_frames=plan.total_frames
        )

    def _candidate(self, episode: Episode) -> Candidate:
        meta = self._meta(episode)
        return Candidate(
            episode_uid=episode.uid,
            n_frames=meta.n_frames,
            source_id=episode.source_id,
            embodiment=meta.embodiment,
            qc_verdict=episode.qc_verdict,
            task=meta.task,
        )

    def _record(
        self, episode: Episode, rules_hit: list[str], license_id: str | None
    ) -> dict[str, Any]:
        meta = self._meta(episode)
        action = meta.action_spec
        return {
            "episode_uid": episode.uid,
            "source_id": episode.source_id,
            # Source D is CC BY-NC 4.0. A manifest that omits that turns a licence term into an
            # unwritten assumption of whoever trains on it.
            "license": license_id,
            "embodiment": meta.embodiment,
            "task": meta.task,
            # A consumer must not have to infer the action semantics from the source name.
            "action_level": action.level.value,
            "action_space": action.space.value,
            "action_dim": action.dim,
            "physical_dim": action.physical_dim,
            "action_is_delta": action.is_delta,
            "frame_start": 0,
            "frame_end": meta.n_frames,
            "n_frames": meta.n_frames,
            "fps_nominal": meta.fps_nominal,
            "fps_effective": meta.fps_effective,
            "duration_s": meta.duration_s,
            "capabilities": meta.capabilities.model_dump(mode="json"),
            "boundary": meta.boundary.model_dump(mode="json"),
            "frames_path": episode.frames_path,
            "raw_frame_columns": list(meta.raw_frame_columns),
            "key_stats": {
                name: stats.model_dump(mode="json")
                for name, stats in sorted(episode.channel_stats.items())
            },
            "qc_verdict": episode.qc_verdict.value,
            "qc_rules_hit": rules_hit,
            "schema_version": meta.schema_version,
        }

    def _record_export(
        self,
        exports: ExportRepository,
        out: Path,
        plan: SubsetPlan,
        run_id: str,
        embodiment: str | None,
        include_review: bool,
    ) -> None:
        exports.record(
            export_id=f"{out.name}@{self.clock.now_iso()}",
            run_id=run_id,
            strategy=plan.strategy,
            budget_frames=plan.budget_frames,
            n_episodes=len(plan.entries),
            n_frames=plan.total_frames,
            path=str(out),
            created_at=self.clock.now_iso(),
            # Everything needed to reproduce this export: the seed, the filters, and the quota
            # each embodiment was offered against what it could actually take.
            seed=plan.seed,
            embodiment=embodiment,
            include_review=include_review,
            stats=plan.stats(),
        )

    @staticmethod
    def _meta(episode: Episode) -> EpisodeMeta:
        if episode.meta is None:
            raise InvariantViolation(f"{episode.uid}: an exportable episode must carry metadata")
        return episode.meta
