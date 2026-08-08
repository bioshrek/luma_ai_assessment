"""M7: the report is a query, and an independent query agrees with it.

Two properties are pinned here. First, **reproducibility**: `rdp report` reads only the catalog,
so rendering it twice — or after the ingesting process is long gone — gives the same bytes.
Second, **consistency**: `scripts/check_report_consistency.py` re-derives every non-measured
number with its own SQL, spelled differently from the repositories', and the two must agree.

The corpus is pusht plus epic on purpose: pusht skips `TS_MONOTONIC` because its clock is
synthesized, epic skips the action rules because its action is an episode label. A report that
collapsed skip reasons into one bucket would look fine on either source alone.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from scripts.check_report_consistency import check_verbose, parse_sections
from tests.conftest import _EPIC, _PUSHT, WorkspaceFactory
from typer.testing import CliRunner

from rdp.domain.run import FETCH, IngestionRun
from rdp.interfaces.cli import app
from rdp.interfaces.presenters.report_md import (
    MEASURED_SECTIONS,
    ConsoleRunReporter,
    MarkdownRunReporter,
    render_json,
    render_markdown,
    run_only_report,
)
from rdp.interfaces.wiring import Container

runner = CliRunner()


def _ingest(container: Container, source_id: str) -> IngestionRun:
    source = container.sources[source_id]
    run = IngestionRun(run_id=container.new_run_id(), started_at=container.clock.now_iso())
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
def reported(make_workspace: WorkspaceFactory) -> Container:
    container = make_workspace(blocks=(_PUSHT, _EPIC))
    for source_id in ("pusht", "epic100"):
        _ingest(container, source_id)
    return container


def test_every_reported_number_is_reproduced_by_an_independent_query(
    reported: Container,
) -> None:
    reported.catalog.close()  # the checker opens the store itself, as the CLI would
    problems, compared = check_verbose(reported.paths.store, reported.paths.config)
    assert problems == []
    # A pass with nothing compared would be no evidence at all.
    assert len(compared) >= 10
    assert "QC verdicts (cumulative)" in compared
    assert "Corpus (committed)" in compared


def test_the_checker_fails_when_the_catalog_and_the_report_disagree(
    reported: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: without it, a checker that compares nothing also passes."""
    import scripts.check_report_consistency as checker

    real = checker.expected

    def wrong(conn, usage, run_id):  # type: ignore[no-untyped-def]
        out = real(conn, usage, run_id)
        out["Corpus (committed)"] = [("episodes", "999"), ("frames", "0"), ("duration_s", "0")]
        return out

    monkeypatch.setattr(checker, "expected", wrong)
    reported.catalog.close()
    problems, _ = check_verbose(reported.paths.store, reported.paths.config)
    assert any(problem.startswith("Corpus (committed)") for problem in problems)


def test_a_report_section_with_no_query_behind_it_is_a_failure(
    reported: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a table without adding a query must not ship silently."""
    import scripts.check_report_consistency as checker

    real = checker.render_markdown
    monkeypatch.setattr(
        checker,
        "render_markdown",
        lambda report: real(report) + "\n## Invented\n\n| a |\n|---|\n| 1 |\n",
    )
    reported.catalog.close()
    problems, _ = check_verbose(reported.paths.store, reported.paths.config)
    assert "Invented: no SQL reproduces this section" in problems


def test_rendering_the_report_twice_gives_identical_bytes(reported: Container) -> None:
    """It is built from the catalog alone, so it does not matter when it is asked."""
    first = render_markdown(reported.report()())
    second = render_markdown(reported.report()())
    assert first == second
    assert render_json(reported.report()()) == render_json(reported.report()())


def test_a_report_read_back_after_the_run_object_is_gone_is_the_same_report(
    reported: Container,
) -> None:
    with reported.unit_of_work() as uow:
        run_id = str(uow.runs.latest()["run_id"])
    reported.catalog.close()
    del reported.__dict__["catalog"]  # the connection the ingest used is gone
    assert render_markdown(reported.report()(run_id)) == render_markdown(reported.report()())


def test_the_two_sources_keep_their_skip_reasons_apart(reported: Container) -> None:
    report = reported.report()(cumulative_only=True)
    assert report.cumulative is not None
    reasons = report.cumulative.skip_reasons
    distinct = {reason for per_rule in reasons.values() for reason in per_rule}
    assert len(distinct) > 1, reasons
    assert "TS_MONOTONIC" in reasons


def test_stage_wall_time_is_the_one_thing_the_catalog_cannot_be_asked_for(
    reported: Container,
) -> None:
    sections = parse_sections(render_markdown(reported.report()()))
    assert "Stage wall time" in sections
    assert "Stage wall time" in MEASURED_SECTIONS
    rows = {row[0]: row for row in sections["Stage wall time"]}
    assert FETCH in rows
    # Every stage that ran was timed, and a duration is not a number any SQL over the catalog
    # could produce — which is exactly why the consistency checker exempts this section.
    assert all(float(row[1]) >= 0.0 for row in rows.values())
    assert int(rows[FETCH][2]) > 0


def test_the_run_reporter_port_has_more_than_one_real_implementation(
    reported: Container, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit criterion: a port with one implementation is an interface nobody has tested."""
    run = IngestionRun(run_id="run_x", started_at="2026-01-01T00:00:00Z")
    run.count("committed", 3)
    run.record_stage(FETCH, 0.5)
    run.record_failure("pusht:episode_000000", "FetchError: gone")
    run.finish("2026-01-01T00:01:00Z")

    reporters = reported.run_reporters()
    assert len(reporters) >= 3
    for reporter in reporters:
        reporter.publish(run)

    written = {path.name for path in reported.paths.reports.iterdir()}
    assert {"run_x.json", "run_x.md"} <= written
    markdown = (reported.paths.reports / "run_x.md").read_text()
    assert "## Stage wall time" in markdown
    assert "FetchError" in markdown
    assert "run_x" in capsys.readouterr().out


def test_a_run_only_report_carries_no_cumulative_claims() -> None:
    """The file a run publishes describes that run; the catalog view is `rdp report`."""
    run = IngestionRun(run_id="run_y", started_at="2026-01-01T00:00:00Z")
    report = run_only_report(run)
    assert report.cumulative is None
    markdown = render_markdown(report)
    assert "## Corpus (committed)" not in markdown
    assert "## This run" in markdown


def test_the_markdown_reporter_writes_where_the_file_reporter_does(
    reported: Container,
) -> None:
    run = IngestionRun(run_id="run_z", started_at="2026-01-01T00:00:00Z")
    MarkdownRunReporter(reported.paths.reports).publish(run)
    assert (reported.paths.reports / "run_z.md").exists()
    ConsoleRunReporter().publish(run)  # must not raise on an empty run


def test_a_run_from_before_stage_timing_says_so_instead_of_showing_zeros() -> None:
    """Never zero-fill an absence — 0.000 s reads as "instant", not as "not measured"."""
    report = run_only_report(IngestionRun(run_id="old", started_at="2026-01-01T00:00:00Z"))
    assert report.run is not None
    stats = {k: v for k, v in report.run["stats"].items() if not k.startswith("stage_")}
    markdown = render_markdown(replace(report, run={**report.run, "stats": stats}))
    assert "_Not measured: this run predates stage timing._" in markdown
    assert "| fetch |" not in markdown


def _cli(reported: Container, *args: str) -> str:
    result = runner.invoke(
        app,
        [
            "report",
            "--store",
            str(reported.paths.store),
            "--config",
            str(reported.paths.config),
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_the_json_format_is_machine_readable_on_plain_stdout(reported: Container) -> None:
    """`rdp report --format json > x.json` has to produce JSON, not a rendered panel."""
    reported.catalog.close()
    payload = json.loads(_cli(reported, "--format", "json"))
    assert payload["cumulative"]["episodes"] > 0
    assert payload["run"]["run_id"]


def test_the_markdown_format_is_the_same_report_the_library_renders(
    reported: Container,
) -> None:
    """Every catalog-derived section survives the trip through the CLI, byte for byte.

    Disk usage is excluded because it is a live measurement rather than a catalog number:
    closing the last WAL connection checkpoints, and `catalog.sqlite` grows from 4 KB to 168 KB
    between the two renders without a single row changing.
    """
    expected = parse_sections(render_markdown(reported.report()()))
    reported.catalog.close()
    actual = parse_sections(_cli(reported, "--format", "md"))
    assert actual.keys() == expected.keys()
    for title in expected:
        if title == "Disk usage":
            continue
        assert actual[title] == expected[title], title


def test_cumulative_drops_the_run_scoped_sections(reported: Container) -> None:
    reported.catalog.close()
    sections = parse_sections(_cli(reported, "--cumulative", "--format", "md"))
    assert "This run" not in sections
    assert "Corpus (committed)" in sections


def test_an_unknown_format_is_rejected_rather_than_guessed(reported: Container) -> None:
    reported.catalog.close()
    result = runner.invoke(
        app,
        ["report", "--store", str(reported.paths.store), "--format", "yaml"],
    )
    assert result.exit_code != 0
