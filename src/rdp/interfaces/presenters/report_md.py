"""Report rendering. Markdown for a human, JSON for a machine, a console table for a terminal.

All three read the same `Report`/`IngestionRun` and none of them computes a statistic: the
definitions live in `domain/run.py`, so a second presenter cannot invent a fourth answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from rdp.application.build_report import Cumulative, Report
from rdp.domain.run import STAGES, IngestionRun
from rdp.infrastructure.storage.atomic_fs import atomic_write_text

MEASURED_SECTIONS = ("Stage wall time", "Recovery", "Failures")
"""Sections SQL cannot reproduce: a duration, what the filesystem looked like during recovery,
and free-text error strings. Every other section is a number the catalog can be asked for again,
and `scripts/check_report_consistency.py` asserts it knows all of them."""


def render_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def render_markdown(report: Report) -> str:
    lines = ["# Ingestion report", ""]
    if report.run is None and report.cumulative is None:
        return "\n".join([*lines, "No run has been recorded yet.", ""])
    if report.run is not None:
        lines += _run_sections(report)
    if report.cumulative is not None:
        lines += _cumulative_sections(report)
    return "\n".join(lines) + "\n"


def _run_sections(report: Report) -> list[str]:
    run = report.run or {}
    stats = run.get("stats", {})
    lines = [
        f"- run_id: `{run['run_id']}`",
        f"- started: {run['started_at']}",
        f"- finished: {run['finished_at'] or '(unfinished)'}",
        f"- status: {run['status']}",
        f"- resumed_from: {run.get('resumed_from') or '(not a resume)'}",
        f"- args: `{json.dumps(run['args'], sort_keys=True)}`",
        "",
        "## This run",
        "",
    ]
    lines += _table(["counter", "value"], sorted(stats.get("counters", {}).items()))

    lines += ["", "## Stage wall time", ""]
    if "stage_seconds" in stats:
        lines += _table(["stage", "seconds", "episodes", "s/episode"], _timing_rows(stats))
    else:
        # Never zero-fill an absence: this run finished before the pipeline measured stages,
        # and a table of 0.000 would read as "instant" rather than "not measured".
        lines += ["_Not measured: this run predates stage timing._"]

    recovery = {key: value for key, value in stats.get("recovery", {}).items() if value}
    if recovery:
        # What the crash left behind, and what was done about it.
        lines += ["", "## Recovery", ""]
        lines += _table(
            ["finding", "value"],
            [(key, json.dumps(value)) for key, value in sorted(recovery.items())],
        )

    lines += ["", "## QC verdicts (this run)", ""]
    lines += _table(["rule", "verdict", "episodes"], _verdict_rows(report.run_verdicts))

    # Why a rule did not run is a result, not the absence of one, and each reason gets its own
    # row: "there is no action" and "the action is an episode label" are different findings.
    lines += ["", "## Skipped rules (this run)", ""]
    lines += _table(["rule", "reason", "episodes"], _reason_rows(report.run_skip_reasons))

    lines += ["", "## Failure reasons (this run)", ""]
    lines += _table(["reason", "episodes"], sorted(stats.get("failure_reasons", {}).items()))

    failures = stats.get("failures", [])
    if failures:
        lines += ["", "## Failures", ""]
        lines += _table(
            ["episode_uid", "error"],
            [(item["episode_uid"], item["error"]) for item in failures],
        )
    return lines


def _cumulative_sections(report: Report) -> list[str]:
    cumulative = report.cumulative or Cumulative()
    lines = ["", "## Catalog totals", ""]
    lines += _table(["stage", "episodes"], sorted(report.stage_counts.items()))

    lines += ["", "## Corpus (committed)", ""]
    lines += _table(
        ["measure", "value"],
        [
            ("episodes", cumulative.episodes),
            ("frames", cumulative.frames),
            ("duration_s", f"{cumulative.duration_s:.3f}"),
        ],
    )

    lines += ["", "## Source x embodiment", ""]
    lines += _table(
        ["source", "embodiment", "episodes", "frames"],
        [list(row) for row in cumulative.by_source_embodiment],
    )

    lines += ["", "## QC verdicts (cumulative)", ""]
    lines += _table(["rule", "verdict", "episodes"], _verdict_rows(cumulative.verdicts))

    lines += ["", "## QC rule rates (cumulative)", ""]
    lines += _table(
        ["rule", "evaluated", "hits", "skipped", "errors", "hit_rate", "skip_rate"],
        [
            (
                rate.rule_id,
                rate.evaluated,
                rate.hits,
                rate.skipped,
                rate.errors,
                f"{rate.hit_rate:.4f}",
                f"{rate.skip_rate:.4f}",
            )
            for rate in cumulative.rates
        ],
    )

    lines += ["", "## Skip reasons (cumulative)", ""]
    lines += _table(["rule", "reason", "episodes"], _reason_rows(cumulative.skip_reasons))

    lines += ["", "## Disk usage", ""]
    lines += _table(["layer", "bytes"], sorted(cumulative.disk_usage.items()))
    return lines


def _timing_rows(stats: dict[str, Any]) -> list[Any]:
    seconds = stats.get("stage_seconds", {})
    calls = stats.get("stage_calls", {})
    rows = []
    for stage in STAGES:
        total = float(seconds.get(stage, 0.0))
        n = int(calls.get(stage, 0))
        rows.append((stage, f"{total:.3f}", n, f"{total / n:.3f}" if n else "-"))
    return rows


def _verdict_rows(counts: dict[str, dict[str, int]]) -> list[Any]:
    return [
        (rule_id, verdict, count)
        for rule_id, verdicts in sorted(counts.items())
        for verdict, count in sorted(verdicts.items())
    ]


def _reason_rows(counts: dict[str, dict[str, int]]) -> list[Any]:
    return [
        (rule_id, reason, count)
        for rule_id, reasons in sorted(counts.items())
        for reason, count in sorted(reasons.items())
    ]


def _table(headers: list[str], rows: list[Any]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return out


def print_report(report: Report, console: Console | None = None) -> None:
    console = console or Console()
    if report.run is not None:
        console.print(
            f"[bold]run[/bold] {report.run['run_id']}  status={report.run['status']}"
        )
        _print_run_tables(report.run["stats"], console)
        if report.run_skip_reasons:
            console.print(_reason_table("skipped this run", report.run_skip_reasons))

    stages = Table("stage", "episodes", box=None)
    for stage, count in sorted(report.stage_counts.items()):
        stages.add_row(stage, str(count))
    console.print(stages)

    if report.cumulative is not None:
        _print_cumulative_tables(report.cumulative, console)


def _print_run_tables(stats: dict[str, Any], console: Console) -> None:
    counters = Table("counter", "value", box=None)
    for name, value in sorted(stats.get("counters", {}).items()):
        counters.add_row(name, str(value))
    console.print(counters)

    if "stage_seconds" not in stats:
        console.print("[dim]stage wall time: not measured for this run[/dim]")
        return
    timing = Table("stage", "seconds", "episodes", "s/episode", box=None)
    for stage, total, n, per in _timing_rows(stats):
        timing.add_row(stage, total, str(n), per)
    console.print(timing)


def _print_cumulative_tables(cumulative: Cumulative, console: Console) -> None:
    corpus = Table("source", "embodiment", "episodes", "frames", box=None)
    for source, embodiment, episodes, frames in cumulative.by_source_embodiment:
        corpus.add_row(source, embodiment, str(episodes), str(frames))
    corpus.add_row("[bold]total[/bold]", "", str(cumulative.episodes), str(cumulative.frames))
    console.print(corpus)

    rates = Table("rule", "evaluated", "hits", "skipped", "hit_rate", "skip_rate", box=None)
    for rate in cumulative.rates:
        rates.add_row(
            rate.rule_id,
            str(rate.evaluated),
            str(rate.hits),
            str(rate.skipped),
            f"{rate.hit_rate:.2%}",
            f"{rate.skip_rate:.2%}",
        )
    console.print(rates)

    if cumulative.skip_reasons:
        console.print(_reason_table("skipped (cumulative)", cumulative.skip_reasons))

    disk = Table("layer", "MiB", box=None)
    for layer, size in sorted(cumulative.disk_usage.items()):
        disk.add_row(layer, f"{size / 1048576:.1f}")
    console.print(disk)


def _reason_table(title: str, counts: dict[str, dict[str, int]]) -> Table:
    table = Table("rule", title, "episodes", box=None)
    for rule_id, reason, count in _reason_rows(counts):
        table.add_row(rule_id, reason, str(count))
    return table


def run_only_report(run: IngestionRun) -> Report:
    """A `Report` over one finished run, with no catalog query behind it."""
    stats = run.stats()
    return Report(
        run=run.as_payload(),
        run_verdicts={rule: dict(counts) for rule, counts in stats["rule_counts"].items()},
        run_skip_reasons=stats["skip_reasons"],
    )


class FileRunReporter:
    """Writes `reports/<run_id>.json` — the machine-readable record of one run."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, run: IngestionRun) -> None:
        atomic_write_text(
            self.directory / f"{run.run_id}.json",
            json.dumps(run.as_payload(), indent=2, sort_keys=True),
        )


class MarkdownRunReporter:
    """Writes `reports/<run_id>.md`, so a run documents itself without `rdp report` being run.

    Run sections only: the cumulative half needs the catalog, and `rdp report` is what asks it.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, run: IngestionRun) -> None:
        atomic_write_text(
            self.directory / f"{run.run_id}.md", render_markdown(run_only_report(run))
        )


class ConsoleRunReporter:
    """The same numbers on the terminal as the run ends.

    Its point is partly that it exists: a third implementation of `RunReporter` that needed no
    change in `application/` or `domain/` is the evidence that the port is a seam rather than
    decoration (design §10.7).
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def publish(self, run: IngestionRun) -> None:
        colour = "green" if run.status == "COMPLETED" else "yellow"
        self.console.print(f"[{colour}]run {run.run_id} {run.status}[/{colour}]")
        if run.resumed_from:
            self.console.print(f"  resumed from {run.resumed_from}")
        _print_run_tables(run.stats(), self.console)
        for reason, count in run.top_failure_reasons():
            self.console.print(f"  [red]{reason}[/red] {count}")
