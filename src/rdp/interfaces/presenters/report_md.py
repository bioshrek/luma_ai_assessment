"""Report rendering. Markdown for a human, JSON for a machine, both from the same `Report`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from rdp.application.build_report import Report
from rdp.domain.run import IngestionRun
from rdp.infrastructure.storage.atomic_fs import atomic_write_text


def render_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def render_markdown(report: Report) -> str:
    lines = ["# Ingestion report", ""]
    run = report.run
    if run is None:
        lines.append("No run has been recorded yet.")
        return "\n".join(lines) + "\n"

    lines += [
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
    lines += _table(["counter", "value"], sorted(run["stats"].get("counters", {}).items()))

    recovery = {k: v for k, v in run["stats"].get("recovery", {}).items() if v}
    if recovery:
        # What the crash left behind, and what was done about it.
        lines += ["", "## Recovery", ""]
        lines += _table(
            ["finding", "value"],
            [(key, json.dumps(value)) for key, value in sorted(recovery.items())],
        )

    skip_reasons = run["stats"].get("skip_reasons", {})
    if skip_reasons:
        # Why a rule did not run is a result, not an absence of one.
        lines += ["", "## Skipped rules", ""]
        lines += _table(["rule:reason", "episodes"], sorted(skip_reasons.items()))

    failures = run["stats"].get("failures", [])
    if failures:
        lines += ["", "## Failures", ""]
        lines += _table(
            ["episode_uid", "error"],
            [(item["episode_uid"], item["error"]) for item in failures],
        )

    lines += ["", "## Catalog totals", ""]
    lines += _table(["stage", "episodes"], sorted(report.stage_counts.items()))

    if report.rule_counts:
        lines += ["", "## QC verdicts (cumulative)", ""]
        rows = [
            (rule_id, verdict, str(count))
            for rule_id, verdicts in sorted(report.rule_counts.items())
            for verdict, count in sorted(verdicts.items())
        ]
        lines += _table(["rule", "verdict", "episodes"], rows)

    return "\n".join(lines) + "\n"


def _table(headers: list[str], rows: list[Any]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return out


def print_report(report: Report, console: Console | None = None) -> None:
    console = console or Console()
    run = report.run
    if run is not None:
        console.print(f"[bold]run[/bold] {run['run_id']}  status={run['status']}")
        counters = Table("counter", "value", box=None)
        for name, value in sorted(run["stats"].get("counters", {}).items()):
            counters.add_row(name, str(value))
        console.print(counters)

    stages = Table("stage", "episodes", box=None)
    for stage, count in sorted(report.stage_counts.items()):
        stages.add_row(stage, str(count))
    console.print(stages)

    if report.rule_counts:
        rules = Table("rule", "verdict", "episodes", box=None)
        for rule_id, verdicts in sorted(report.rule_counts.items()):
            for verdict, count in sorted(verdicts.items()):
                rules.add_row(rule_id, verdict, str(count))
        console.print(rules)


class FileRunReporter:
    """Writes `reports/run_<id>.json`. The markdown is produced by `rdp report`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, run: IngestionRun) -> None:
        payload = {
            "run_id": run.run_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "status": run.status,
            "resumed_from": run.resumed_from,
            "args": run.args,
            "stats": run.stats(),
        }
        atomic_write_text(
            self.directory / f"{run.run_id}.json", json.dumps(payload, indent=2, sort_keys=True)
        )
