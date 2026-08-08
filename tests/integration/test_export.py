"""Curation end to end: two embodiments, one budget, and a manifest that replays exactly.

The fixtures are deliberately unequal in size — that is the whole point of stratifying, and a
single-source corpus would have hidden every quota bug in M1's sequential strategy.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.conftest import _ALOHA, _PUSHT, WorkspaceFactory
from typer.testing import CliRunner

from rdp.domain.run import IngestionRun
from rdp.interfaces.cli import app
from rdp.interfaces.wiring import Container

runner = CliRunner()


def _ingest(container: Container, source_id: str) -> None:
    source = container.sources[source_id]
    run = IngestionRun(run_id=container.new_run_id(), started_at=container.clock.now_iso())
    with container.unit_of_work() as uow:
        uow.sources.upsert(source)
        uow.runs.start(run)
        uow.commit()
    container.ingest()(source, container.adapter_for(source), run)


@pytest.fixture
def mixed(make_workspace: WorkspaceFactory) -> Container:
    container = make_workspace(blocks=(_PUSHT, _ALOHA))
    for source_id in ("pusht", "aloha_sim_insertion"):
        _ingest(container, source_id)
    return container


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _eligible_frames(container: Container) -> dict[str, int]:
    with container.unit_of_work() as uow:
        episodes = uow.episodes.list_exportable(verdicts=["PASS"])
    out: dict[str, int] = {}
    for episode in episodes:
        assert episode.meta is not None
        out[episode.meta.embodiment] = out.get(episode.meta.embodiment, 0) + episode.meta.n_frames
    return out


def test_a_balanced_export_gives_the_smaller_embodiment_more_than_its_size_share(
    mixed: Container,
) -> None:
    eligible = _eligible_frames(mixed)
    budget = min(eligible.values())
    result = mixed.export()(out=mixed.paths.store / "mixed.jsonl", budget_frames=budget, seed=7)

    weights = {group.embodiment: group.weight for group in result.plan.groups}
    assert set(weights) == set(eligible)
    assert sum(weights.values()) == pytest.approx(1.0)
    smaller = min(eligible, key=lambda name: eligible[name])
    size_share = eligible[smaller] / sum(eligible.values())
    # Square-root smoothing exists precisely so the small embodiment is not drowned.
    assert weights[smaller] > size_share
    assert result.n_frames <= budget


def test_the_same_seed_writes_a_byte_identical_manifest(mixed: Container) -> None:
    """The reviewer's check: two exports, one `diff`, no output."""
    first = mixed.paths.store / "a.jsonl"
    second = mixed.paths.store / "b.jsonl"
    for out in (first, second):
        mixed.export()(out=out, budget_frames=1200, seed=7)
    assert first.read_bytes() == second.read_bytes()


def test_the_embodiment_filter_spends_the_whole_budget_on_that_embodiment(
    mixed: Container,
) -> None:
    out = mixed.paths.store / "aloha.jsonl"
    result = mixed.export()(out=out, budget_frames=1200, embodiment="aloha_bimanual", seed=7)

    assert [group.embodiment for group in result.plan.groups] == ["aloha_bimanual"]
    assert {record["embodiment"] for record in _records(out)} == {"aloha_bimanual"}
    assert result.n_episodes > 0


def test_every_exported_entry_is_a_whole_episode(mixed: Container) -> None:
    out = mixed.paths.store / "whole.jsonl"
    mixed.export()(out=out, budget_frames=1200, seed=7)

    for record in _records(out):
        assert record["frame_start"] == 0
        assert record["frame_end"] == record["n_frames"]


def test_the_export_row_records_everything_needed_to_reproduce_it(mixed: Container) -> None:
    mixed.export()(out=mixed.paths.store / "recorded.jsonl", budget_frames=1200, seed=7)

    with sqlite3.connect(mixed.paths.catalog) as conn:
        row = conn.execute(
            "SELECT strategy, seed, embodiment, include_review, stats_json, n_frames FROM exports"
        ).fetchone()
    strategy, seed, embodiment, include_review, stats_json, n_frames = row
    stats = json.loads(stats_json)
    assert (strategy, seed, embodiment, include_review) == ("balanced", 7, None, 0)
    assert stats["used_frames"] == n_frames
    assert sum(group["selected_frames"] for group in stats["groups"]) == n_frames


def test_a_budget_below_the_shortest_episode_exits_non_zero(mixed: Container) -> None:
    result = runner.invoke(
        app,
        [
            "export",
            "--budget",
            "1",
            "--out",
            str(mixed.paths.store / "impossible.jsonl"),
            "--store",
            str(mixed.paths.store),
            "--config",
            str(mixed.paths.config),
        ],
    )
    assert result.exit_code == 2
    assert "never truncated" in result.stdout


def test_an_unknown_strategy_is_rejected_by_name(mixed: Container) -> None:
    with pytest.raises(ValueError, match="balanced, sequential"):
        mixed.export()(out=mixed.paths.store / "nope.jsonl", budget_frames=1200, strategy="random")
