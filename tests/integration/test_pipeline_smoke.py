"""The M1 walking skeleton, end to end: real SQLite, real parquet, real fixture bytes.

No network and no mocks below the source URI. What this pins down is the two acceptance
scenarios' second half — a re-run must not re-ingest — plus the `frames.parquet` column
contract, which is where a wrong channel mapping would hide.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from rdp.domain.errors import BudgetTooSmall
from rdp.domain.qc.rule import EpisodeVerdict, Verdict
from rdp.domain.run import IngestionRun
from rdp.domain.stage import IngestionStage
from rdp.interfaces.wiring import Container

EXPECTED_COLUMNS = {
    "t",
    "action.ee.x",
    "action.ee.y",
    "state.ee.x",
    "state.ee.y",
    "raw.next.reward",
    "raw.next.done",
    "raw.next.success",
}


def _ingest(container: Container, run_id: str) -> IngestionRun:
    source = container.sources["pusht"]
    run = IngestionRun(run_id=run_id, started_at=container.clock.now_iso())
    with container.unit_of_work() as uow:
        uow.sources.upsert(source)
        uow.runs.start(run)
        uow.commit()
    container.ingest()(source, container.adapter_for(source), run)
    run.finish(container.clock.now_iso())
    with container.unit_of_work() as uow:
        uow.runs.finish(run)
        uow.commit()
    return run


@pytest.fixture
def ingested(workspace: Container) -> Container:
    _ingest(workspace, "run_1")
    return workspace


def test_a_first_run_commits_every_episode_in_the_fixture(ingested: Container) -> None:
    with ingested.unit_of_work() as uow:
        assert uow.episodes.counts_by_stage() == {IngestionStage.COMMITTED.value: 3}


def test_a_second_run_re_ingests_nothing(ingested: Container) -> None:
    """Acceptance scenario 2. `skipped_already_processed` is the whole point."""
    run = _ingest(ingested, "run_2")
    assert run.counters["skipped_already_processed"] == 3
    for counter in ("discovered", "fetched", "normalized", "committed"):
        assert run.counters[counter] == 0


def test_content_hash_is_stable_across_runs(ingested: Container) -> None:
    with ingested.unit_of_work() as uow:
        before = {
            e.uid: e.content_hash for e in uow.episodes.list_by_stage(IngestionStage.COMMITTED)
        }
    _ingest(ingested, "run_2")
    with ingested.unit_of_work() as uow:
        after = {
            e.uid: e.content_hash for e in uow.episodes.list_by_stage(IngestionStage.COMMITTED)
        }
    assert before == after
    assert all(value for value in before.values())


def test_frames_parquet_holds_exactly_the_declared_columns(ingested: Container) -> None:
    table = pq.read_table(
        ingested.paths.normalized / "pusht/episode_000000/frames.parquet"
    )
    assert set(table.column_names) == EXPECTED_COLUMNS
    metadata = table.schema.metadata or {}
    assert json.loads(metadata[b"rdp.raw_frame_columns"]) == [
        "raw.next.reward",
        "raw.next.done",
        "raw.next.success",
    ]


def test_pusht_actions_are_pixels_not_a_guessed_unit(ingested: Container) -> None:
    """The channel names upstream are `motor_0`/`motor_1`; they are neither motors nor metres."""
    with ingested.unit_of_work() as uow:
        episode = uow.episodes.get("pusht:episode_000000")
    assert episode is not None and episode.meta is not None
    channels = episode.meta.action_spec.channels
    assert [c.name for c in channels] == ["ee.x", "ee.y"]
    assert {c.unit for c in channels} == {"px"}
    assert all(c.is_delta is False for c in channels)
    assert episode.meta.action_spec.space == "cartesian_2d"


def test_a_synthesized_clock_makes_the_timestamp_rule_skip_not_pass(
    ingested: Container,
) -> None:
    """pusht's `timestamp` is exactly `frame_index / fps`; see ADR 005."""
    with ingested.unit_of_work() as uow:
        episode = uow.episodes.get("pusht:episode_000000")
        counts = uow.qc_results.verdict_counts()
    assert episode is not None and episode.meta is not None
    assert episode.meta.provenance.timestamp_source == "synthesized@10Hz"
    assert episode.meta.has_real_timestamps is False
    assert counts["TS_MONOTONIC"] == {Verdict.SKIPPED.value: 3}
    assert episode.qc_verdict is EpisodeVerdict.PASS


def test_export_writes_whole_episodes_within_the_budget(ingested: Container) -> None:
    out = ingested.paths.store / "subset.jsonl"
    result = ingested.export()(out=out, budget_frames=300, run_id="run_1")
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert result.n_frames <= 300
    assert len(lines) == result.n_episodes
    for record in lines:
        assert record["frame_start"] == 0
        assert record["frame_end"] == record["n_frames"]
        assert record["action_space"] == "cartesian_2d"
        assert record["embodiment"] == "pusht_planar"
        assert Path(record["frames_path"]).parts[0] == "pusht"
        assert record["capabilities"]["has_action"] is True


def test_export_refuses_to_truncate_an_episode_to_hit_the_budget(
    ingested: Container,
) -> None:
    with pytest.raises(BudgetTooSmall):
        ingested.export()(out=ingested.paths.store / "tiny.jsonl", budget_frames=5)


def test_report_summarises_the_last_run(ingested: Container) -> None:
    report = ingested.report()()
    assert report.run is not None
    assert report.stage_counts == {"COMMITTED": 3}
    assert report.rule_counts["TS_MONOTONIC"]["SKIPPED"] == 3
