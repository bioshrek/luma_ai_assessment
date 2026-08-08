"""Acceptance scenario 2: run again, find nothing new — and the three ways that can be wrong.

A pipeline that re-ingests unchanged data is not merely slow; on 500 datasets it is a
correctness problem, because every re-ingestion is another chance to write a different answer.
So "nothing changed" must be provable at the level of individual rows: not just "no new
episodes", but *no writes at all*.

The staleness cases are the same predicate read in the other direction. Their point is that
rewinding is targeted: a threshold edit re-runs QC and nothing else, and an adapter change
re-normalizes without re-downloading (design §5, §8.7).
"""

from __future__ import annotations

from pathlib import Path

from tests.acceptance.rig import Rig
from tests.fakes.source import FETCH, NORMALIZE

from rdp.domain import run as counters
from rdp.domain.stage import IngestionStage

N_EPISODES = 3


def _updated_at(rig: Rig) -> dict[str, str]:
    return {row["episode_uid"]: row["updated_at"] for row in rig.rows("episodes", "episode_uid")}


def test_a_second_run_on_unchanged_upstream_writes_nothing(tmp_path: Path) -> None:
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1")
    before, timestamps, hashes = rig.snapshot(), _updated_at(rig), rig.artifact_hashes()

    second, recovery = rig.run("run_2")

    assert second.counters[counters.SKIPPED_ALREADY_PROCESSED] == N_EPISODES
    for counter in (
        counters.DISCOVERED,
        counters.FETCHED,
        counters.NORMALIZED,
        counters.QC_DONE,
        counters.COMMITTED,
        counters.STALE_RENORMALIZE,
        counters.STALE_REQC,
    ):
        assert second.counters[counter] == 0, counter
    assert recovery.resumed_from is None, "a clean previous run is not something to resume from"
    assert rig.snapshot() == before
    assert _updated_at(rig) == timestamps, "an unchanged episode must not even be touched"
    assert rig.artifact_hashes() == hashes
    assert rig.fake.counts()[FETCH] == N_EPISODES
    assert rig.fake.counts()[NORMALIZE] == N_EPISODES


def test_upstream_gaining_one_episode_processes_exactly_that_one(tmp_path: Path) -> None:
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1")
    timestamps = _updated_at(rig)

    rig.fake.n_episodes = N_EPISODES + 1
    second, _ = rig.run("run_2")

    assert second.counters[counters.DISCOVERED] == 1
    assert second.counters[counters.FETCHED] == 1
    assert second.counters[counters.NORMALIZED] == 1
    assert second.counters[counters.COMMITTED] == 1
    assert second.counters[counters.SKIPPED_ALREADY_PROCESSED] == N_EPISODES
    assert rig.fake.counts()[FETCH] == N_EPISODES + 1

    after = _updated_at(rig)
    assert len(after) == N_EPISODES + 1
    assert {uid: after[uid] for uid in timestamps} == timestamps


def test_bumping_the_ruleset_version_reruns_qc_and_nothing_else(tmp_path: Path) -> None:
    """A threshold edit must not re-download 50 GB — that is why staleness returns a stage."""
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1")

    rig.ruleset_version = "rules@2"
    second, _ = rig.run("run_2")

    assert second.counters[counters.STALE_REQC] == N_EPISODES
    assert second.counters[counters.QC_DONE] == N_EPISODES
    assert second.counters[counters.COMMITTED] == N_EPISODES
    assert second.counters[counters.FETCHED] == 0
    assert second.counters[counters.NORMALIZED] == 0
    assert rig.fake.counts()[FETCH] == N_EPISODES
    assert rig.fake.counts()[NORMALIZE] == N_EPISODES

    versions = {row["ruleset_version"] for row in rig.rows("episodes", "episode_uid")}
    assert versions == {"rules@2"}
    # The old verdicts are kept: history is appended to, never overwritten.
    assert len(rig.rows("qc_results", "id")) == 2 * 2 * N_EPISODES


def test_bumping_the_adapter_version_renormalizes_without_refetching(tmp_path: Path) -> None:
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1")

    rig.fake.adapter_version = "fake@2"
    second, _ = rig.run("run_2")

    assert second.counters[counters.STALE_RENORMALIZE] == N_EPISODES
    assert second.counters[counters.NORMALIZED] == N_EPISODES
    assert second.counters[counters.FETCHED] == 0
    assert rig.fake.counts()[FETCH] == N_EPISODES, "raw bytes are immutable; never re-fetched"
    assert rig.fake.counts()[NORMALIZE] == 2 * N_EPISODES


def test_recovery_deletes_orphan_temp_files(tmp_path: Path) -> None:
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1")
    orphan = rig.root / "raw/fake/episode_000000/frames.parquet.tmp"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"half a parquet file")

    _, recovery = rig.run("run_2")

    assert recovery.orphan_temp_files == ("raw/fake/episode_000000/frames.parquet.tmp",)
    assert not orphan.exists()


def test_an_unreadable_artifact_demotes_its_episode_instead_of_being_exported(
    tmp_path: Path,
) -> None:
    """The catalog may only vouch for files the filesystem can still open."""
    rig = Rig.build(tmp_path / "store", n_episodes=N_EPISODES)
    rig.run("run_1", fault="qc.before", occurrence=1)

    damaged = rig.root / "normalized/fake/episode_000000/frames.parquet"
    assert damaged.exists()
    damaged.write_bytes(b"not parquet any more")

    _, recovery = rig.run("run_2")

    assert recovery.demoted == ("fake:episode_000000",)
    assert rig.fake.counts()[NORMALIZE] == N_EPISODES + 1
    assert rig.fake.counts()[FETCH] == N_EPISODES
    assert all(
        row["status"] == IngestionStage.COMMITTED.value for row in rig.rows("episodes", "1")
    )
