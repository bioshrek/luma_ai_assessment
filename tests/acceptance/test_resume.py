"""Acceptance scenario 1: crash, restart, resume — at every checkpoint the pipeline has.

The claim under test is not "it recovers" but the two measurable consequences of recovering:

1. **No work is repeated.** Across the crashed run plus the resume, `fetch` and `normalize` are
   called exactly as many times as in one uninterrupted run — except for the single stage that
   was in flight when the process died, which is redone once and no more.
2. **The outcome is the same.** The catalog rows and the parquet bytes are identical, field by
   field and byte for byte, to the uninterrupted baseline.

`REDONE` is the honest form of claim 1. A crash *after* the work but *before* the row that
records it must redo that one unit of work — there is no other correct behaviour, since the
alternative is trusting an artifact no transaction ever vouched for. Making that cost explicit
per checkpoint, rather than asserting a blanket equality, is what turns the test into a
statement about the protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.acceptance.rig import Rig
from tests.fakes.source import FETCH, NORMALIZE

from rdp.application.ingest_episodes import (
    CHECKPOINTS,
    COMMIT_AFTER_FILE_BEFORE_DB,
    FETCH_AFTER,
    NORMALIZE_AFTER_WRITE_BEFORE_COMMIT,
    QC_BEFORE,
)
from rdp.domain import run as counters
from rdp.domain.stage import IngestionStage

N_EPISODES = 3

# The one unit of work each checkpoint costs, because it crashed between doing the work and
# recording that it was done.
REDONE: dict[str, dict[str, int]] = {
    FETCH_AFTER: {FETCH: 1},
    NORMALIZE_AFTER_WRITE_BEFORE_COMMIT: {NORMALIZE: 1},
}


def _baseline(tmp_path: Path) -> Rig:
    rig = Rig.build(tmp_path / "baseline", n_episodes=N_EPISODES)
    rig.run("run_baseline")
    return rig


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_a_crash_at_any_checkpoint_resumes_to_the_uninterrupted_result(
    tmp_path: Path, checkpoint: str
) -> None:
    baseline = _baseline(tmp_path)

    rig = Rig.build(tmp_path / "crashed", n_episodes=N_EPISODES)
    # Occurrence 2: die while processing the second episode, so the resume has both finished
    # work to skip and unfinished work to continue.
    crashed, _ = rig.run("run_1", fault=checkpoint, occurrence=2)
    assert crashed.counters[counters.COMMITTED] < N_EPISODES, (
        f"{checkpoint} did not actually interrupt the run"
    )

    resumed, recovery = rig.run("run_2")

    expected = {name: baseline.fake.counts()[name] for name in (FETCH, NORMALIZE)}
    for name, extra in REDONE.get(checkpoint, {}).items():
        expected[name] += extra
    assert {name: rig.fake.counts().get(name, 0) for name in (FETCH, NORMALIZE)} == expected

    assert rig.snapshot() == baseline.snapshot()
    assert rig.artifact_hashes() == baseline.artifact_hashes()
    assert recovery.resumed_from == "run_1"
    assert resumed.counters[counters.COMMITTED] + crashed.counters[counters.COMMITTED] == (
        N_EPISODES
    )


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_a_crash_leaves_no_orphan_temp_files_after_recovery(
    tmp_path: Path, checkpoint: str
) -> None:
    rig = Rig.build(tmp_path / "crashed", n_episodes=N_EPISODES)
    rig.run("run_1", fault=checkpoint, occurrence=2)
    rig.run("run_2")
    assert rig.temp_files() == []


def test_the_resuming_run_records_which_run_it_picked_up_from(tmp_path: Path) -> None:
    """`resumed_from` is how the report can say "this was a resume" without guessing."""
    rig = Rig.build(tmp_path / "crashed", n_episodes=N_EPISODES)
    rig.run("run_1", fault=QC_BEFORE, occurrence=2)
    rig.run("run_2")

    runs = rig.rows("runs", "started_at, rowid")
    assert [row["run_id"] for row in runs] == ["run_1", "run_2"]
    assert runs[0]["status"] == "INTERRUPTED"
    assert runs[0]["resumed_from"] is None
    assert runs[1]["resumed_from"] == "run_1"
    assert runs[1]["status"] == "COMPLETED"


def test_a_crash_before_the_commit_row_still_leaves_the_artifact_usable(
    tmp_path: Path,
) -> None:
    """`commit.after_file_before_db` is the crash the atomic-write protocol exists for."""
    rig = Rig.build(tmp_path / "crashed", n_episodes=N_EPISODES)
    rig.run("run_1", fault=COMMIT_AFTER_FILE_BEFORE_DB, occurrence=2)

    state = rig.rows("episode_state", "episode_uid")[1]
    assert state["stage"] == IngestionStage.QC_DONE.value
    assert state["lease_owner"] is not None, "a crash leaves its lease behind; recovery clears it"

    rig.run("run_2")
    assert rig.fake.counts()[NORMALIZE] == N_EPISODES
    assert all(row["status"] == IngestionStage.COMMITTED.value for row in rig.rows("episodes", "1"))
